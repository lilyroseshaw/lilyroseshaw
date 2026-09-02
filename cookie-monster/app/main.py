import datetime
import logging
import secrets
from urllib.parse import urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from googleapiclient.errors import HttpError
from starlette.middleware.sessions import SessionMiddleware

from app import config, deletion_engine, google_oauth
from app.aggregator import aggregate, store
from app.db import get_session, init_db
from app.deletion_constants import (
    DeletionMethod,
    DeletionStatus,
    EventSource,
    EventType,
    ExecutionCapability,
    RecipeStatus,
    ResearchFailureReason,
)
from app.deletion_events import record_event
from app.deletion_queue import start_background_worker
from app.deletion_research import build_default_provider
from app.deletion_resolver import backfill_all_companies, recover_stuck_method_lookup, resolve_deletion_method
from app.deletion_response_tracker import check_company_response
from app.gmail_scan import scan_inbox
from app.models import Company, DeletionEvent, DeletionRecipe
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


# Same reasoning as _oauth_log above: uvicorn's default logging setup
# swallows a plain INFO-level logger, and knowing whether the startup
# backfill/recovery actually did anything was an explicit gap raised in
# the recipe-verification investigation - so this gets its own handler too.
_startup_log = logging.getLogger("cookie_monster.startup")
_startup_log.setLevel(logging.INFO)
if not _startup_log.handlers:
    _startup_handler = logging.StreamHandler()
    _startup_handler.setFormatter(logging.Formatter("%(asctime)s [cookie-monster-startup] %(message)s"))
    _startup_log.addHandler(_startup_handler)
    _startup_log.propagate = False


@app.on_event("startup")
def on_startup():
    init_db()
    db = get_session()
    try:
        # Ensures every existing company - not just ones a scan happens to
        # touch - has at least a recipe stub and becomes visible to the
        # background research worker; then clears any company left showing
        # "Researching..." from a process that was killed mid-attempt on a
        # previous run. Both are idempotent and safe to run on every startup.
        backfilled = backfill_all_companies(db)
        recovered = recover_stuck_method_lookup(db)
        interrupted = deletion_engine.recover_stuck_submitting(db)
        if backfilled or recovered:
            _startup_log.info(
                "startup: ensured recipe coverage for %d compan(y/ies), recovered %d stuck in METHOD_LOOKUP",
                backfilled, recovered,
            )
        if interrupted:
            _startup_log.info(
                "startup: found %d compan(y/ies) with an interrupted deletion-email send from a previous "
                "run - moved to USER_ACTION_REQUIRED for manual review (never auto-resent)",
                interrupted,
            )
    finally:
        db.close()
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


# Short, safe categories only (see ResearchFailureReason) - never the raw
# exception text kept in a DeletionEvent's "detail" field for the audit
# trail. Deliberately not shown at all on the dashboard.
_FAILURE_REASON_LABELS = {
    ResearchFailureReason.NO_OFFICIAL_SOURCE_FOUND: "Couldn't find an official deletion/privacy page on their site",
    ResearchFailureReason.TECHNICAL_ERROR: "A technical error interrupted the last attempt",
    # Deliberately NOT phrased as "no deletion method exists" - a source
    # was found, on the company's own domain, we just couldn't confirm it
    # ourselves. See product decision: never imply a company doesn't
    # support deletion just because automatic verification didn't succeed.
    ResearchFailureReason.SOURCE_BLOCKED: "Possible official privacy route found — automatic verification blocked",
}


def _research_info_for_companies(db, companies: list[Company]) -> dict[int, dict]:
    """Per-company research metadata for the dashboard's UNKNOWN/
    NO_METHOD_FOUND states (attempt count, last/next attempt time, a short
    safe failure-reason label) - looked up from the shared DeletionRecipe
    (by domain) and, only for companies actually in one of those two
    statuses, the most recent RESEARCH_FAILED event for a reason label."""
    relevant = [c for c in companies if c.deletion_status in (DeletionStatus.UNKNOWN, DeletionStatus.NO_METHOD_FOUND)]
    if not relevant:
        return {}

    domains = {c.domain for c in relevant}
    recipes_by_domain = {
        r.domain: r for r in db.query(DeletionRecipe).filter(DeletionRecipe.domain.in_(domains)).all()
    }

    info: dict[int, dict] = {}
    for company in relevant:
        recipe = recipes_by_domain.get(company.domain)
        if recipe is None:
            continue
        next_retry_at = None
        if recipe.last_attempted_at and recipe.status != RecipeStatus.VERIFIED:
            next_retry_at = recipe.last_attempted_at + datetime.timedelta(days=config.DELETION_RECIPE_RETRY_COOLDOWN_DAYS)

        failure_reason_label = None
        blocked_url = None
        last_event = (
            db.query(DeletionEvent)
            .filter(DeletionEvent.company_id == company.id, DeletionEvent.event_type == EventType.RESEARCH_FAILED)
            .order_by(DeletionEvent.occurred_at.desc())
            .first()
        )
        if last_event:
            evidence = last_event.evidence or {}
            reason = evidence.get("reason")
            failure_reason_label = _FAILURE_REASON_LABELS.get(reason)
            # A manual-review lead - the specific URL Cookie Monster found
            # but couldn't independently confirm - never a body/secret,
            # just the URL itself.
            blocked_url = evidence.get("blocked_url") or evidence.get("unverified_lead_url")

        info[company.id] = {
            "attempts": recipe.research_attempts,
            "last_attempted_at": recipe.last_attempted_at,
            "next_retry_at": next_retry_at,
            "failure_reason_label": failure_reason_label,
            "blocked_url": blocked_url,
        }
    return info


