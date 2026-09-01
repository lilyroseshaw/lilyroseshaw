import logging
import secrets
from urllib.parse import urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import config, deletion_engine, google_oauth
from app.aggregator import aggregate, store
from app.db import get_session, init_db
from app.deletion_queue import start_background_worker
from app.deletion_research import build_default_provider
from app.deletion_resolver import resolve_deletion_method
from app.deletion_response_tracker import check_company_response
from app.gmail_scan import scan_inbox
from app.models import Company
from app.response_classify import build_default_classifier

app = FastAPI(title="Cookie Monster")
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# TEMPORARY diagnostic logging for the OAuth callback ("Invalid OAuth state
# or missing code") investigation - logs booleans/hostnames only, never the
# actual state/code/token values. Safe to remove once response tracking is
# confirmed working end-to-end against a real Google account. Configured
# with its own handler/level so it prints regardless of uvicorn's own
# logging setup (uvicorn's default root log level would otherwise swallow
# plain INFO-level messages from an unconfigured logger).
_oauth_log = logging.getLogger("cookie_monster.oauth")
_oauth_log.setLevel(logging.INFO)
if not _oauth_log.handlers:
    _oauth_handler = logging.StreamHandler()
    _oauth_handler.setFormatter(logging.Formatter("%(asctime)s [cookie-monster-oauth] %(message)s"))
    _oauth_log.addHandler(_oauth_handler)
    _oauth_log.propagate = False

GMAIL_SCOPE_EXPLANATION = (
    "Read-only access to Gmail message metadata only (sender, subject, date, labels). "
    "Cookie Monster never requests permission to read message bodies/attachments, "
    "send mail, or modify/delete/label anything in your inbox."
)

# One shared provider/classifier instance for the process - built once from
# .env config. Used by both the manual "Research"/"Check for responses"
# routes and the background worker.
_research_provider = build_default_provider()
_response_classifier = build_default_classifier()


@app.on_event("startup")
def on_startup():
    init_db()
    start_background_worker(_research_provider, _response_classifier)


def _status_counts(db) -> dict[str, int]:
    companies = db.query(Company).all()
    return {
        "total": len(companies),
        "confirmed": sum(1 for c in companies if c.status == "confirmed"),
        "pending": sum(1 for c in companies if c.status == "pending"),
        "rejected": sum(1 for c in companies if c.status == "rejected"),
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    db = get_session()
    try:
        connected_address = google_oauth.get_connected_address(db)
        send_enabled = google_oauth.has_send_scope(db)
        response_tracking_enabled = google_oauth.has_readonly_scope(db)
        counts = _status_counts(db)
    finally:
        db.close()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "connected_address": connected_address,
            "scope_explanation": GMAIL_SCOPE_EXPLANATION,
            "counts": counts,
            "google_configured": bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET),
            "send_enabled": send_enabled,
            "response_tracking_enabled": response_tracking_enabled,
        },
    )


def _canonical_host_redirect(request: Request) -> RedirectResponse | None:
    """Google always sends the OAuth callback to the EXACT host baked into
    GOOGLE_REDIRECT_URI - never to whatever host the browser happened to
    start the flow from. If those differ (e.g. GOOGLE_REDIRECT_URI is
    configured for 'localhost' - the .env.example default - but the app
    was opened at '127.0.0.1'), the session cookie set while starting the
    flow is scoped to the host the browser is on right now, and a browser
    correctly never sends that cookie to a different host's request. The
    callback then has no pending state and rejects it as invalid - not
    because anything was tampered with, but because the round trip could
    never have worked. Redirecting to the canonical host FIRST, before any
    session state is written, means the cookie set moments later already
    belongs to the host the callback will land on."""
    try:
        canonical = urlparse(config.GOOGLE_REDIRECT_URI)
    except ValueError:
        return None
    if not canonical.hostname:
        return None
    if request.url.hostname == canonical.hostname and request.url.port == canonical.port:
        return None
    target = request.url.replace(scheme=canonical.scheme, hostname=canonical.hostname, port=canonical.port)
    _oauth_log.info(
        "oauth host mismatch: request_host=%s:%s redirect_uri_host=%s:%s path=%s - "
        "redirecting to canonical host before starting flow",
        request.url.hostname, request.url.port, canonical.hostname, canonical.port, request.url.path,
    )
    return RedirectResponse(str(target))


