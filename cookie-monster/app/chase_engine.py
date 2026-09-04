"""The 24-hour chase: while an EMAIL_REQUEST deletion case is unresolved
and the ball is in the company's court, Baker's Dozen automatically sends
a deterministic follow-up in the SAME Gmail thread once a day, so the
user never has to remember to nag a company themselves.

Kept deliberately case-oriented rather than Gmail-oriented: every function
here takes a Company (today's stand-in for a future PrivacyCase) and only
reaches for google_oauth.send_reply_email at the one point an email
actually needs to go out - so a future Outlook/other-channel adapter only
has to replace that one call, not this module's scheduling/state logic.

Safety invariants, same philosophy as deletion_engine.py and
deletion_response_tracker.py:
- Never sent without the user having authorized automatic follow-ups (via
  approving the original request - see deletion_engine.py) - this module
  itself sends no email a user hasn't already covered by that consent.
- Deterministic templates only. No LLM, ever.
- A follow-up is only ever recorded as sent after Gmail returns evidence
  of a successful send (see send_followup / reconcile_ambiguous_followup).
- next_followup_at only ever advances on a REAL event (initial send, a
  confirmed follow-up send, or the user completing a required action) -
  never merely because the company sent another acknowledgment while
  already in COMPANY's court with a follow-up already scheduled.
"""
import datetime
import logging
import re

from google.oauth2.credentials import Credentials
from sqlalchemy.orm import Session

from app import config, google_oauth
from app.deletion_constants import DeletionStatus, EventType, WaitingOn
from app.deletion_events import record_event
from app.models import Company, MailMessage
from app.response_classify import MORE_INFO_REQUIRED_PATTERNS, VERIFICATION_NEEDED_PATTERNS

_log = logging.getLogger("cookie_monster.chase_engine")
_log.setLevel(logging.INFO)
if not _log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [cookie-monster-chase] %(message)s"))
    _log.addHandler(_handler)
    _log.propagate = False


# --- waiting_on derivation (state model) ---

def _unknown_response_needs_user(body_text: str) -> bool:
    """UNKNOWN_RESPONSE didn't cleanly match a top-level category, but may
    still contain a clear human-action signal the classifier's own
    VERIFICATION_NEEDED/MORE_INFO_REQUIRED patterns would recognize.
    Deterministic, regex-only - reuses the SAME high-precision patterns
    the primary classifier already trusts, never a new guess."""
    text = body_text or ""
    return any(
        re.search(p, text, re.IGNORECASE) for p in (*VERIFICATION_NEEDED_PATTERNS, *MORE_INFO_REQUIRED_PATTERNS)
    )


def derive_waiting_on(classification_status: str, body_text: str) -> str | None:
    """The one place DeletionStatus (plus, for the uncertain case, a
    narrow textual signal) maps onto WaitingOn. Never guesses: anything
    not explicitly covered here returns None (not an active chase case)."""
    if classification_status in (DeletionStatus.VERIFICATION_NEEDED, DeletionStatus.MORE_INFO_REQUIRED):
        return WaitingOn.USER
    if classification_status in (
        DeletionStatus.IN_PROGRESS, DeletionStatus.SUBMITTED, DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED,
    ):
        return WaitingOn.COMPANY
    if classification_status == DeletionStatus.UNKNOWN_RESPONSE:
        return WaitingOn.USER if _unknown_response_needs_user(body_text) else WaitingOn.COMPANY
    if classification_status == DeletionStatus.REJECTED:
        return WaitingOn.ESCALATION_NEEDED
    # COMPLETED, FAILED, or anything else - resolved/terminal, not chased.
    return None


