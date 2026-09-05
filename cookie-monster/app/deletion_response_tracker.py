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
from app.models import Company, DeletionEvent, MailMessage
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
    # Deliberately NOT COMPANY_ACKNOWLEDGED - UNKNOWN_RESPONSE means the
    # classifier could not confidently place the reply into any category,
    # so the audit event must not assert the company acknowledged
    # anything. See EventType.UNCLASSIFIED_REPLY_RECEIVED's own comment.
    DeletionStatus.UNKNOWN_RESPONSE: EventType.UNCLASSIFIED_REPLY_RECEIVED,
    DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED: EventType.ACCOUNT_CLOSED_DATA_UNVERIFIED,
}

# check_company_response's return value - lets callers (the manual
# "Check for reply" button, the background batch job) distinguish what
# actually happened instead of inferring it from whether the dedup cursor
# moved. Cursor movement alone conflates two very different events: a
# genuinely new company-authored message arriving, versus correcting an
# already-seen message's OLD classification with today's improved
# classifier (which never moves the cursor - see
# reclassify_stale_unknown_response). Company-agnostic: these three
# outcomes are the only ones the function can ever produce, for any
# company.
CHECK_RESULT_NEW_MESSAGE = "new_message"    # a new company-authored message was found and classified
CHECK_RESULT_RECLASSIFIED = "reclassified"  # no new message, but stale persisted evidence was reclassified
CHECK_RESULT_NO_CHANGE = "no_change"        # nothing new, nothing to reclassify


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