def _start_oauth_flow(request: Request, flow_name: str, auth_url: str, state: str, scopes: list[str]) -> RedirectResponse:
    """Records this flow's pending (state, scopes) under its OWN key in the
    session, instead of one shared slot. Three flows (login/send/readonly)
    can otherwise be triggered close together - e.g. two tabs, a double
    click, or starting a second 'enable X' before finishing the first one's
    Google consent screen - and a single shared slot means the second write
    silently clobbers the first flow's pending state, so its callback later
    fails with 'Invalid OAuth state' even though the user did everything
    right. Per-flow keys mean starting one flow never erases another's."""
    pending = request.session.get("oauth_pending", {})
    pending[flow_name] = {"state": state, "scopes": scopes}
    request.session["oauth_pending"] = pending
    return RedirectResponse(auth_url)


@app.get("/auth/login")
def auth_login(request: Request):
    redirect = _canonical_host_redirect(request)
    if redirect is not None:
        return redirect
    try:
        auth_url, state = google_oauth.get_authorization_url()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _start_oauth_flow(request, "login", auth_url, state, config.GMAIL_SCOPES)


@app.get("/auth/enable-sending")
def auth_enable_sending(request: Request):
    """SEPARATE, explicit consent step for the optional 'send deletion
    request emails automatically' feature. Never reached from the normal
    connect/scan flow - only from its own clearly-labeled button, after the
    user has read what gmail.send additionally grants."""
    redirect = _canonical_host_redirect(request)
    if redirect is not None:
        return redirect
    db = get_session()
    try:
        try:
            auth_url, state = google_oauth.get_send_authorization_url(db)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Union of whatever's already granted + gmail.send - never drops an
        # already-granted scope (e.g. gmail.readonly, if enabled first).
        scopes = google_oauth.get_granted_scopes(db) | {config.GMAIL_SEND_SCOPE}
    finally:
        db.close()
    return _start_oauth_flow(request, "send", auth_url, state, sorted(scopes))


@app.get("/auth/enable-response-tracking")
def auth_enable_response_tracking(request: Request):
    """SEPARATE, explicit consent step for the optional response-tracking
    feature (gmail.readonly). Never reached from the normal connect/scan
    flow, never bundled with gmail.send - only from its own clearly-labeled
    button, after the user has read exactly what this additionally grants
    and that Cookie Monster only ever reads threads it itself started."""
    redirect = _canonical_host_redirect(request)
    if redirect is not None:
        return redirect
    db = get_session()
    try:
        try:
            auth_url, state = google_oauth.get_response_tracking_authorization_url(db)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        scopes = google_oauth.get_granted_scopes(db) | {config.GMAIL_READONLY_SCOPE}
    finally:
        db.close()
    return _start_oauth_flow(request, "readonly", auth_url, state, sorted(scopes))


@app.get("/auth/callback")
def auth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        return RedirectResponse(f"/?error={error}")

    # Find which pending flow (login/send/readonly) this callback belongs to
    # by matching Google's returned state against each flow's own stored
    # state - NOT a single shared slot, so completing this flow never
    # touches any other flow that might still be pending in this session.
    pending = request.session.get("oauth_pending", {})
    matched_flow_name = None
    if state:
        for flow_name, entry in pending.items():
            if secrets.compare_digest(state, entry.get("state", "")):
                matched_flow_name = flow_name
                break

    # TEMPORARY diagnostic (see module-level note near app = FastAPI(...)):
    # booleans/hostnames/flow-names only - never the actual code/state value.
    _oauth_log.info(
        "oauth callback: path=%s request_host=%s:%s code_present=%s state_present=%s "
        "pending_flow_names=%s matched_flow=%s configured_redirect_uri=%s",
        request.url.path, request.url.hostname, request.url.port,
        bool(code), bool(state), sorted(pending.keys()), matched_flow_name, config.GOOGLE_REDIRECT_URI,
    )

    if not code or not state or matched_flow_name is None:
        raise HTTPException(status_code=400, detail="Invalid OAuth state or missing code")

    matched = pending.pop(matched_flow_name)
    request.session["oauth_pending"] = pending
    scopes = matched["scopes"]

    creds = google_oauth.exchange_code_for_credentials(code, scopes=scopes)
    gmail_address = google_oauth.get_gmail_address(creds)

    db = get_session()
    try:
        google_oauth.save_credentials(db, creds, gmail_address)
    finally:
        db.close()
    return RedirectResponse("/")


