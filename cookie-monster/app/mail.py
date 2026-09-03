"""Baker's Dozen mailbox: presents a company's tracked deletion-request
correspondence as readable "letters" instead of raw Gmail, without ever
widening the privacy boundary deletion_response_tracker.py already
enforces.

Privacy boundary (unchanged, just reused): every MailMessage row this
module creates comes from a Gmail message deletion_response_tracker.py
ALREADY fetched via its one legitimate call - a direct
threads().get(id=company.deletion_thread_id) on a thread Baker's Dozen
itself created or the user explicitly attached. This module never calls
the Gmail API to READ anything itself; record_inbound_mail_message() is a
pure function of a message dict + its classification, called from inside
check_company_response()'s existing loop. The only Gmail call this module
DOES make is the one explicit, human-approved reply send
(send_mailbox_reply -> google_oauth.send_reply_email).

Mailbox state (MailState) is deliberately NOT a stored column - it's
computed fresh from a company's MailMessage rows + its (unchanged, still
strictly-evidenced) deletion_status every time it's needed, the same
"never cached, always recomputed" philosophy deletion_engine.ExecutionPlan
already uses. This means there is exactly one place mailbox state can be
wrong, and it's always self-consistent with the messages it describes.

RESPONSE_DRAFTED is part of the MailState vocabulary for documentation/
future-persistence purposes, but this iteration never stores a draft -
the outgoing-letter preview is computed fresh and shown, then either sent
or discarded, the same pattern as the existing "Delete my data" modal
(GET preview, POST to actually act). So RESPONSE_DRAFTED only exists
client-side, for as long as that preview panel is open.
"""
import datetime
import email.utils

from sqlalchemy.orm import Session

from app.deletion_constants import DeletionStatus, EventSource, EventType
from app.deletion_events import record_event
from app.models import Company, DeletionRecipe, MailMessage
from app.response_classify import ResponseClassification


class MailState:
    UNREAD = "UNREAD"
    READ = "READ"
    ACTION_NEEDED = "ACTION_NEEDED"
    RESPONSE_DRAFTED = "RESPONSE_DRAFTED"  # transient/client-side only - see module docstring
    REPLIED = "REPLIED"
    RESOLVED = "RESOLVED"


class MailSendError(Exception):
    """Raised when a mailbox reply's Gmail send itself fails. No MailMessage
    row is ever created and deletion_status is never touched when this is
    raised - a failed send must never appear as a sent reply."""


class ReplyKind:
    """The only two auto-draftable mailbox replies this iteration supports -
    both answer a well-defined question (do you want your account closed
    along with your data, or kept) rather than fabricating an answer to an
    open-ended "what information do you need" ask. See
    account_deletion_choice_available()."""

    DELETE_ACCOUNT_AND_DATA = "delete_account_and_data"
    KEEP_ACCOUNT_DATA_ONLY = "keep_account_request_data_only"

    ALL = {DELETE_ACCOUNT_AND_DATA, KEEP_ACCOUNT_DATA_ONLY}


_ACTION_NEEDED_CLASSIFICATIONS = {DeletionStatus.VERIFICATION_NEEDED, DeletionStatus.MORE_INFO_REQUIRED}

_RESOLVED_LABELS = {
    DeletionStatus.COMPLETED: "Deletion completed",
    DeletionStatus.REJECTED: "Request declined",
    DeletionStatus.FAILED: "Couldn't be tracked further",
}


# --- Header / body extraction from an already-fetched Gmail message ---

def _header(message: dict, name: str) -> str:
    headers = message.get("payload", {}).get("headers", [])
    return next((h.get("value", "") for h in headers if h.get("name", "").lower() == name.lower()), "")


def _occurred_at(message: dict) -> datetime.datetime:
    try:
        ms = int(message.get("internalDate", "0"))
        return datetime.datetime.utcfromtimestamp(ms / 1000)
    except (TypeError, ValueError, OSError):
        return datetime.datetime.utcnow()


def record_inbound_mail_message(
    db: Session, company: Company, message: dict, body_text: str, classification: ResponseClassification,
) -> MailMessage:
    """Creates a MailMessage row for ONE already-fetched, already-classified,
    already-quote-stripped inbound Gmail message. Never fetches anything
    itself - called from inside deletion_response_tracker.check_company_response's
    existing loop, the only caller. Idempotent: a second call for the same
    gmail_message_id returns the existing row rather than inserting a
    duplicate, so a crash/retry between the dedup marker moving and this
    commit can never produce two mailbox entries for one Gmail message."""
    gmail_message_id = message.get("id", "")
    existing = db.query(MailMessage).filter(MailMessage.gmail_message_id == gmail_message_id).one_or_none()
    if existing is not None:
        return existing
    row = MailMessage(
        company_id=company.id,
        direction="inbound",
        gmail_message_id=gmail_message_id,
        gmail_thread_id=company.deletion_thread_id or "",
        rfc822_message_id=_header(message, "Message-ID") or None,
        occurred_at=_occurred_at(message),
        from_display=_header(message, "From"),
        subject=_header(message, "Subject"),
        body_excerpt=(body_text or "").strip()[:4000],
        classification_status=classification.status,
        classification_confidence=classification.confidence,
        classification_quote=classification.quote,
        read_at=None,
    )
    db.add(row)
    return row