def _select_new_company_messages(
    messages: list[dict], gmail_address: str, last_processed_id: str | None, include_cursor: bool = False,
) -> list[dict]:
    """include_cursor=True treats the message AT last_processed_id itself
    as unseen too, not just messages strictly after it. Used ONLY for a
    legacy company whose cursor points at a message from before mailbox
    persistence existed (zero MailMessage rows) - the ORIGINAL evidence
    for that exact message may have been produced by a since-fixed bug
    (a real example: corrupted quote-extraction that captured a fragment
    of Cookie Monster's OWN outgoing request instead of the company's new
    words, misclassifying a Goop Kitchen account-closure reply as
    VERIFICATION_NEEDED). See check_company_response's own call site for
    exactly when this is set - never once even one MailMessage row
    exists for the company, at which point modern, MailMessage-backed
    state always takes precedence."""
    def _timestamp(m: dict) -> int:
        try:
            return int(m.get("internalDate", "0"))
        except (TypeError, ValueError):
            return 0

    ordered = sorted(messages, key=_timestamp)
    if last_processed_id:
        ids = [m["id"] for m in ordered]
        if last_processed_id in ids:
            cursor_index = ids.index(last_processed_id)
            ordered = ordered[cursor_index:] if include_cursor else ordered[cursor_index + 1:]
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
) -> str:
    """Checks ONE company's tracked thread for new company replies. Commits
    its own changes. Never raises - any failure is caught and recorded.
    Returns one of CHECK_RESULT_NEW_MESSAGE / CHECK_RESULT_RECLASSIFIED /
    CHECK_RESULT_NO_CHANGE (see their definitions above) - callers that
    only care about failures (the pre-existing before/after failure-count
    comparison) can ignore the return value entirely."""
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
        return CHECK_RESULT_NO_CHANGE
    except Exception as exc:  # noqa: BLE001 - network errors, etc. - always transient, never overwrite status
        company.deletion_response_check_failures += 1
        record_event(db, company.id, EventType.RESPONSE_CHECK_FAILED, evidence={"error": str(exc)[:200]})
        db.commit()
        return CHECK_RESULT_NO_CHANGE

    # Talking to Gmail succeeded - reset the failure streak regardless of
    # whether there's anything new to classify.
    company.deletion_response_check_failures = 0

    own_count = sum(1 for m in messages if _is_own_message(m, gmail_address))
    _log.info(
        "check fetched: company=%s thread=%s total_messages=%d own_messages=%d",
        company.id, company.deletion_thread_id, len(messages), own_count,
    )
    last_processed_id = company.deletion_last_response_message_id
    # Legacy-cursor recovery: a company whose cursor is already set but has
    # ZERO MailMessage rows predates mailbox persistence entirely - the
    # ORIGINAL evidence for the message at that cursor may have been
    # produced by a since-fixed bug (corrupted quote-extraction, an own-
    # message-detection miss, ...) and never actually captured the
    # company's real words. Re-examining that ONE message with today's
    # extraction/classification is safe exactly because there is no
    # MailMessage row yet to duplicate or contradict - the very first
    # MailMessage row this creates is what turns this off for good on
    # every later check (modern, MailMessage-backed state then always
    # takes precedence). Never broadens WHICH thread is read - still the
    # one already-tracked thread, still no cursor reset.
    # WHY this exact condition, and only this one: the MODERN pipeline
    # (the loop below, every time) unconditionally creates a MailMessage
    # row for every message it ever examines - so "a cursor is already
    # set" (a message WAS classified at some point) together with "zero
    # MailMessage rows exist for this company" can only ever be true for
    # a case that predates mailbox persistence entirely. It is not a
    # guess or a heuristic; it is the one combination the current system
    # cannot itself produce. Do NOT broaden this to any other condition
    # (a particular deletion_status, a time window, "looks stuck", etc.)
    # without equally strong justification - narrowness here is what
    # keeps this safe to run unattended on every check.
    original_cursor_id = company.deletion_last_response_message_id
    legacy_cursor_recovery = bool(original_cursor_id) and (
        db.query(MailMessage).filter(MailMessage.company_id == company.id).count() == 0
    )
    new_messages = _select_new_company_messages(
        messages, gmail_address, company.deletion_last_response_message_id, include_cursor=legacy_cursor_recovery,
    )
    if legacy_cursor_recovery and new_messages:
        _log.info(
            "check legacy-cursor recovery: company=%s cursor=%s has zero MailMessage rows - "
            "re-examining the cursor message itself with current extraction/classification",
            company.id, original_cursor_id,
        )
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
        # No new company-authored content in the live thread - but if the
        # company is STILL sitting at UNKNOWN_RESPONSE, today's classifier
        # may now understand the reply it already persisted better than
        # the classifier that originally examined it did (a real example:
        # a deterministic pattern gap gets fixed). This is exactly what
        # reclassify_stale_unknown_response exists for - re-running the
        # CURRENT classifier against the already-stored MailMessage body,
        # never a new Gmail read, never a resend, never able to run twice
        # in a row (its own idempotency guard requires deletion_status to
        # still be UNKNOWN_RESPONSE). Company-agnostic: keyed purely off
        # deletion_status, nothing about who the company is.
        reclassified = False
        if company.deletion_status == DeletionStatus.UNKNOWN_RESPONSE:
            reclassified = reclassify_stale_unknown_response(db, company, classifier)
        db.commit()
        return CHECK_RESULT_RECLASSIFIED if reclassified else CHECK_RESULT_NO_CHANGE

    last_classification = None
    last_message_body = ""
    last_message_occurred_at = now
    last_was_recovered_cursor_message = False
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
        # Transparent provenance: a re-examined legacy-cursor message is
        # tagged distinctly from a genuinely brand-new one, so it's always
        # obvious, later, that Cookie Monster corrected its own earlier
        # (possibly corrupted) interpretation of this exact message rather
        # than just having received it for the first time.
        is_recovered_cursor_message = legacy_cursor_recovery and message.get("id") == original_cursor_id
        event_evidence = {
            "quote": classification.quote,
            "confidence": classification.confidence,
            "message_id": message.get("id"),
        }
        if is_recovered_cursor_message:
            event_evidence["legacy_cursor_recovered"] = True
        record_event(
            db, company.id,
            _EVENT_TYPE_FOR_STATUS.get(classification.status, EventType.COMPANY_ACKNOWLEDGED),
            evidence=event_evidence,
        )
        # Mailbox correspondence row for this same message - see app/mail.py
        # for why this lives here rather than a second Gmail fetch: this is
        # the ONE place a message from the tracked thread is ever seen.
        mail.record_inbound_mail_message(db, company, message, body_text, classification)
        company.deletion_last_response_message_id = message.get("id")
        last_classification = classification
        last_message_body = body_text
        last_message_occurred_at = mail._occurred_at(message)
        last_was_recovered_cursor_message = is_recovered_cursor_message

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
        if last_was_recovered_cursor_message:
            company.deletion_evidence["legacy_cursor_recovered"] = True
        if last_classification.status == DeletionStatus.COMPLETED:
            company.deletion_completed_at = now
        chase_engine.on_reply_classified(company, last_classification.status, last_message_body, last_message_occurred_at)

    db.commit()
    return CHECK_RESULT_NEW_MESSAGE


