import base64
import datetime
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config
from app.db import Base
from app.deletion_constants import DeletionStatus, EventType
from app.deletion_response_tracker import (
    check_company_response,
    extract_body_text,
    get_companies_due_for_check,
    process_response_checks,
)
from app.models import Company, DeletionEvent
from app.response_classify import ResponseClassifier


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _company(db, **overrides) -> Company:
    defaults = dict(
        name="Widget Co", domain="widgetco.com", relationship_type="transactional", status="confirmed",
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        deletion_status=DeletionStatus.SUBMITTED, deletion_thread_id="thread123",
    )
    defaults.update(overrides)
    company = Company(**defaults)
    db.add(company)
    db.commit()
    return company


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _msg(msg_id, body_text, from_addr, internal_date, sent=False):
    return {
        "id": msg_id,
        "labelIds": ["SENT"] if sent else ["INBOX"],
        "internalDate": str(internal_date),
        "payload": {
            "headers": [{"name": "From", "value": from_addr}],
            "mimeType": "text/plain",
            "body": {"data": _b64(body_text)},
        },
    }


class _FakeHttpError(HttpError):
    def __init__(self, status):
        self.resp = type("Resp", (), {"status": status})()
        self.content = b"error"
        self.uri = "https://fake"

    def __str__(self):
        return f"HttpError {self.resp.status}"


# --- body extraction ---

def test_extract_body_text_plain():
    msg = _msg("m1", "Hello world", "x@example.com", 1000)
    assert extract_body_text(msg) == "Hello world"


def test_extract_body_text_multipart_prefers_plain_over_html():
    msg = {
        "id": "m1",
        "payload": {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64("<p>Hello <b>world</b></p>")}},
                {"mimeType": "text/plain", "body": {"data": _b64("Hello world plain")}},
            ],
        },
    }
    assert "Hello world plain" in extract_body_text(msg)


def test_extract_body_text_falls_back_to_html():
    msg = {"id": "m1", "payload": {"mimeType": "text/html", "body": {"data": _b64("<p>Only <b>HTML</b> here</p>")}}}
    text = extract_body_text(msg)
    assert "Only" in text and "HTML" in text and "<b>" not in text


def test_extract_body_text_empty_when_nothing_decodable():
    msg = {"id": "m1", "payload": {"mimeType": "application/octet-stream"}}
    assert extract_body_text(msg) == ""


# --- own-message filtering + dedup ---

