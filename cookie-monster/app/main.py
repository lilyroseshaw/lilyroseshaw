import secrets

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import config, deletion_engine, google_oauth
from app.aggregator import aggregate, store
from app.db import get_session, init_db
from app.deletion_resolver import resolve_deletion_method
from app.gmail_scan import scan_inbox
from app.models import Company

app = FastAPI(title="Cookie Monster")
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

GMAIL_SCOPE_EXPLANATION = (
    "Read-only access to Gmail message metadata only (sender, subject, date, labels). "
    "Cookie Monster never requests permission to read message bodies/attachments, "
    "send mail, or modify/delete/label anything in your inbox."
)


@app.on_event("startup")
def on_startup():
    init_db()


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
        },
    )


@app.get("/auth/login")
def auth_login(request: Request):
    try:
        auth_url, state = google_oauth.get_authorization_url()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    request.session["oauth_state"] = state
    request.session["oauth_scopes"] = config.GMAIL_SCOPES
    return RedirectResponse(auth_url)


@app.get("/auth/enable-sending")
def auth_enable_sending(request: Request):
    """SEPARATE, explicit consent step for the optional 'send deletion
    request emails automatically' feature. Never reached from the normal
    connect/scan flow - only from its own clearly-labeled button, after the
    user has read what gmail.send additionally grants."""
    try:
        auth_url, state = google_oauth.get_send_authorization_url()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    request.session["oauth_state"] = state
    request.session["oauth_scopes"] = [*config.GMAIL_SCOPES, config.GMAIL_SEND_SCOPE]
    return RedirectResponse(auth_url)


@app.get("/auth/callback")
def auth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        return RedirectResponse(f"/?error={error}")
    expected_state = request.session.pop("oauth_state", None)
    scopes = request.session.pop("oauth_scopes", config.GMAIL_SCOPES)
    if not code or not state or not expected_state or not secrets.compare_digest(state, expected_state):
        raise HTTPException(status_code=400, detail="Invalid OAuth state or missing code")

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
    """'Research deletion method' - re-checks the verified registry (only
    the registry; never guesses). Cheap/instant, safe to click repeatedly."""
    db = get_session()
    try:
        company = db.get(Company, company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        resolve_deletion_method(company, force=True)
        db.commit()
    finally:
        db.close()
    return RedirectResponse("/dashboard", status_code=303)


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
        deletion_engine.mark_user_completed(company, evidence_note)
        db.commit()
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