# --- Reading / state ---

def get_company_mail(db: Session, company_id: int) -> list[MailMessage]:
    return (
        db.query(MailMessage)
        .filter(MailMessage.company_id == company_id)
        .order_by(MailMessage.occurred_at.asc())
        .all()
    )


def mark_inbound_read(db: Session, company_id: int) -> int:
    """Marks every currently-unread inbound message for this company read.
    An explicit action (opening the letter), never automatic on a
    background poll - see main.py's mail routes."""
    rows = (
        db.query(MailMessage)
        .filter(MailMessage.company_id == company_id, MailMessage.direction == "inbound", MailMessage.read_at.is_(None))
        .all()
    )
    now = datetime.datetime.utcnow()
    for row in rows:
        row.read_at = now
    if rows:
        db.commit()
    return len(rows)


def mail_state_for_company(company: Company, messages: list[MailMessage]) -> str | None:
    """Derived, never stored - see module docstring. Returns None if this
    company has no mail at all (no mailbox entry should be shown)."""
    if not messages:
        return None
    if company.deletion_status in DeletionStatus.TERMINAL:
        return MailState.RESOLVED

    inbound = [m for m in messages if m.direction == "inbound"]
    if any(m.read_at is None for m in inbound):
        return MailState.UNREAD

    outbound = [m for m in messages if m.direction == "outbound"]
    latest_inbound = max(inbound, key=lambda m: m.occurred_at) if inbound else None
    latest_outbound = max(outbound, key=lambda m: m.occurred_at) if outbound else None

    if latest_inbound is not None and latest_inbound.classification_status in _ACTION_NEEDED_CLASSIFICATIONS:
        if latest_outbound is None or latest_outbound.occurred_at < latest_inbound.occurred_at:
            return MailState.ACTION_NEEDED

    if latest_outbound is not None and (latest_inbound is None or latest_outbound.occurred_at > latest_inbound.occurred_at):
        return MailState.REPLIED

    return MailState.READ


def mailbox_reason_label(company: Company, state: str | None) -> str:
    """The short, plain-language line shown next to each envelope - e.g.
    "New reply" / "Action needed" / "Deletion completed". Never a raw
    DeletionStatus or MailState value."""
    if state == MailState.RESOLVED:
        return _RESOLVED_LABELS.get(company.deletion_status, "Resolved")
    return {
        MailState.UNREAD: "New reply",
        MailState.ACTION_NEEDED: "Action needed",
        MailState.REPLIED: "Waiting on their reply",
        MailState.READ: "Read",
    }.get(state, "")


def mailbox_entries(db: Session) -> list[dict]:
    """One entry per company that has at least one MailMessage row, newest
    activity first - the mailbox list view's data. Small/prototype-scale
    query pattern (N+1 per company); fine at this scale, not the place to
    add a heavier join for a local single-user tool."""
    company_ids = [row[0] for row in db.query(MailMessage.company_id).distinct().all()]
    if not company_ids:
        return []
    companies = {c.id: c for c in db.query(Company).filter(Company.id.in_(company_ids)).all()}
    entries = []
    for company_id in company_ids:
        company = companies.get(company_id)
        if company is None:
            continue
        messages = get_company_mail(db, company_id)
        if not messages:
            continue
        state = mail_state_for_company(company, messages)
        entries.append({
            "company": company,
            "state": state,
            "reason_label": mailbox_reason_label(company, state),
            "latest": messages[-1],
            "unread": state == MailState.UNREAD,
        })
    entries.sort(key=lambda e: e["latest"].occurred_at, reverse=True)
    return entries


def unread_mail_count(db: Session) -> int:
    return db.query(MailMessage).filter(MailMessage.direction == "inbound", MailMessage.read_at.is_(None)).count()


# --- Account-deletion-vs-data-deletion choice + reply drafting ---

def _recipe_for(db: Session, company: Company) -> DeletionRecipe | None:
    return db.query(DeletionRecipe).filter(DeletionRecipe.domain == company.domain).one_or_none()


def account_deletion_choice_available(company: Company, recipe: DeletionRecipe | None) -> bool:
    """True only when there's something real to warn about: the company is
    currently asking for verification or more information (the only two
    states this iteration offers a drafted reply for - see ReplyKind's
    docstring for why), AND the verified recipe explicitly says this
    method deletes the account too. Never inferred from the reply text
    itself - only from the same already-verified recipe field the
    execution-approval modal already reads (DeletionRecipe.deletes_account),
    so nothing here is a new, less-certain source of truth."""
    if company.deletion_status not in _ACTION_NEEDED_CLASSIFICATIONS:
        return False
    return bool(recipe and recipe.deletes_account)