def _is_tracked_email_case(company: Company) -> bool:
    """Structural gate only (method + tracked thread) - deliberately NOT
    conditioned on deletion_status, because on_reply_classified is called
    AFTER deletion_status has already been set to the new (possibly
    terminal - COMPLETED/REJECTED) value. Gating on TERMINAL there would
    stop chase-state derivation from ever running for the very
    transitions that need it most: clearing waiting_on/next_followup_at
    on COMPLETED, or routing REJECTED into ESCALATION_NEEDED."""
    return company.deletion_method == "EMAIL_REQUEST" and bool(company.deletion_thread_id)


def is_chase_eligible(company: Company) -> bool:
    """Only EMAIL_REQUEST cases Baker's Dozen itself sent, not already in
    a terminal state - used to decide whether to START/CONTINUE a chase
    (on_request_sent, on_user_action_completed). See
    _is_tracked_email_case for the status-independent structural check
    on_reply_classified needs instead."""
    return _is_tracked_email_case(company) and company.deletion_status not in DeletionStatus.TERMINAL


def _followup_interval() -> datetime.timedelta:
    return datetime.timedelta(hours=config.FOLLOWUP_INTERVAL_HOURS)


# --- state transitions (called from the send/response/attestation paths) ---

def on_request_sent(company: Company, sent_at: datetime.datetime) -> None:
    """The initial EMAIL_REQUEST send succeeded - starts a fresh chase."""
    if not is_chase_eligible(company):
        return
    company.waiting_on = WaitingOn.COMPANY
    company.next_followup_at = sent_at + _followup_interval()
    company.followup_attempt = 0
    company.last_followup_at = None
    company.followup_locked_at = None
    company.followups_paused = False


def on_reply_classified(company: Company, classification_status: str, body_text: str, occurred_at: datetime.datetime) -> None:
    """Called for every newly-classified inbound message, alongside (not
    instead of) the existing DeletionStatus update in
    deletion_response_tracker.py. A generic company acknowledgment that
    arrives while already COMPANY with a follow-up already scheduled must
    NEVER push next_followup_at further out - only a fresh entry into
    COMPANY's court (from USER/ESCALATION_NEEDED/unscheduled) gets a new
    24h window."""
    if not _is_tracked_email_case(company):
        return
    previous_waiting_on = company.waiting_on
    new_waiting_on = derive_waiting_on(classification_status, body_text)
    company.waiting_on = new_waiting_on
    if new_waiting_on == WaitingOn.COMPANY:
        if previous_waiting_on != WaitingOn.COMPANY or company.next_followup_at is None:
            company.next_followup_at = occurred_at + _followup_interval()
        # else: unchanged on purpose - see module docstring.
    else:
        company.next_followup_at = None


def on_user_action_completed(company: Company, completed_at: datetime.datetime) -> None:
    """The user finished something only they could do (attestation, or an
    approved mailbox reply actually sent) - responsibility passes back to
    the company with a fresh 24h window, always (unlike a mere company
    acknowledgment, this always resets the clock, per product decision)."""
    if not is_chase_eligible(company):
        return
    company.waiting_on = WaitingOn.COMPANY
    company.next_followup_at = completed_at + _followup_interval()


def pause_followups(company: Company) -> None:
    company.followups_paused = True


def resume_followups(company: Company, now: datetime.datetime) -> None:
    company.followups_paused = False
    if company.waiting_on == WaitingOn.COMPANY and company.next_followup_at is None:
        company.next_followup_at = now + _followup_interval()


# --- deterministic follow-up templates (no LLM, ever) ---

