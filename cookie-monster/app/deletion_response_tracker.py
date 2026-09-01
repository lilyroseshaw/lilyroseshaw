"""Background response tracking: reads a company's replies to a deletion
request Cookie Monster itself sent, and updates status accordingly.

Privacy/safety rules enforced here:
- Only ever reads the ONE thread stored in Company.deletion_thread_id
  (google_oauth.fetch_thread_messages does a direct thread get, never a
  search/list) - no other part of the inbox is ever touched.
- Message bodies are decoded in-process for classification, then discarded.
  Only a short (~200 char) quote is ever persisted, as event evidence.
- A TRANSIENT check failure (Gmail API error, network error) never changes
  Company.deletion_status - the underlying request status is left exactly
  as it was, and a RESPONSE_CHECK_FAILED event records the failure for the
  backoff policy to retry later. DeletionStatus.FAILED is reserved for a
  genuinely permanent failure (the thread no longer exists at all) - never
  set merely because one poll attempt errored.
- Cookie Monster never auto-replies to a company or sends anything in
  response to VERIFICATION_NEEDED/MORE_INFO_REQUIRED - those just update
  status and evidence for the user to act on themselves in their own Gmail.
"""
import base64
import datetime

from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session

from app import config, google_oauth
from app.deletion_constants import DeletionStatus, EventType
from app.deletion_events import record_event
from app.models import Company
from app.response_classify import ResponseClassifier

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
    return " ".join(soup.get_text(separator=" ").split())


def extract_body_text(message: dict) -> str:
    """Walks a Gmail message payload for text/plain (preferred) or text/html
    content. Returns "" if nothing decodable is found. The caller uses this
    for classification only, in-process, and discards it afterward."""
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
    if "SENT" in message.get("labelIds", []):
        return True
    headers = message.get("payload", {}).get("headers", [])
    from_header = next((h["value"] for h in headers if h.get("name", "").lower() == "from"), "")
    return gmail_address.lower() in from_header.lower()


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

    try:
        messages = google_oauth.fetch_thread_messages(creds, company.deletion_thread_id)
    except HttpError as exc:
        status_code = getattr(getattr(exc, "resp", None), "status", None)
        if status_code == 404:
            # Permanent: the thread itself is gone - tracking has genuinely,
            # meaningfully failed, not just one poll attempt.
            company.deletion_status = DeletionStatus.FAILED
            company.deletion_error = "The tracked Gmail thread could not be found (it may have been deleted)."
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

    new_messages = _select_new_company_messages(messages, gmail_address, company.deletion_last_response_message_id)
    if not new_messages:
        db.commit()
        return

    last_classification = None
    for message in new_messages:
        body_text = extract_body_text(message)  # transient - discarded at end of this loop iteration
        classification = classifier.classify(body_text)
        record_event(
            db, company.id,
            _EVENT_TYPE_FOR_STATUS.get(classification.status, EventType.COMPANY_ACKNOWLEDGED),
            evidence={
                "quote": classification.quote,
                "confidence": classification.confidence,
                "message_id": message.get("id"),
            },
        )
        company.deletion_last_response_message_id = message.get("id")
        last_classification = classification

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
