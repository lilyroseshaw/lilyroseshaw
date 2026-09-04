"""Background response tracking: reads a company's replies to a deletion
request Cookie Monster itself sent, and updates status accordingly.

Privacy/safety rules enforced here:
- Only ever reads the ONE thread stored in Company.deletion_thread_id
  (google_oauth.fetch_thread_messages does a direct thread get, never a
  search/list) - no other part of the inbox is ever touched.
- Message bodies are decoded in-process for classification, then discarded.
  Only a short (~200 char) quote is ever persisted as event evidence -
  plus, since the mailbox feature, a longer (still capped, still
  quote-stripped) excerpt on a MailMessage row (see app/mail.py) so the
  user can read the letter itself, not just an audit-trail snippet.
- A TRANSIENT check failure (Gmail API error, network error) never changes
  Company.deletion_status - the underlying request status is left exactly
  as it was, and a RESPONSE_CHECK_FAILED event records the failure for the
  backoff policy to retry later. DeletionStatus.FAILED is reserved for a
  genuinely permanent failure (the thread no longer exists at all) - never
  set merely because one poll attempt errored.
- This module itself never auto-replies to a company or sends anything in
  response to VERIFICATION_NEEDED/MORE_INFO_REQUIRED - it only ever
  updates status/evidence/mailbox state. Baker's Dozen's mailbox (see
  app/mail.py's send_mailbox_reply) CAN draft and send a narrow, specific
  reply for exactly these two statuses - but only after the user reads
  the letter and explicitly approves the exact outgoing text, the same
  "never sent without a human click" guarantee this rule always meant.
- A reply almost always quotes some or all of Cookie Monster's own
  outgoing message back (Gmail/most clients do this automatically) - and
  that outgoing text itself contains phrases the classifier looks for
  ("identity verification", etc.). strip_quoted_reply()/_html_to_text()
  below strip that quoted content BEFORE classification, so a company's
  reply is never misclassified off text Cookie Monster itself wrote - see
  their docstrings.
"""
import base64
import datetime
import email.utils
import logging
import re

from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session

from app import chase_engine, config, google_oauth, mail
from app.deletion_constants import DeletionStatus, EventType
from app.deletion_events import record_event
from app.models import Company
from app.response_classify import ResponseClassifier

# Diagnostic-only logging for tracing exactly where a real reply gets lost
# in the pipeline (fetched -> own-message-filtered -> new -> classified ->
# persisted). Deliberately never logs OAuth tokens, secrets, or full
# message bodies - only counts, ids, domains, and classification labels,
# same allow-list the response-check audit events already use. Explicit
# handler/level for the same reason deletion_queue.py's logger has one -
# uvicorn's default root level would otherwise silently swallow it.
_log = logging.getLogger("cookie_monster.response_tracker")
_log.setLevel(logging.INFO)
if not _log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [cookie-monster-reply-check] %(message)s"))
    _log.addHandler(_handler)
    _log.propagate = False

_EVENT_TYPE_FOR_STATUS = {
    DeletionStatus.IN_PROGRESS: EventType.COMPANY_ACKNOWLEDGED,
    DeletionStatus.VERIFICATION_NEEDED: EventType.VERIFICATION_REQUESTED,
    DeletionStatus.MORE_INFO_REQUIRED: EventType.ADDITIONAL_INFO_REQUESTED,
    DeletionStatus.COMPLETED: EventType.COMPLETION_CONFIRMED,
    DeletionStatus.REJECTED: EventType.REQUEST_REJECTED,
    DeletionStatus.SUBMITTED: EventType.COMPANY_ACKNOWLEDGED,
    DeletionStatus.UNKNOWN_RESPONSE: EventType.COMPANY_ACKNOWLEDGED,
}


# --- Body extraction (transient - never persisted) ---

