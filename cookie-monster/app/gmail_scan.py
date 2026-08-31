"""Talks to the Gmail API. Every message fetch uses format="metadata" with an
explicit header allowlist - this app never requests format="full" or "raw",
so it never sees a message body, attachment, or snippet.

Message IDs and raw headers are only ever held in memory for the duration of
a single scan; nothing here writes them to disk. See aggregator.py for what
actually gets persisted.
"""
import datetime
from collections.abc import Iterator

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.classifier import Classification, classify_message

# Search buckets narrow the scan to subjects that plausibly contain evidence,
# instead of pulling the entire mailbox. Keeps API usage and processing down,
# and means messages that clearly aren't evidence are never fetched at all.
SEARCH_QUERIES = [
    'subject:(welcome OR "verify your email" OR "confirm your email" OR "verify your account" OR "account created")',
    'subject:(order OR receipt OR invoice OR shipped OR shipping OR delivered OR "on its way")',
    'subject:("password reset" OR "reset your password" OR "verification code" OR "security code" OR "one-time")',
    'subject:(subscription OR membership OR rewards OR loyalty OR "your plan")',
    'subject:(newsletter OR "% off" OR "exclusive offer" OR unsubscribe)',
    'subject:("support ticket" OR "case #" OR "customer service" OR "customer support")',
]

HEADER_ALLOWLIST = ["From", "Subject", "Date"]

DEFAULT_MAX_MESSAGES = 600


def _build_service(creds: Credentials):
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _collect_candidate_ids(service, max_results: int) -> list[str]:
    message_ids: list[str] = []
    page_token = None

    while len(message_ids) < max_results:
        remaining = max_results - len(message_ids)

        resp = (
            service.users()
            .messages()
            .list(
                userId="me",
                pageToken=page_token,
                maxResults=min(100, remaining),
            )
            .execute()
        )

        messages = resp.get("messages", [])

        for message in messages:
            message_ids.append(message["id"])

        page_token = resp.get("nextPageToken")

        if not page_token or not messages:
            break

    return message_ids


def _headers_dict(message: dict) -> dict[str, str]:
    headers = message.get("payload", {}).get("headers", [])
    return {h["name"]: h["value"] for h in headers if h["name"] in HEADER_ALLOWLIST}


def _parse_date(raw: str | None) -> datetime.datetime:
    if not raw:
        return datetime.datetime.utcnow()
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is not None:
            dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return datetime.datetime.utcnow()


def scan_inbox(
    creds: Credentials, max_results: int = DEFAULT_MAX_MESSAGES
) -> Iterator[tuple[Classification, datetime.datetime]]:
    """Yields (Classification, message_date) for every message that matches
    a known evidence pattern. Everything else is discarded in-process."""
    service = _build_service(creds)
    message_ids = _collect_candidate_ids(service, max_results)

    for msg_id in message_ids:
        message = (
            service.users()
            .messages()
            .get(userId="me", id=msg_id, format="metadata", metadataHeaders=HEADER_ALLOWLIST)
            .execute()
        )
        headers = _headers_dict(message)
        subject = headers.get("Subject", "")
        from_header = headers.get("From", "")
        classification = classify_message(subject, from_header)
        if classification is None:
            continue
        yield classification, _parse_date(headers.get("Date"))