def _followup_template(company: Company, to_email: str, subject: str, attempt: int) -> dict:
    date_str = company.deletion_requested_at.strftime("%b %d, %Y") if company.deletion_requested_at else "recently"
    days_elapsed = (
        (datetime.datetime.utcnow() - company.deletion_requested_at).days
        if company.deletion_requested_at else 0
    )
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    if company.deletion_status == DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED:
        # Distinct from the generic tiers below: the open question here is
        # never "how much longer" - it's "you confirmed the ACCOUNT, but
        # not the DATA" - so this stays the same regardless of attempt
        # count, deterministic and specific every time, never treating a
        # generic security assurance ("protected", "handled securely",
        # "not shared publicly") as an answer to the actual question.
        body = (
            f"Thank you for confirming that the account associated with my data deletion "
            f"request (sent on {date_str}) has been deactivated/closed.\n\n"
            "However, my original request was for the deletion of my PERSONAL INFORMATION, "
            "not only closure of the account - and closing the account does not by itself "
            "confirm that my personal information was deleted.\n\n"
            "Could you please confirm whether my personal information has been deleted? If "
            "any of my information is still being retained, please specify which categories "
            "of information are being retained, the reason each is being kept, and how long "
            "it will be retained, if known.\n\n"
            "Thank you."
        )
        return {"to": to_email, "subject": subject, "body": body}

    if attempt <= 1:
        body = (
            f"I'm following up on my data deletion request sent on {date_str}, "
            "which has not yet been resolved.\n\n"
            "Please confirm when this request will be completed, or let me know "
            "if you need anything further from me.\n\n"
            "Thank you."
        )
    elif attempt == 2:
        body = (
            f"This is a follow-up on my data deletion request, originally sent on "
            f"{date_str} ({days_elapsed} days ago). I have not yet received "
            "confirmation that it has been completed.\n\n"
            "Please provide an update on the status of this request.\n\n"
            "Thank you."
        )
    else:
        body = (
            "This is another follow-up regarding my unresolved data deletion "
            f"request, originally sent on {date_str}. I still have not received "
            "confirmation that my personal data has been deleted.\n\n"
            "Please confirm the status of the deletion request and, if it "
            "remains pending, provide the expected completion date.\n\n"
            "Thank you."
        )
    return {"to": to_email, "subject": subject, "body": body}


def _latest_inbound_message(db: Session, company_id: int) -> MailMessage | None:
    return (
        db.query(MailMessage)
        .filter(MailMessage.company_id == company_id, MailMessage.direction == "inbound")
        .order_by(MailMessage.occurred_at.desc())
        .first()
    )


def _reply_to_address(latest_inbound: MailMessage | None, company: Company) -> str:
    import email.utils
    if latest_inbound is not None:
        addr = email.utils.parseaddr(latest_inbound.from_display or "")[1]
        if addr:
            return addr
    return company.deletion_email or f"privacy@{company.domain}"


def _original_subject(db: Session, company: Company) -> str:
    first_outbound = (
        db.query(MailMessage)
        .filter(MailMessage.company_id == company.id, MailMessage.direction == "outbound")
        .order_by(MailMessage.occurred_at.asc())
        .first()
    )
    if first_outbound and first_outbound.subject:
        return first_outbound.subject
    return f"CCPA/CPRA Deletion Request - {company.name}"


def _send_followup_email(creds: Credentials, to_email: str, subject: str, body: str, thread_id: str, in_reply_to: str | None) -> dict:
    """Thin wrapper, same pattern as mail.google_oauth_send_reply - kept
    as its own function so tests mock exactly this call, never the raw
    google_oauth module."""
    return google_oauth.send_reply_email(creds, to_email, subject, body, thread_id, in_reply_to=in_reply_to)


# --- sending + idempotency/crash protection ---

def get_companies_due_for_followup(db: Session, limit: int | None = None) -> list[Company]:
    limit = limit or config.FOLLOWUP_BATCH_SIZE
    now = datetime.datetime.utcnow()
    candidates = (
        db.query(Company)
        .filter(
            Company.waiting_on == WaitingOn.COMPANY,
            Company.followups_paused.is_(False),
            Company.next_followup_at.isnot(None),
            Company.next_followup_at <= now,
        )
        .order_by(Company.next_followup_at.asc())
        .limit(limit * 3)  # over-fetch; some may be locked/stale-but-unresolved
        .all()
    )
    due = []
    for c in candidates:
        if c.followup_locked_at is not None:
            continue  # still (or ambiguously) locked - reconcile_stale_followup_locks handles these separately
        due.append(c)
        if len(due) >= limit:
            break
    return due