_EXECUTION_ACTION_TEXT = {
    (ExecutionCapability.AUTO_EXECUTABLE, DeletionMethod.EMAIL_REQUEST):
        "Cookie Monster will send the email below from your connected Gmail address.",
    (ExecutionCapability.USER_STEP_REQUIRED, DeletionMethod.EMAIL_REQUEST):
        "Cookie Monster will prepare the email below for you to send yourself.",
}


def _execution_plans_for_companies(db, companies: list[Company]) -> dict[int, dict]:
    """Per-company execution plan for the dashboard's READY/FAILED states
    (the only ones that show the 'Delete my data' approval button) - the
    SAME classify_execution_capability() the actual execute endpoint uses,
    so the approval modal can never promise something execution won't
    actually do. Kept out of the template's own logic on purpose - see
    deletion_engine.py's module docstring."""
    relevant = [c for c in companies if c.deletion_status in (DeletionStatus.READY, DeletionStatus.FAILED)]
    plans: dict[int, dict] = {}
    for company in relevant:
        if not company.deletion_verified:
            continue
        plan = deletion_engine.classify_execution_capability(db, company)
        action_text = _EXECUTION_ACTION_TEXT.get(
            (plan.capability, plan.method),
            f"Cookie Monster will take you to {company.name}'s official deletion page. "
            "Completing the request there is up to you - Cookie Monster can't do it on your behalf.",
        )
        plans[company.id] = {
            "capability": plan.capability,
            "reason": plan.reason,
            "consequences": plan.consequences,
            "action_text": action_text,
            "missing_identity_fields": plan.missing_identity_fields,
        }
    return plans