def _b64_decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    # Quoted prior content in an HTML reply is virtually always wrapped in
    # a <blockquote> (the standard HTML-email quoting tag every major
    # client emits, including Gmail's own <blockquote class="gmail_quote">)
    # or a "gmail_quote" div - drop it here, structurally, before it ever
    # becomes plain text. This is more reliable than any text-pattern
    # heuristic for HTML mail, and covers most of what strip_quoted_reply()
    # below has to fall back to guessing at for plain text.
    for tag in soup.select("blockquote, .gmail_quote"):
        tag.decompose()
    return " ".join(soup.get_text(separator=" ").split())


# A reply's own NEW words are, by overwhelming convention (top-posting),
# followed by a quote HEADER and then the original quoted message - never
# the reverse. Each pattern below is a well-established, high-precision
# marker for exactly that boundary; deliberately narrow (no bare "From:"
# line, no generic separator) so genuine new text is never mistaken for a
# quote header and truncated.
_QUOTE_BOUNDARY_PATTERNS = [
    re.compile(r"^On .{0,150} wrote:\s*$", re.IGNORECASE),  # Gmail / Apple Mail / most clients
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),  # classic Outlook
    re.compile(r"^_{10,}$"),  # Outlook's separator line before a quoted header block
]


def strip_quoted_reply(text: str) -> str:
    """Best-effort removal of quoted prior-message content from a PLAIN
    TEXT reply (HTML replies are already handled structurally by
    _html_to_text's blockquote removal above - this is the fallback for
    clients that quote with plain '>' markers or a text quote header
    instead). Conservative: only trims from the FIRST clearly-quoted
    boundary onward - the first '>'-prefixed line, or a recognized quote-
    header line (see _QUOTE_BOUNDARY_PATTERNS), whichever comes first. If
    neither is found anywhere in the text, returns it completely
    unchanged - this never invents a boundary or strips content it isn't
    reasonably confident is quoted history.

    This is what stops a reply that merely quotes Cookie Monster's own
    outgoing deletion request (which itself contains phrases like
    "identity verification" - see response_classify.py's patterns) from
    having that quoted text mistaken for the company's own new words."""
    lines = text.splitlines()
    cutoff = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(">"):
            cutoff = i
            break
        if any(pattern.match(stripped) for pattern in _QUOTE_BOUNDARY_PATTERNS):
            cutoff = i
            break
    return "\n".join(lines[:cutoff]).strip()


def extract_body_text(message: dict) -> str:
    """Walks a Gmail message payload for text/plain (preferred) or text/html
    content. Returns "" if nothing decodable is found. The caller uses this
    for classification only, in-process, and discards it afterward. Does
    NOT strip quoted content itself - see strip_quoted_reply(), applied
    separately by the caller, so extract_body_text stays purely about MIME
    parsing."""
    plain_texts: list[str] = []
    html_texts: list[str] = []

    def _walk(part: dict) -> None:
        mime_type = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if mime_type == "text/plain" and data:
            plain_texts.append(_b64_decode(data))
        elif mime_type == "text/html" and data:
            html_texts.append(_html_to_text(_b64_decode(data)))
        for sub_part in part.get("parts", []):
            _walk(sub_part)

    _walk(message.get("payload", {}))
    if plain_texts:
        return "\n".join(plain_texts)
    if html_texts:
        return "\n".join(html_texts)
    return ""


# --- Own-message filtering / dedup ---

def _is_own_message(message: dict, gmail_address: str) -> bool:
    """A real, reproducible bug lived here: comparing gmail_address as a
    raw SUBSTRING of the whole From header, rather than the header's
    actual parsed address. Some helpdesk/ticketing senders format a
    reply's From header in ways that legitimately CONTAIN the
    recipient's own address as text - "Support <support@co.com> on
    behalf of me@gmail.com", '"me@gmail.com via Co Support" <...>', a
    parenthetical "(me@gmail.com)" note - none of those mean the message
    is actually FROM this account. A substring match silently treated
    every one of those as "our own message" and discarded a genuine
    company reply before it ever reached classification, in the SAME
    tracked thread, evidence-free. Parsing the address out and comparing
    it exactly closes this."""
    if "SENT" in message.get("labelIds", []):
        return True
    headers = message.get("payload", {}).get("headers", [])
    from_header = next((h["value"] for h in headers if h.get("name", "").lower() == "from"), "")
    sender_address = email.utils.parseaddr(from_header)[1]
    return sender_address.lower() == gmail_address.lower()