def send_followup(db: Session, company: Company, creds: Credentials, gmail_address: str) -> bool:
    """Sends ONE due follow-up. Returns True if actually sent. Never
    raises - any failure is caught, recorded, and left for the next tick,
    exactly like check_company_response's own failure handling."""
    now = datetime.datetime.utcnow()
    company.followup_locked_at = now
    db.commit()  # the lock itself must be durable before the Gmail call

    latest_inbound = _latest_inbound_message(db, company.id)
    to_email = _reply_to_address(latest_inbound, company)
    subject = _original_subject(db, company)
    attempt = company.followup_attempt + 1
    draft = _followup_template(company, to_email, subject, attempt)

    _log.info("followup sending: company=%s attempt=%d thread=%s", company.id, attempt, company.deletion_thread_id)
    try:
        response = _send_followup_email(
            creds, draft["to"], draft["subject"], draft["body"],
            company.deletion_thread_id, latest_inbound.rfc822_message_id if latest_inbound else None,
        )
    except Exception as exc:  # noqa: BLE001 - any send failure, never silent
        company.followup_locked_at = None
        record_event(
            db, company.id, EventType.RESPONSE_CHECK_FAILED,
            evidence={"error": str(exc)[:200], "context": "followup_send"},
        )
        db.commit()
        _log.info("followup send failed: company=%s error=%s", company.id, str(exc)[:200])
        return False

    message_id = response.get("id", "")
    thread_id = response.get("threadId") or company.deletion_thread_id
    _record_followup_sent(db, company, message_id, thread_id, subject, draft["body"], attempt, now, recovered=False)
    db.commit()
    _log.info("followup sent: company=%s attempt=%d message=%s", company.id, attempt, message_id)
    return True


def _record_followup_sent(
    db: Session, company: Company, message_id: str, thread_id: str, subject: str, body: str,
    attempt: int, sent_at: datetime.datetime, recovered: bool,
) -> None:
    existing = db.query(MailMessage).filter(MailMessage.gmail_message_id == message_id).one_or_none()
    if existing is None:
        db.add(MailMessage(
            company_id=company.id, direction="outbound", gmail_message_id=message_id,
            gmail_thread_id=thread_id, occurred_at=sent_at, from_display="You",
            subject=subject, body_excerpt=body[:4000], read_at=sent_at,
        ))
    record_event(
        db, company.id, EventType.FOLLOWUP_SENT,
        evidence={"attempt": attempt, "gmail_message_id": message_id, "gmail_thread_id": thread_id, "recovered": recovered},
    )
    company.followup_attempt = attempt
    company.last_followup_at = sent_at
    company.next_followup_at = sent_at + _followup_interval()
    company.followup_locked_at = None


# --- ambiguous-send reconciliation ---

def reconcile_stale_followup_locks(db: Session, creds: Credentials, gmail_address: str, limit: int | None = None) -> int:
    """Finds cases whose followup_locked_at is old enough to be
    ambiguous (crash/hang during a previous send attempt) and resolves
    each against the ONE tracked Gmail thread - never a broader search.
    Returns how many were resolved (either way)."""
    limit = limit or config.FOLLOWUP_BATCH_SIZE
    stale_before = datetime.datetime.utcnow() - datetime.timedelta(minutes=config.FOLLOWUP_LOCK_STALE_MINUTES)
    stale = (
        db.query(Company)
        .filter(Company.followup_locked_at.isnot(None), Company.followup_locked_at <= stale_before)
        .order_by(Company.followup_locked_at.asc())
        .limit(limit)
        .all()
    )
    resolved = 0
    for company in stale:
        if _reconcile_one(db, company, creds, gmail_address):
            resolved += 1
    return resolved