def _reply_to_address(latest_inbound: MailMessage | None, company: Company, recipe: DeletionRecipe | None) -> str:
    if latest_inbound is not None:
        addr = email.utils.parseaddr(latest_inbound.from_display or "")[1]
        if addr:
            return addr
    return (recipe.email if recipe else None) or company.deletion_email or f"privacy@{company.domain}"


def build_choice_reply(
    company: Company, recipe: DeletionRecipe | None, latest_inbound: MailMessage | None, kind: str, user_email: str,
) -> dict:
    """Builds the exact outgoing reply from structured fields only - same
    rule as deletion_engine.build_structured_email, never LLM-composed
    prose. The KEEP_ACCOUNT_DATA_ONLY wording is a generic baseline, not
    jurisdiction- or company-tuned - see the mailbox proposal notes;
    tuning it per jurisdiction is explicitly future work, not pretended to
    be solved here."""
    if kind not in ReplyKind.ALL:
        raise ValueError(f"Unknown reply kind: {kind}")

    to_email = _reply_to_address(latest_inbound, company, recipe)
    subject = (latest_inbound.subject if latest_inbound else "") or f"Re: your data at {company.name}"
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    if kind == ReplyKind.DELETE_ACCOUNT_AND_DATA:
        body_lines = [
            f"Hello {company.name} team,",
            "",
            "I confirm I would like to proceed with my original deletion request, "
            "including the closure of my account and the consequences described "
            "(such as loss of account access, order/account history, and saved "
            "preferences, where applicable).",
        ]
    else:
        body_lines = [
            f"Hello {company.name} team,",
            "",
            "I would like to maintain my account. Please delete personal information "
            "that is not necessary to provide the service, maintain my account, "
            "comply with legal obligations, or otherwise subject to an applicable "
            "retention exception. Please let me know what information must be "
            "retained and why.",
        ]
    body_lines += [
        "", f"This request relates to the account/contact associated with: {user_email}", "", "Thank you.",
    ]
    return {"to": to_email, "subject": subject, "body": "\n".join(body_lines)}


def send_mailbox_reply(db: Session, company: Company, kind: str, creds, gmail_address: str) -> MailMessage:
    """The one place a mailbox reply is actually sent - only ever reached
    after the outgoing-letter preview has been shown and explicitly
    approved (see main.py's respond preview/send route split). Raises
    MailSendError (no MailMessage row, deletion_status untouched) if the
    Gmail send itself fails - a failed send must never appear as sent."""
    if not company.deletion_thread_id:
        raise ValueError("This company has no tracked email thread to reply on.")
    recipe = _recipe_for(db, company)
    if not account_deletion_choice_available(company, recipe):
        raise ValueError("No account-deletion choice is available for this company right now.")

    messages = get_company_mail(db, company.id)
    inbound = [m for m in messages if m.direction == "inbound"]
    latest_inbound = max(inbound, key=lambda m: m.occurred_at) if inbound else None

    draft = build_choice_reply(company, recipe, latest_inbound, kind, gmail_address)

    try:
        response = google_oauth_send_reply(
            creds, draft["to"], draft["subject"], draft["body"],
            company.deletion_thread_id, latest_inbound.rfc822_message_id if latest_inbound else None,
        )
    except Exception as exc:  # noqa: BLE001 - any send failure lands here, never silently
        record_event(
            db, company.id, EventType.FAILED, source=EventSource.USER,
            evidence={"reason": str(exc)[:300], "context": "mailbox_reply"},
        )
        db.commit()
        raise MailSendError(str(exc)) from exc

    message_id = response.get("id", "")
    thread_id = response.get("threadId") or company.deletion_thread_id
    now = datetime.datetime.utcnow()

    row = MailMessage(
        company_id=company.id, direction="outbound", gmail_message_id=message_id,
        gmail_thread_id=thread_id, occurred_at=now, from_display="You",
        subject=draft["subject"], body_excerpt=draft["body"][:4000], read_at=now,
    )
    db.add(row)

    # Real, evidenced progress: a reply actually went out, so the request is
    # back to waiting on the company - never further than that on the
    # strength of an outbound message alone (COMPLETED still requires the
    # SAME strict classifier evidence it always has - see
    # deletion_response_tracker.py, untouched by this).
    company.deletion_status = DeletionStatus.IN_PROGRESS
    company.deletion_evidence = {
        "type": "gmail_send", "gmail_message_id": message_id, "gmail_thread_id": thread_id,
        "sent_to": draft["to"], "subject": draft["subject"], "sent_at": now.isoformat(),
        "reply_kind": kind,
    }
    record_event(
        db, company.id, EventType.MAIL_REPLY_SENT, source=EventSource.USER,
        evidence={"gmail_message_id": message_id, "gmail_thread_id": thread_id, "reply_kind": kind},
    )
    db.commit()
    return row


def google_oauth_send_reply(creds, to_email, subject, body, thread_id, in_reply_to):
    """Thin indirection so tests can monkeypatch this one call without
    reaching into app.google_oauth directly."""
    from app import google_oauth

    return google_oauth.send_reply_email(creds, to_email, subject, body, thread_id, in_reply_to=in_reply_to)
