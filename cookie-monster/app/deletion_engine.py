"""Executes (or routes) a deletion request for a confirmed company.

What "execute" means depends on deletion_method:

- EMAIL_REQUEST: always drafts the request. Sends it for real, via the
  user's own Gmail, ONLY if they've completed the separate gmail.send
  consent (google_oauth.has_send_scope) - otherwise returns the draft for
  the user to send themselves and sets USER_ACTION_REQUIRED.
- WEB_FORM / PRIVACY_PORTAL / ACCOUNT_SETTING: never automated here (see
  README "Deletion automation - what's real" for why: these almost always
  sit behind login/CAPTCHA/email verification, and automating around that
  is exactly what this project's own safety rules forbid). Returns the
  official URL and sets USER_ACTION_REQUIRED; the user completes it and
  self-reports back via mark_user_completed().
- API: wired for completeness; inert until a recipe actually sets
  method=API with a real, documented endpoint (none do yet).
- UNKNOWN: never submits anything.

Nothing here ever marks DeletionStatus.SUBMITTED without concrete evidence
(a Gmail message id, an HTTP response, etc.) - see DeletionStatus.SYSTEM_VERIFIED.
Every transition is also recorded in the deletion_events audit log
(app/deletion_events.py), not just the current status column.
"""
import datetime

from sqlalchemy.orm import Session

from app import google_oauth
from app.deletion_constants import DeletionMethod, DeletionStatus, EventSource, EventType
from app.deletion_events import record_event
from app.models import Company, OAuthToken

ALREADY_DONE_STATUSES = {DeletionStatus.SUBMITTED, DeletionStatus.COMPLETED}


class DuplicateRequestWarning(Exception):
    """Raised instead of executing again when a request already has a
    terminal, evidenced outcome - the caller should surface this as a
    confirmation prompt, not silently resend."""

    def __init__(self, company: Company):
        self.company = company
        super().__init__(
            f"A deletion request for {company.name} was already marked "
            f"{company.deletion_status} on "
            f"{(company.deletion_completed_at or company.deletion_requested_at)}."
        )


def build_email_draft(company: Company, user_email: str) -> dict:
    to_email = company.deletion_email or f"privacy@{company.domain}"
    subject = f"CCPA/CPRA Deletion Request - {company.name}"
    body = (
        f"To the Privacy Team at {company.name},\n\n"
        "I am requesting deletion of my personal information pursuant to "
        "applicable California privacy law (CCPA/CPRA).\n\n"
        "Please confirm receipt of this request and provide instructions "
        "for completing any required identity verification.\n\n"
        f"This request relates to the account/contact associated with: {user_email}\n\n"
        "Thank you."
    )
    return {"to": to_email, "subject": subject, "body": body}


def execute_deletion(db: Session, company: Company, force_resend: bool = False) -> Company:
    """Runs (or routes) one deletion request. Raises DuplicateRequestWarning
    if the company already has a terminal, evidenced outcome and
    force_resend is False - the caller should re-confirm with the user
    before passing force_resend=True."""
    if company.status != "confirmed":
        raise ValueError("Only confirmed companies can have a deletion request executed")

    if company.deletion_status in ALREADY_DONE_STATUSES and not force_resend:
        raise DuplicateRequestWarning(company)

    now = datetime.datetime.utcnow()
    record_event(
        db, company.id, EventType.USER_CONFIRMED, source=EventSource.USER,
        evidence={"method": company.deletion_method, "forced_resend": force_resend},
    )

    if company.deletion_method == DeletionMethod.EMAIL_REQUEST:
        _execute_email_request(db, company, now)
    elif company.deletion_method in (
        DeletionMethod.WEB_FORM,
        DeletionMethod.PRIVACY_PORTAL,
        DeletionMethod.ACCOUNT_SETTING,
        DeletionMethod.MANUAL,
    ):
        _route_to_user_action(db, company, now)
    elif company.deletion_method == DeletionMethod.API:
        # No recipe sets method=API with a real endpoint yet - this branch
        # exists so the state machine is complete, but is unreachable in
        # practice today. Never fabricate an API call.
        company.deletion_status = DeletionStatus.FAILED
        company.deletion_error = "No supported API integration exists for this company yet."
        record_event(db, company.id, EventType.FAILED, evidence={"reason": company.deletion_error})
    else:
        company.deletion_status = DeletionStatus.UNKNOWN
        company.deletion_error = "Deletion method not verified yet."

    db.commit()
    return company