def process_response_checks(
    db: Session, creds: Credentials, gmail_address: str, classifier: ResponseClassifier, limit: int | None = None
) -> int:
    """Batch entry point for the background worker. Returns how many
    companies were checked."""
    companies = get_companies_due_for_check(db, limit)
    for company in companies:
        check_company_response(db, company, creds, gmail_address, classifier)
    return len(companies)


# --- Stale UNKNOWN_RESPONSE reclassification ---
#
# A real bug: a company reply already stored and marked "processed" (its
# gmail_message_id is company.deletion_last_response_message_id) can have
# been classified UNKNOWN_RESPONSE only because response_classify.py's
# patterns didn't yet cover its exact phrasing (e.g. "we have received
# your EMAIL" instead of "...your request", or "someone from our team
# will get back to you" instead of "we will get back to you" - both real,
# now-fixed gaps found via a live MALK Organics reply). Once the
# classifier improves, that company is stuck: check_company_response only
# ever re-examines messages NEWER than the stored cursor, so a message
# already marked processed is never looked at again, no matter how much
# better the classifier gets.
#
# The fix reclassifies using ONLY what's already stored - the inbound
# MailMessage.body_excerpt captured at the time (already the same
# quote-stripped text the classifier originally saw, see
# mail.record_inbound_mail_message) - never a new Gmail fetch, never a
# new MailMessage row, and never a change to deletion_last_response_message_id
# (the message stays "processed"; nothing is ever reprocessed as if it
# just arrived). A no-op (still UNKNOWN_RESPONSE) touches nothing at all.

def get_companies_with_stale_unknown_response(db: Session, limit: int | None = None) -> list[Company]:
    """Companies stuck on a stored UNKNOWN_RESPONSE classification for
    their last-processed reply - candidates for safe reclassification if
    the classifier's patterns have since improved. Every OTHER status
    (including a wrong one a human would need to correct by hand) is left
    completely alone - only genuine classifier uncertainty is ever
    silently re-derived."""
    limit = limit or config.RESPONSE_CHECK_BATCH_SIZE
    return (
        db.query(Company)
        .filter(
            Company.deletion_status == DeletionStatus.UNKNOWN_RESPONSE,
            Company.deletion_thread_id.isnot(None),
            Company.deletion_last_response_message_id.isnot(None),
        )
        .order_by(Company.id)
        .limit(limit)
        .all()
    )


def _apply_reclassification(
    db: Session, company: Company, classification, body_text: str, occurred_at: datetime.datetime,
    message_id: str, legacy: bool,
) -> None:
    """Shared write path for both reconciliation sources (a current
    MailMessage row, or a legacy pre-mailbox DeletionEvent) - identical
    status/evidence/audit/chase-scheduling treatment either way, so the
    two sources can never quietly diverge in behavior. Caller has already
    confirmed classification.status != UNKNOWN_RESPONSE."""
    now = datetime.datetime.utcnow()
    company.deletion_status = classification.status
    evidence = {
        "type": "gmail_reply",
        "quote": classification.quote,
        "confidence": classification.confidence,
        "classified_at": now.isoformat(),
        # Transparent provenance: this came from re-reading ALREADY-stored
        # evidence with an improved classifier, never a fresh Gmail read -
        # obvious, later, that Baker's Dozen corrected its own earlier
        # interpretation.
        "reclassified": True,
    }
    if legacy:
        evidence["legacy_reconciliation"] = True
        evidence["source_message_id"] = message_id
    company.deletion_evidence = evidence
    if classification.status == DeletionStatus.COMPLETED:
        company.deletion_completed_at = now

    event_evidence = {
        "quote": classification.quote,
        "confidence": classification.confidence,
        "message_id": message_id,
        "reclassified": True,
    }
    if legacy:
        event_evidence["legacy_reconciliation"] = True
        event_evidence["source_message_id"] = message_id
    record_event(db, company.id, _EVENT_TYPE_FOR_STATUS.get(classification.status, EventType.COMPANY_ACKNOWLEDGED), evidence=event_evidence)

    # Anchored to the REAL historical occurred_at, not "now" - a
    # classifier mistake Baker's Dozen itself made must never hand a
    # company an extra 24 hours just because the correction happened
    # late. If the real reply was, say, 3 days ago, next_followup_at
    # lands 3 days in the past - immediately overdue, so the very next
    # worker tick's normal get_companies_due_for_followup/send_followup
    # path picks it up and sends exactly ONE catch-up follow-up (never
    # one per missed day: the due-check only ever asks "is
    # next_followup_at <= now", not "how many days overdue"). That send
    # then reschedules next_followup_at from its OWN real send time, same
    # as any other follow-up - so cadence resumes at +24h from the actual
    # catch-up send, never from this reconciliation moment. If the reply
    # was recent (< 24h old), this schedules a normal future window
    # instead - the same formula handles both cases.
    chase_engine.on_reply_classified(company, classification.status, body_text, occurred_at)