@pytest.mark.parametrize(
    "from_header",
    [
        "Support <support@widgetco.com> on behalf of me@gmail.com",  # "on behalf of" ticketing pattern
        '"me@gmail.com via Widget Co Support" <support@widgetco.com>',  # Gmail-style "via" reply-list format
        "Privacy Team <privacy@widgetco.com> (me@gmail.com)",  # parenthetical note
    ],
)
def test_real_company_reply_is_never_mistaken_for_own_message(db, from_header):
    """Regression for a real bug: own-message detection used to check
    whether the connected Gmail address appeared ANYWHERE in the raw
    From header string, rather than comparing the header's actual parsed
    address. Several real helpdesk/ticketing senders format a reply's
    From header in ways that legitimately contain the recipient's own
    address as text without the message being from that account at all -
    the substring check silently discarded a genuine, same-thread company
    reply before it ever reached classification."""
    company = _company(db)
    reply = _msg("m2", "We are currently reviewing your request.", from_header, 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_status == DeletionStatus.IN_PROGRESS
    assert company.deletion_last_response_message_id == "m2"
    assert db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count() == 1


def test_own_sent_message_is_never_classified(db):
    company = _company(db)
    sent = _msg("m1", "please delete my data", "me@gmail.com", 1000, sent=True)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[sent]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_status == DeletionStatus.SUBMITTED  # unchanged - nothing new from the company
    assert db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count() == 0


def test_already_processed_message_is_never_reclassified(db):
    company = _company(db, deletion_last_response_message_id="m2")
    reply = _msg("m2", "We received your request.", "privacy@widgetco.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count() == 0


def test_new_reply_is_classified_and_dedup_marker_updated(db):
    company = _company(db)
    reply = _msg("m2", "We are currently reviewing your request.", "privacy@widgetco.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_status == DeletionStatus.IN_PROGRESS
    assert company.deletion_last_response_message_id == "m2"
    assert db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count() == 1


# --- the critical refinement: transient failures never overwrite status ---

def test_transient_network_error_does_not_change_status(db):
    company = _company(db, deletion_status=DeletionStatus.IN_PROGRESS)
    with patch("app.google_oauth.fetch_thread_messages", side_effect=ConnectionError("network down")):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_status == DeletionStatus.IN_PROGRESS  # untouched
    assert company.deletion_response_check_failures == 1
    event = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).one()
    assert event.event_type == EventType.RESPONSE_CHECK_FAILED


def test_transient_http_5xx_does_not_change_status(db):
    company = _company(db, deletion_status=DeletionStatus.SUBMITTED)
    with patch("app.google_oauth.fetch_thread_messages", side_effect=_FakeHttpError(500)):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_status == DeletionStatus.SUBMITTED
    assert company.deletion_response_check_failures == 1


def test_repeated_transient_failures_increment_failure_count(db):
    company = _company(db)
    with patch("app.google_oauth.fetch_thread_messages", side_effect=ConnectionError("down")):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_response_check_failures == 2
    assert company.deletion_status == DeletionStatus.SUBMITTED


def test_successful_check_resets_failure_count(db):
    company = _company(db, deletion_response_check_failures=3)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_response_check_failures == 0


# --- permanent failure: only a genuinely broken thread becomes FAILED ---

def test_permanent_404_marks_failed(db):
    company = _company(db, deletion_status=DeletionStatus.SUBMITTED)
    with patch("app.google_oauth.fetch_thread_messages", side_effect=_FakeHttpError(404)):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_status == DeletionStatus.FAILED
    assert company.deletion_error
    event = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).one()
    assert event.event_type == EventType.FAILED


# --- classification -> status transitions for every state ---

@pytest.mark.parametrize(
    "reply_text,expected_status",
    [
        ("Your data has been deleted from our systems.", DeletionStatus.COMPLETED),
        ("We are unable to fulfill this request.", DeletionStatus.REJECTED),
        ("Please verify your identity to continue.", DeletionStatus.VERIFICATION_NEEDED),
        ("We need additional information to process this.", DeletionStatus.MORE_INFO_REQUIRED),
        ("We have received your request and are reviewing it.", DeletionStatus.IN_PROGRESS),
        ("Thanks for your email!", DeletionStatus.UNKNOWN_RESPONSE),
    ],
)
def test_status_transition_per_classification(db, reply_text, expected_status):
    company = _company(db)
    reply = _msg("m2", reply_text, "privacy@widgetco.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_status == expected_status


def test_completed_sets_completed_at(db):
    company = _company(db)
    reply = _msg("m2", "Your personal data has been deleted.", "privacy@widgetco.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_completed_at is not None


# --- scheduling / backoff ---

def test_terminal_statuses_are_never_due(db):
    _company(db, domain="done.com", deletion_status=DeletionStatus.COMPLETED)
    _company(db, domain="rejected.com", deletion_status=DeletionStatus.REJECTED)
    _company(db, domain="failed.com", deletion_status=DeletionStatus.FAILED)
    due = get_companies_due_for_check(db)
    assert due == []


def test_never_checked_thread_is_immediately_due(db):
    _company(db)
    due = get_companies_due_for_check(db)
    assert len(due) == 1


def test_recently_checked_healthy_thread_is_not_due(db):
    _company(db, deletion_response_checked_at=datetime.datetime.utcnow())
    due = get_companies_due_for_check(db)
    assert due == []


def test_healthy_thread_due_after_min_interval(db):
    checked_at = datetime.datetime.utcnow() - datetime.timedelta(hours=config.RESPONSE_CHECK_MIN_INTERVAL_HOURS + 1)
    _company(db, deletion_response_checked_at=checked_at)
    due = get_companies_due_for_check(db)
    assert len(due) == 1


def test_failing_thread_uses_backoff_not_min_interval(db):
    # Just past the healthy min-interval, but with failures - should NOT be due yet under backoff.
    checked_at = datetime.datetime.utcnow() - datetime.timedelta(hours=config.RESPONSE_CHECK_MIN_INTERVAL_HOURS + 1)
    _company(db, deletion_response_checked_at=checked_at, deletion_response_check_failures=5)
    due = get_companies_due_for_check(db)
    assert due == []


def test_no_tracked_thread_is_never_checked(db):
    _company(db, deletion_thread_id=None)
    assert get_companies_due_for_check(db) == []


# --- data minimization: body text never persisted ---

def test_full_body_text_is_never_stored_in_evidence(db):
    company = _company(db)
    long_body = "We are currently reviewing your request. " + ("secret personal detail " * 50)
    reply = _msg("m2", long_body, "privacy@widgetco.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert long_body not in str(company.deletion_evidence)
    assert len(company.deletion_evidence.get("quote", "")) <= 200


# --- realistic real-world thread shapes (regression coverage for the
# real-Gmail-reply bug report: every test above only ever hands
# fetch_thread_messages a single already-filtered message, never the full
# thread INCLUDING Cookie Monster's own SENT message the way Gmail's
# threads().get() actually returns it) ---

def test_realistic_two_message_thread_finds_the_reply(db):
    """The exact real shape: fetch_thread_messages returns BOTH Cookie
    Monster's own outgoing request (labelIds=["SENT"], sent first) AND the
    company's reply (labelIds=["INBOX"], sent later) in the same thread -
    not just the reply in isolation like every other test in this file."""
    company = _company(db)
    own_sent = _msg("m1", "I am requesting deletion of my personal information.", "me@gmail.com", 1000, sent=True)
    reply = _msg("m2", "We are currently reviewing your request.", "privacy@widgetco.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[own_sent, reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_status == DeletionStatus.IN_PROGRESS
    assert company.deletion_last_response_message_id == "m2"
    assert db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count() == 1


def test_realistic_two_message_thread_out_of_order_from_gmail(db):
    """Gmail does not guarantee messages come back in chronological order -
    _select_new_company_messages must sort by internalDate itself, not
    trust list order."""
    company = _company(db)
    own_sent = _msg("m1", "I am requesting deletion of my personal information.", "me@gmail.com", 1000, sent=True)
    reply = _msg("m2", "We are currently reviewing your request.", "privacy@widgetco.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply, own_sent]):  # reversed
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_status == DeletionStatus.IN_PROGRESS
    assert company.deletion_last_response_message_id == "m2"


def _html_msg(msg_id, html_body, from_addr, internal_date):
    return {
        "id": msg_id,
        "labelIds": ["INBOX"],
        "internalDate": str(internal_date),
        "payload": {
            "headers": [{"name": "From", "value": from_addr}],
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64(html_body)}},
            ],
        },
    }


def test_realistic_html_only_corporate_reply_with_gmail_quote(db):
    """Many real company privacy-team replies are HTML-only (no text/plain
    part at all - common for Zendesk/Salesforce/Outlook-originated mail),
    and Gmail wraps the quoted original request in a
    <div class="gmail_quote"> the reply's own new content sits outside of.
    The company's new words must be extracted and classified; Cookie
    Monster's own quoted request text must never leak into classification."""
    company = _company(db)
    own_sent = _msg("m1", "I am requesting deletion of my personal information pursuant to CCPA.", "me@gmail.com", 1000, sent=True)
    html = (
        "<div dir=\"ltr\">Thanks for reaching out. Before we can process your request, "
        "we need to verify your identity. Please reply with your account email.</div>"
        "<div class=\"gmail_quote\">"
        "<div class=\"gmail_attr\">On Mon, Jan 1, 2024 at 10:00 AM Me &lt;me@gmail.com&gt; wrote:<br></div>"
        "<blockquote class=\"gmail_quote\" style=\"margin:0px 0px 0px 0.8ex;border-left:1px solid #ccc;padding-left:1ex\">"
        "I am requesting deletion of my personal information pursuant to CCPA."
        "</blockquote></div>"
    )
    reply = _html_msg("m2", html, "Privacy Team <privacy@widgetco.com>", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[own_sent, reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_status == DeletionStatus.VERIFICATION_NEEDED
    assert company.deletion_last_response_message_id == "m2"
    quote = company.deletion_evidence["quote"]
    assert "verify your identity" in quote.lower()
    assert "ccpa" not in quote.lower()  # Cookie Monster's own quoted request must never leak in


def test_no_reply_yet_leaves_status_and_marker_untouched(db):
    """Thread contains only Cookie Monster's own request so far - a
    genuine 'nothing new yet', not a bug, and must look identical in
    outcome to a healthy no-op check (no event, no status change)."""
    company = _company(db)
    own_sent = _msg("m1", "I am requesting deletion of my personal information.", "me@gmail.com", 1000, sent=True)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[own_sent]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_status == DeletionStatus.SUBMITTED
    assert company.deletion_last_response_message_id is None
    assert db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count() == 0


# --- process_response_checks batch entry point ---

def test_process_response_checks_processes_multiple_due_companies(db):
    c1 = _company(db, domain="a.com")
    c2 = _company(db, domain="b.com")
    reply = _msg("m2", "We have received your request.", "privacy@a.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        count = process_response_checks(db, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert count == 2