def _select_new_company_messages(messages: list[dict], gmail_address: str, last_processed_id: str | None) -> list[dict]:
    def _timestamp(m: dict) -> int:
        try:
            return int(m.get("internalDate", "0"))
        except (TypeError, ValueError):
            return 0

    ordered = sorted(messages, key=_timestamp)
    if last_processed_id:
        ids = [m["id"] for m in ordered]
        if last_processed_id in ids:
            ordered = ordered[ids.index(last_processed_id) + 1:]
    return [m for m in ordered if not _is_own_message(m, gmail_address)]


# --- Scheduling / backoff ---

def _is_due(company: Company, now: datetime.datetime) -> bool:
    if company.deletion_response_checked_at is None:
        return True
    if company.deletion_response_check_failures > 0:
        backoff_hours = min(
            config.RESPONSE_CHECK_BACKOFF_BASE_HOURS * (2 ** (company.deletion_response_check_failures - 1)),
            config.RESPONSE_CHECK_BACKOFF_MAX_HOURS,
        )
        return now - company.deletion_response_checked_at >= datetime.timedelta(hours=backoff_hours)
    return now - company.deletion_response_checked_at >= datetime.timedelta(hours=config.RESPONSE_CHECK_MIN_INTERVAL_HOURS)


def get_companies_due_for_check(db: Session, limit: int | None = None) -> list[Company]:
    """Only companies with a tracked thread, still in an actively-monitored
    status (terminal statuses stop being polled), whose backoff/interval has
    elapsed."""
    limit = limit or config.RESPONSE_CHECK_BATCH_SIZE
    now = datetime.datetime.utcnow()
    min_interval = datetime.timedelta(hours=config.RESPONSE_CHECK_MIN_INTERVAL_HOURS)

    candidates = (
        db.query(Company)
        .filter(Company.deletion_thread_id.isnot(None))
        .filter(Company.deletion_status.in_(DeletionStatus.ACTIVELY_MONITORED))
        .filter(
            (Company.deletion_response_checked_at.is_(None))
            | (Company.deletion_response_checked_at <= now - min_interval)
        )
        .order_by(Company.deletion_response_checked_at.asc())  # NULLs (never checked) first
        .limit(limit * 3)  # over-fetch; some will still be in backoff
        .all()
    )
    due = [c for c in candidates if _is_due(c, now)]
    return due[:limit]


# --- Core check ---