def reclassify_stale_unknown_response(db: Session, company: Company, classifier: ResponseClassifier) -> bool:
    """Re-runs the CURRENT classifier against already-stored evidence for
    this company's last-processed reply - preferring the current
    MailMessage-based path, falling back to a legacy pre-mailbox
    DeletionEvent only when no MailMessage exists at all (see
    _find_legacy_acknowledgment_event's docstring for exactly why that
    fallback is safe and how narrowly it's scoped). Returns True only if
    the status actually changed to something other than UNKNOWN_RESPONSE
    (a genuine improvement) - False (and no changes made at all) if
    nothing usable is found, or the classifier still can't confidently
    place it."""
    # Self-contained idempotency guard - NOT just relied on via the batch
    # query's filter (get_companies_with_stale_unknown_response), since
    # this function is called directly elsewhere (tests, and potentially
    # future callers). Once reclassified, the event this looks up for the
    # legacy path can otherwise still "match" (IN_PROGRESS/SUBMITTED and
    # UNKNOWN_RESPONSE all record under the SAME EventType.COMPANY_ACKNOWLEDGED
    # - see _EVENT_TYPE_FOR_STATUS), so this check must come first, not be
    # inferred from whatever the lookups below happen to find.
    if company.deletion_status != DeletionStatus.UNKNOWN_RESPONSE:
        return False
    message_row = (
        db.query(MailMessage)
        .filter(
            MailMessage.company_id == company.id,
            MailMessage.gmail_message_id == company.deletion_last_response_message_id,
            MailMessage.direction == "inbound",
        )
        .one_or_none()
    )
    if message_row is not None:
        classification = classifier.classify(message_row.body_excerpt)
        if classification.status == DeletionStatus.UNKNOWN_RESPONSE:
            return False  # no improvement (yet) - leave everything untouched

        # Keep the mailbox letter's own understanding in sync with the
        # correction, so "Baker's Dozen understands this as" never
        # disagrees with the company's current status for the same message.
        message_row.classification_status = classification.status
        message_row.classification_confidence = classification.confidence
        message_row.classification_quote = classification.quote

        _apply_reclassification(
            db, company, classification, message_row.body_excerpt, message_row.occurred_at,
            message_row.gmail_message_id, legacy=False,
        )
        db.commit()
        return True

    # No MailMessage row at all - this case predates the mailbox feature's
    # persistence (see _find_legacy_acknowledgment_event). NEVER fabricate
    # one just to satisfy the current schema: the absence is historically
    # accurate, and a fabricated row would misrepresent what the tracker
    # actually captured at the time.
    legacy_event = _find_legacy_acknowledgment_event(db, company)
    if legacy_event is None:
        return False

    quote = (legacy_event.evidence or {}).get("quote", "")
    classification = classifier.classify(quote)
    if classification.status == DeletionStatus.UNKNOWN_RESPONSE:
        return False  # still genuinely unclear even under the current classifier - leave untouched

    _apply_reclassification(
        db, company, classification, quote, legacy_event.occurred_at,
        company.deletion_last_response_message_id, legacy=True,
    )
    db.commit()
    return True