def _execute_email_request(db: Session, company: Company, now: datetime.datetime) -> None:
    token_row = db.query(OAuthToken).first()
    user_email = token_row.gmail_address if token_row else "your Gmail address"

    draft = build_email_draft(company, user_email)

    if not google_oauth.has_send_scope(db):
        company.deletion_status = DeletionStatus.USER_ACTION_REQUIRED
        company.deletion_url = None
        company.deletion_instructions = (
            f"Send this request yourself to {draft['to']} (Cookie Monster hasn't been "
            "authorized to send email - see 'Enable automatic sending' if you want that)."
        )
        company.deletion_evidence = {
            "type": "draft_only",
            "to": draft["to"],
            "subject": draft["subject"],
            "prepared_at": now.isoformat(),
        }
        return

    try:
        creds = google_oauth.load_credentials(db)
        if creds is None:
            raise RuntimeError("Gmail is not connected")
        response = google_oauth.send_email(creds, draft["to"], draft["subject"], draft["body"])
    except Exception as exc:  # noqa: BLE001 - any send failure lands here, never silently
        company.deletion_status = DeletionStatus.FAILED
        company.deletion_error = f"Failed to send deletion request email: {exc}"
        record_event(db, company.id, EventType.FAILED, evidence={"reason": str(exc)[:300]})
        return

    message_id = response.get("id")
    thread_id = response.get("threadId")

    company.deletion_status = DeletionStatus.SUBMITTED
    company.deletion_requested_at = now
    company.deletion_error = None
    company.deletion_thread_id = thread_id
    company.deletion_evidence = {
        "type": "gmail_send",
        "gmail_message_id": message_id,
        "gmail_thread_id": thread_id,
        "sent_to": draft["to"],
        "subject": draft["subject"],
        "sent_at": now.isoformat(),
    }
    record_event(
        db, company.id, EventType.EMAIL_SENT,
        evidence={"gmail_message_id": message_id, "gmail_thread_id": thread_id, "sent_to": draft["to"]},
    )


def _route_to_user_action(db: Session, company: Company, now: datetime.datetime) -> None:
    if not company.deletion_url:
        company.deletion_status = DeletionStatus.UNKNOWN
        company.deletion_error = "No verified deletion page is on file for this company."
        return
    company.deletion_status = DeletionStatus.USER_ACTION_REQUIRED
    if company.deletion_requested_at is None:
        company.deletion_requested_at = now
    record_event(db, company.id, EventType.PORTAL_OPENED, evidence={"url": company.deletion_url})


def mark_user_completed(db: Session, company: Company, evidence_note: str | None = None) -> Company:
    """Self-report path: the user did the WEB_FORM/ACCOUNT_SETTING/manual-email
    step themselves outside Cookie Monster and is telling us it's done. This
    is recorded as COMPLETED (self-reported), never as SUBMITTED - Cookie
    Monster has no independent proof, and this project's own rules require
    we never claim otherwise."""
    company.deletion_status = DeletionStatus.COMPLETED
    company.deletion_completed_at = datetime.datetime.utcnow()
    note = (evidence_note or "").strip()[:300] or None
    company.deletion_evidence = {
        "type": "user_reported",
        "note": note,
        "reported_at": company.deletion_completed_at.isoformat(),
    }
    record_event(
        db, company.id, EventType.USER_MARKED_COMPLETE, source=EventSource.USER,
        evidence={"note": note} if note else {},
    )
    db.commit()
    return company