def _dashboard_context(request: Request, status: str, q: str, **extra) -> dict:
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
        research_info = _research_info_for_companies(db, companies)
        execution_plans = _execution_plans_for_companies(db, companies)
    finally:
        db.close()

    context = {
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
        "research_info": research_info,
        "execution_plans": execution_plans,
        "threshold": config.DELETION_RECIPE_FAILURE_THRESHOLD,
        "attach_preview": None,
        "attach_preview_error": None,
    }
    context.update(extra)
    return context


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, status: str = "all", q: str = ""):
    return templates.TemplateResponse("dashboard.html", _dashboard_context(request, status, q))


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
    """Returns the FULL execution plan for the approval modal - not just
    the exact outgoing email (recipient/subject/body) for EMAIL_REQUEST,
    but also the execution capability (AUTO_EXECUTABLE/USER_STEP_REQUIRED/
    MANUAL_HANDOFF) and why, computed by the exact same
    classify_execution_capability() the execute endpoint itself uses - so
    this preview can never promise something execution won't actually do."""
    db = get_session()
    try:
        company = db.get(Company, company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        if not company.deletion_verified:
            raise HTTPException(status_code=400, detail="No verified deletion recipe for this company.")
        plan = deletion_engine.classify_execution_capability(db, company)
        response = {
            "capability": plan.capability,
            "reason": plan.reason,
            "consequences": plan.consequences,
            "missing_identity_fields": plan.missing_identity_fields,
        }
        if plan.draft:
            response.update(plan.draft)  # to / subject / body, at the top level for existing callers
        return response
    finally:
        db.close()


@app.post("/api/companies/{company_id}/deletion/execute")
def execute_company_deletion(company_id: int, force: bool = Form(False)):
    """The one 'Delete my data' action. What actually happens depends on the
    company's deletion_method AND its current execution capability - see
    deletion_engine.execute_deletion. If a request was already submitted/
    completed, this refuses to silently repeat it unless force=True (the UI
    re-confirms with the user first). A concurrent/duplicate approval for
    the SAME company (double-click, two tabs) is a silent no-op, not an
    error - the in-flight attempt resolves on its own shortly."""
    db = get_session()
    try:
        company = db.get(Company, company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        try:
            deletion_engine.execute_deletion(db, company, force_resend=force)
        except deletion_engine.DuplicateRequestWarning:
            return RedirectResponse(f"/dashboard?duplicate={company_id}", status_code=303)
        except deletion_engine.ExecutionInFlightError:
            return RedirectResponse("/dashboard", status_code=303)
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


def _check_attach_eligible(db, company: Company) -> str | None:
    """Returns an error message if this company isn't eligible for manual
    thread attachment, else None. Checked before BOTH the preview and the
    confirm step - state can change between the two requests (another tab,
    a background check completing first), so confirm must never trust that
    what was true at preview time is still true now."""
    if not google_oauth.has_readonly_scope(db):
        return "Response tracking is not enabled - enable it first to attach a confirmation email."
    if company.deletion_thread_id:
        return "This company already has a tracked email thread."
    if company.deletion_status in DeletionStatus.TERMINAL:
        return "This request is already resolved - there's nothing left to track."
    return None


@app.post("/api/companies/{company_id}/deletion/attach-thread/preview")
def preview_attach_thread(request: Request, company_id: int, gmail_ref: str = Form(...)):
    """Step 1 of manually attaching a Gmail confirmation email to a
    deletion request Cookie Monster did NOT itself send (e.g. one
    submitted through a company's own web form or account settings - see
    TODO.md's "Attach confirmation email" design notes). Resolves ONLY the
    one specific message the user pasted a link/ID for - never a search or
    listing - and shows just its From/Subject/Date for the user to
    recognize before anything is saved. Re-rendered inline on the
    dashboard rather than redirected, so the (potentially personal) email
    subject line never ends up in a URL/browser history/server access log."""
    db = get_session()
    try:
        company = db.get(Company, company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        error = _check_attach_eligible(db, company)
        preview = None
        if error is None:
            try:
                message_id = google_oauth.parse_gmail_message_ref(gmail_ref)
                creds = google_oauth.load_credentials(db)
                if creds is None:
                    error = "Gmail is not connected."
                else:
                    resolved = google_oauth.fetch_message_preview(creds, message_id)
                    preview = {"company_id": company_id, **resolved}
            except ValueError as exc:
                error = str(exc)
            except HttpError as exc:
                status_code = getattr(getattr(exc, "resp", None), "status", None)
                error = (
                    "That message couldn't be found in your Gmail account - double-check the link/ID."
                    if status_code == 404
                    else "Couldn't look up that message right now - please try again."
                )
        context = _dashboard_context(
            request, "all", "",
            attach_preview=preview,
            attach_preview_error={"company_id": company_id, "message": error} if error else None,
        )
    finally:
        db.close()
    return templates.TemplateResponse("dashboard.html", context)


@app.post("/api/companies/{company_id}/deletion/attach-thread/confirm")
def confirm_attach_thread(company_id: int, message_id: str = Form(...)):
    """Step 2: the explicit second confirmation. Re-validates eligibility
    and re-resolves the EXACT SAME message shown at preview time, by its
    message ID (not its thread ID - a thread's first message can have
    different From/Subject/Date than the message the user actually
    reviewed) - never trusts client-supplied from/subject/date, the server
    is the sole source of truth for what gets written to the audit trail.
    This ONLY sets deletion_thread_id and records the association - it
    never touches deletion_status, deletion_evidence, or any other field,
    so the original submission (e.g. a user-reported manual request) stays
    historically exactly as it was. From here on, the existing response
    tracker (deletion_response_tracker.py, unchanged) owns this thread."""
    db = get_session()
    try:
        company = db.get(Company, company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        error = _check_attach_eligible(db, company)
        if error:
            raise HTTPException(status_code=400, detail=error)

        creds = google_oauth.load_credentials(db)
        if creds is None:
            raise HTTPException(status_code=400, detail="Gmail is not connected.")
        try:
            resolved = google_oauth.fetch_message_preview(creds, message_id)
        except HttpError as exc:
            status_code = getattr(getattr(exc, "resp", None), "status", None)
            detail = (
                "That message couldn't be found in your Gmail account - double-check the link/ID."
                if status_code == 404
                else "Couldn't look up that message right now - please try again."
            )
            raise HTTPException(status_code=400, detail=detail) from exc

        company.deletion_thread_id = resolved["thread_id"]
        record_event(
            db, company.id, EventType.THREAD_ASSOCIATED, source=EventSource.USER,
            evidence={
                "message_id": resolved["message_id"],
                "thread_id": resolved["thread_id"],
                "from": resolved["from"],
                "subject": resolved["subject"],
                "date": resolved["date"],
            },
        )
        db.commit()
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