def _reconcile_one(db: Session, company: Company, creds: Credentials, gmail_address: str) -> bool:
    lock_time = company.followup_locked_at
    try:
        messages = google_oauth.fetch_thread_messages(creds, company.deletion_thread_id)
    except Exception as exc:  # noqa: BLE001 - can't verify either way - leave the lock, try again later
        _log.info("followup reconcile: could not check thread for company=%s error=%s", company.id, str(exc)[:200])
        if lock_time and datetime.datetime.utcnow() - lock_time > datetime.timedelta(hours=config.FOLLOWUP_RECONCILE_MAX_AGE_HOURS):
            record_event(
                db, company.id, EventType.RESPONSE_CHECK_FAILED,
                evidence={"context": "followup_reconcile_unresolved", "locked_since": lock_time.isoformat()},
            )
            db.commit()
        return False

    known_ids = {
        m.gmail_message_id
        for m in db.query(MailMessage).filter(MailMessage.company_id == company.id).all()
    }
    buffer = datetime.timedelta(minutes=2)
    found = None
    for m in messages:
        if m.get("id") in known_ids:
            continue
        if "SENT" not in m.get("labelIds", []):
            continue
        try:
            sent_at = datetime.datetime.utcfromtimestamp(int(m.get("internalDate", "0")) / 1000)
        except (TypeError, ValueError):
            continue
        if sent_at >= lock_time - buffer:
            found = (m, sent_at)
            break

    if found:
        message, sent_at = found
        attempt = company.followup_attempt + 1
        subject = next(
            (h["value"] for h in message.get("payload", {}).get("headers", []) if h.get("name", "").lower() == "subject"),
            _original_subject(db, company),
        )
        _log.info("followup reconcile: recovered send for company=%s message=%s", company.id, message.get("id"))
        _record_followup_sent(
            db, company, message.get("id"), company.deletion_thread_id, subject, "",
            attempt, sent_at, recovered=True,
        )
        db.commit()
        return True

    # Not found - but a single negative check is NOT proof nothing was
    # sent. Gmail's API gives no documented guarantee that a just-accepted
    # send is immediately visible via threads().get() the instant it
    # returns 200; in the crash-after-Gmail-accepted-but-before-we-recorded
    # -it window, one early check could in principle be racing a brief
    # propagation lag rather than observing a genuine non-send. So the
    # lock is only cleared - authorizing a fresh (potentially duplicate-
    # risking) send - once the thread has shown NOTHING across the whole
    # FOLLOWUP_RECONCILE_CONFIRM_MINUTES window since the lock was first
    # set, not merely once past FOLLOWUP_LOCK_STALE_MINUTES. Until then,
    # the lock stays in place and this same check runs again next tick
    # (see reconcile_stale_followup_locks) - deferring, not resolving.
    confirmed_absent = datetime.datetime.utcnow() - lock_time >= datetime.timedelta(minutes=config.FOLLOWUP_RECONCILE_CONFIRM_MINUTES)
    if not confirmed_absent:
        _log.info(
            "followup reconcile: no evidence yet for company=%s - within the confirmation window, "
            "deferring rather than risking a duplicate send; will re-check next tick", company.id,
        )
        return False

    _log.info("followup reconcile: no evidence of a send for company=%s after confirmation window - clearing lock, will retry soon", company.id)
    company.followup_locked_at = None
    company.next_followup_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=config.FOLLOWUP_RECONCILE_RETRY_MINUTES)
    db.commit()
    return True


# --- batch entry point (called by deletion_queue.py) ---

def process_followups(db: Session, creds: Credentials, gmail_address: str, limit: int | None = None) -> int:
    """Reconciles any stale locks first (never send while a prior
    attempt's outcome is unknown), then sends any due, unlocked
    follow-ups. Returns how many were actually sent."""
    reconcile_stale_followup_locks(db, creds, gmail_address, limit)
    due = get_companies_due_for_followup(db, limit)
    sent = 0
    for company in due:
        if send_followup(db, company, creds, gmail_address):
            sent += 1
    return sent