@app.post("/auth/disconnect")
def auth_disconnect():
    db = get_session()
    try:
        google_oauth.revoke_and_forget(db)
    finally:
        db.close()
    return RedirectResponse("/", status_code=303)


@app.post("/scan")
def run_scan(max_messages: int = Form(600)):
    db = get_session()
    try:
        creds = google_oauth.load_credentials(db)
        if creds is None:
            raise HTTPException(status_code=400, detail="Gmail is not connected")

        matches = list(scan_inbox(creds, max_results=max_messages))
        aggregated = aggregate(iter(matches))
        result = store(db, aggregated)
    finally:
        db.close()
    return RedirectResponse(
        f"/dashboard?scanned={len(matches)}&created={result['created']}&updated={result['updated']}",
        status_code=303,
    )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, status: str = "all", q: str = ""):
    db = get_session()
    try:
        query = db.query(Company)
        if status in ("pending", "confirmed", "rejected"):
            query = query.filter(Company.status == status)
        if q:
            like = f"%{q.lower()}%"
            query = query.filter(
                (Company.name.ilike(like)) | (Company.domain.ilike(like))
            )
        companies = query.order_by(Company.evidence_count.desc()).all()
        counts = _status_counts(db)
        send_enabled = google_oauth.has_send_scope(db)
        response_tracking_enabled = google_oauth.has_readonly_scope(db)
    finally:
        db.close()

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "companies": companies,
            "counts": counts,
            "status_filter": status,
            "query": q,
            "scanned": request.query_params.get("scanned"),
            "created": request.query_params.get("created"),
            "updated": request.query_params.get("updated"),
            "duplicate_id": request.query_params.get("duplicate"),
            "send_enabled": send_enabled,
            "response_tracking_enabled": response_tracking_enabled,
        },
    )


@app.post("/api/companies/{company_id}/confirm")
def confirm_company(company_id: int):
    return _set_status(company_id, "confirmed")


@app.post("/api/companies/{company_id}/reject")
def reject_company(company_id: int):
    return _set_status(company_id, "rejected")


@app.post("/api/companies/{company_id}/reset")
def reset_company(company_id: int):
    return _set_status(company_id, "pending")