def check_company_response(
    db: Session, company: Company, creds: Credentials, gmail_address: str, classifier: ResponseClassifier
) -> None:
    """Checks ONE company's tracked thread for new company replies. Commits
    its own changes. Never raises - any failure is caught and recorded."""
    now = datetime.datetime.utcnow()
    company.deletion_response_checked_at = now

    _log.info(
        "check start: company=%s domain=%s thread=%s last_processed=%s",
        company.id, company.domain, company.deletion_thread_id, company.deletion_last_response_message_id,
    )
    try:
        messages = google_oauth.fetch_thread_messages(creds, company.deletion_thread_id)
    except HttpError as exc:
        status_code = getattr(getattr(exc, "resp", None), "status", None)
        if status_code == 404:
            # Permanent: the thread itself is gone - tracking has genuinely,
            # meaningfully failed, not just one poll attempt.
            company.deletion_status = DeletionStatus.FAILED
            company.deletion_error = "The tracked Gmail thread could not be found (it may have been deleted)."
            company.waiting_on = None
            company.next_followup_at = None
            record_event(
                db, company.id, EventType.FAILED,
                evidence={"reason": "thread_not_found", "http_status": 404},
            )
        else:
            company.deletion_response_check_failures += 1
            record_event(
                db, company.id, EventType.RESPONSE_CHECK_FAILED,
                evidence={"http_status": status_code, "error": str(exc)[:200]},
            )
        db.commit()
        return
    except Exception as exc:  # noqa: BLE001 - network errors, etc. - always transient, never overwrite status
        company.deletion_response_check_failures += 1
        record_event(db, company.id, EventType.RESPONSE_CHECK_FAILED, evidence={"error": str(exc)[:200]})
        db.commit()
        return

    # Talking to Gmail succeeded - reset the failure streak regardless of
    # whether there's anything new to classify.
    company.deletion_response_check_failures = 0

    own_count = sum(1 for m in messages if _is_own_message(m, gmail_address))
    _log.info(
        "check fetched: company=%s thread=%s total_messages=%d own_messages=%d",
        company.id, company.deletion_thread_id, len(messages), own_count,
    )
    new_messages = _select_new_company_messages(messages, gmail_address, company.deletion_last_response_message_id)
    _log.info("check filtered: company=%s new_messages=%d", company.id, len(new_messages))
    if not new_messages:
        if len(messages) <= own_count:
            _log.info(
                "check found nothing yet: company=%s thread=%s has only our own message(s) so far "
                "- if the company already replied in Gmail, their reply likely landed in a DIFFERENT "
                "thread (broken References/In-Reply-To on their end is common) - use 'Add confirmation "
                "email' / 'Use this email instead' on the dashboard to point tracking at the right one.",
                company.id, company.deletion_thread_id,
            )
        db.commit()
        return

    last_classification = None
    last_message_body = ""
    last_message_occurred_at = now
    for message in new_messages:
        # Quoted prior content (almost always including Cookie Monster's own
        # outgoing message) is stripped BEFORE classification AND before
        # anything is persisted - see strip_quoted_reply()'s docstring for
        # why this matters. The stripped text is used for classification and
        # for the mailbox's MailMessage.body_excerpt below (capped, never
        # the full raw message) - it is not otherwise kept around.
        raw_body = extract_body_text(message)
        body_text = strip_quoted_reply(raw_body)
        classification = classifier.classify(body_text)
        _log.info(
            "check classifying: company=%s message=%s extracted_chars=%d stripped_chars=%d "
            "classification=%s confidence=%s",
            company.id, message.get("id"), len(raw_body), len(body_text),
            classification.status, classification.confidence,
        )
        record_event(
            db, company.id,
            _EVENT_TYPE_FOR_STATUS.get(classification.status, EventType.COMPANY_ACKNOWLEDGED),
            evidence={
                "quote": classification.quote,
                "confidence": classification.confidence,
                "message_id": message.get("id"),
            },
        )
        # Mailbox correspondence row for this same message - see app/mail.py
        # for why this lives here rather than a second Gmail fetch: this is
        # the ONE place a message from the tracked thread is ever seen.
        mail.record_inbound_mail_message(db, company, message, body_text, classification)
        company.deletion_last_response_message_id = message.get("id")
        last_classification = classification
        last_message_body = body_text
        last_message_occurred_at = mail._occurred_at(message)

    if last_classification is not None:
        company.deletion_status = last_classification.status
        # Same transparency pattern used everywhere else in this app -
        # "detected because" evidence, not just for COMPLETED.
        company.deletion_evidence = {
            "type": "gmail_reply",
            "quote": last_classification.quote,
            "confidence": last_classification.confidence,
            "classified_at": now.isoformat(),
        }
        if last_classification.status == DeletionStatus.COMPLETED:
            company.deletion_completed_at = now
        chase_engine.on_reply_classified(company, last_classification.status, last_message_body, last_message_occurred_at)

    db.commit()


def process_response_checks(
    db: Session, creds: Credentials, gmail_address: str, classifier: ResponseClassifier, limit: int | None = None
) -> int:
    """Batch entry point for the background worker. Returns how many
    companies were checked."""
    companies = get_companies_due_for_check(db, limit)
    for company in companies:
        check_company_response(db, company, creds, gmail_address, classifier)
    return len(companies)