# --- Legacy fallback: pre-mailbox-persistence UNKNOWN_RESPONSE cases ---
#
# Before the mailbox feature added MailMessage rows, a classified reply
# left evidence in exactly two places: Company.deletion_evidence (the
# CURRENT/latest evidence only, overwritten on every new classification)
# and the append-only DeletionEvent audit trail (every classification
# ever recorded, including this one, never overwritten). A genuinely real
# case (a live MALK Organics reply) is stuck as UNKNOWN_RESPONSE from
# that earlier era - reclassify_stale_unknown_response's MailMessage
# lookup can never find it, because there IS no MailMessage row for it
# and never was. That absence is historically accurate, not a bug to
# paper over by fabricating one - so this fallback reads ONLY the
# DeletionEvent audit trail that deletion_response_tracker.py itself
# already wrote at classification time, never anything new.

# The exact set of event types _EVENT_TYPE_FOR_STATUS can ever produce
# for an inbound company-reply classification (see that dict above) -
# anything else (EMAIL_SENT, USER_CONFIRMED, EXECUTION_STARTED/
# EXECUTION_INTERRUPTED, FOLLOWUP_SENT, MAIL_REPLY_SENT,
# USER_ATTESTED_ACTION_COMPLETED, RESPONSE_CHECK_FAILED, FAILED, RETRY,
# THREAD_ASSOCIATED, RESEARCH_*, ...) is never a legacy-reconciliation
# candidate, no matter what its evidence dict happens to contain.
_LEGACY_INBOUND_CLASSIFICATION_EVENT_TYPES = set(_EVENT_TYPE_FOR_STATUS.values())


def _find_legacy_acknowledgment_event(db: Session, company: Company) -> DeletionEvent | None:
    """Finds the ONE historical DeletionEvent that classified THIS exact
    message, from before MailMessage persistence existed - never a guess,
    never simply "the latest event for this company" (which could be
    anything - a send, an attestation, an unrelated later classification
    of a DIFFERENT message). Requires an exact evidence.message_id match
    against the message currently stuck as UNKNOWN_RESPONSE, a safe
    candidate event_type (see _LEGACY_INBOUND_CLASSIFICATION_EVENT_TYPES),
    and a non-empty quote (nothing to classify without one).

    Critically, an event this SAME reconciliation mechanism already
    produced (evidence.reclassified or evidence.legacy_reconciliation) is
    NEVER eligible as a source - a reconciliation-generated audit event
    must never become the historical basis for another reconciliation.
    The top-level UNKNOWN_RESPONSE status guard in
    reclassify_stale_unknown_response already prevents this in the normal
    case (once reclassified, the company is no longer UNKNOWN_RESPONSE),
    but this exclusion is enforced here too, independently, at the
    evidence-selection layer - so this function stays safe to call even
    if that guard were ever bypassed or this function reused elsewhere.

    If more than one genuine (non-reconciliation) event still qualifies
    for the same message_id - shouldn't normally happen, since
    check_company_response records exactly one classification event per
    message, but the real world is the real world - the EARLIEST one
    wins: occurred_at ascending, then id ascending as a stable tie-
    breaker for two events sharing a timestamp. This is the historical
    record of what the company ACTUALLY said first, never a later
    duplicate/retry."""
    target_message_id = company.deletion_last_response_message_id
    if not target_message_id:
        return None
    candidates = (
        db.query(DeletionEvent)
        .filter(
            DeletionEvent.company_id == company.id,
            DeletionEvent.event_type.in_(_LEGACY_INBOUND_CLASSIFICATION_EVENT_TYPES),
        )
        .order_by(DeletionEvent.occurred_at.asc(), DeletionEvent.id.asc())
        .all()
    )
    for event in candidates:
        evidence = event.evidence or {}
        if evidence.get("message_id") != target_message_id:
            continue
        if not (evidence.get("quote") or "").strip():
            continue
        if evidence.get("reclassified") or evidence.get("legacy_reconciliation"):
            continue
        return event
    return None


def process_stale_unknown_responses(db: Session, classifier: ResponseClassifier, limit: int | None = None) -> int:
    """Batch entry point for the background worker - pure local
    reconciliation using already-stored evidence only, no Gmail access at
    all. Returns how many companies were actually reclassified (never
    counts a recheck that changed nothing)."""
    companies = get_companies_with_stale_unknown_response(db, limit)
    return sum(1 for company in companies if reclassify_stale_unknown_response(db, company, classifier))