def _set_status(company_id: int, status: str):
    db = get_session()
    try:
        company = db.get(Company, company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        company.status = status
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/api/companies/{company_id}/deletion/research")
def research_deletion_method(company_id: int):
    """'Research deletion method' - forces a fresh lookup for this one
    company: checks the cache, and if stale/missing, actually runs the
    research provider now. Synchronous (blocks this one request) since it's
    a single explicit user click on a single row - the automatic background
    path (deletion_queue.py) is what keeps scanning itself fast."""
    db = get_session()
    try:
        company = db.get(Company, company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        resolve_deletion_method(db, company, _research_provider, force=True)
    finally:
        db.close()
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/api/companies/{company_id}/deletion/preview")
def preview_deletion_email(company_id: int):
    """Returns the exact draft that would be sent for an EMAIL_REQUEST
    company, so the confirmation modal can show the real outgoing email
    (recipient/subject/body) before the user confirms - never just a
    one-line description of what will happen."""
    db = get_session()
    try:
        company = db.get(Company, company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        gmail_address = google_oauth.get_connected_address(db) or "your Gmail address"
        draft = deletion_engine.build_email_draft(company, gmail_address)
        return draft
    finally:
        db.close()


@app.post("/api/companies/{company_id}/deletion/execute")
def execute_company_deletion(company_id: int, force: bool = Form(False)):
    """The one 'Delete my data' action. What actually happens depends on the
    company's deletion_method - see deletion_engine.execute_deletion. If a
    request was already submitted/completed, this refuses to silently repeat
    it unless force=True (the UI re-confirms with the user first)."""
    db = get_session()
    try:
        company = db.get(Company, company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        try:
            deletion_engine.execute_deletion(db, company, force_resend=force)
        except deletion_engine.DuplicateRequestWarning:
            return RedirectResponse(f"/dashboard?duplicate={company_id}", status_code=303)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        db.close()
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/api/companies/{company_id}/deletion/check-response")
def check_company_response_now(company_id: int):
    """Manual 'Check for responses' - one explicit, on-demand check of this
    company's tracked thread, same logic the background worker uses.
    404s if response tracking isn't enabled or this company has no tracked
    thread, since there'd be nothing to check."""
    db = get_session()
    try:
        company = db.get(Company, company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        if not google_oauth.has_readonly_scope(db):
            raise HTTPException(status_code=400, detail="Response tracking is not enabled")
        if not company.deletion_thread_id:
            raise HTTPException(status_code=400, detail="No tracked email thread for this company")
        creds = google_oauth.load_credentials(db)
        gmail_address = google_oauth.get_connected_address(db)
        if creds is None or gmail_address is None:
            raise HTTPException(status_code=400, detail="Gmail is not connected")
        check_company_response(db, company, creds, gmail_address, _response_classifier)
    finally:
        db.close()
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/api/companies/{company_id}/deletion/mark-completed")
def mark_deletion_completed(company_id: int, evidence_note: str = Form("")):
    """Self-report path for WEB_FORM/ACCOUNT_SETTING/PRIVACY_PORTAL and any
    EMAIL_REQUEST the user sent themselves: the user completed the company's
    own process outside Cookie Monster and is telling us so. Recorded as
    COMPLETED (self-reported), never as SUBMITTED - see deletion_engine.py."""
    db = get_session()
    try:
        company = db.get(Company, company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        deletion_engine.mark_user_completed(db, company, evidence_note)
    finally:
        db.close()
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/api/companies/{company_id}/correct")
def correct_company(company_id: int, name: str = Form(...), relationship_type: str = Form(...)):
    db = get_session()
    try:
        company = db.get(Company, company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        company.name = name.strip() or company.name
        company.relationship_type = relationship_type
        company.user_corrected = True
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/api/companies/merge")
def merge_companies(keep_id: int = Form(...), merge_id: int = Form(...)):
    if keep_id == merge_id:
        raise HTTPException(status_code=400, detail="Cannot merge a company into itself")
    db = get_session()
    try:
        keep = db.get(Company, keep_id)
        merge = db.get(Company, merge_id)
        if keep is None or merge is None:
            raise HTTPException(status_code=404, detail="Company not found")

        keep.evidence_count += merge.evidence_count
        keep.evidence_types = sorted(set(keep.evidence_types) | set(merge.evidence_types))
        keep.example_subjects = (keep.example_subjects + merge.example_subjects)[:3]
        keep.detection_reasons = (keep.detection_reasons + merge.detection_reasons)[:5]
        keep.first_seen = min(keep.first_seen, merge.first_seen)
        keep.last_seen = max(keep.last_seen, merge.last_seen)
        db.delete(merge)
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/dashboard", status_code=303)


@app.post("/api/delete-all")
def delete_all():
    """Wipes all discovered/imported data. Does not disconnect Gmail -
    use /auth/disconnect separately to revoke and delete the OAuth token too."""
    db = get_session()
    try:
        db.query(Company).delete()
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/dashboard", status_code=303)
