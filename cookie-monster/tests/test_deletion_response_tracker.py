import base64
import datetime
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import chase_engine, config, mail
from app.db import Base
from app.deletion_constants import DeletionStatus, EventType, WaitingOn
from app.deletion_events import record_event
from app.deletion_response_tracker import (
    CHECK_RESULT_NEW_MESSAGE,
    CHECK_RESULT_NO_CHANGE,
    CHECK_RESULT_RECLASSIFIED,
    _find_legacy_acknowledgment_event,
    check_company_response,
    extract_body_text,
    get_companies_due_for_check,
    get_companies_with_stale_unknown_response,
    process_response_checks,
    process_stale_unknown_responses,
    reclassify_stale_unknown_response,
)
from app.models import Company, DeletionEvent, MailMessage
from app.response_classify import ResponseClassification, ResponseClassifier

MALK_ACKNOWLEDGMENT_TEXT = (
    "Hello Lily, Thanks for reaching out to MALK Organics! We have received "
    "your email and someone from our team will get back to you as soon as "
    "possible. Thank you!"
)


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
    """Modern precedence (see the legacy-cursor-recovery tests further
    down): a message the CURRENT system already processed - meaning a
    MailMessage row exists for it, exactly like the real pipeline always
    creates for every message it examines - must never be reclassified,
    regardless of what a live re-fetch of the thread returns for it."""
    company = _company(db, deletion_last_response_message_id="m2")
    db.add(MailMessage(
        company_id=company.id, direction="inbound", gmail_message_id="m2",
        gmail_thread_id=company.deletion_thread_id, occurred_at=datetime.datetime(2022, 1, 2),
        from_display="privacy@widgetco.com", subject="Re: request", body_excerpt="We received your request.",
    ))
    db.commit()
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


# --- Audit-event precision: UNKNOWN_RESPONSE must never be recorded under
# EventType.COMPANY_ACKNOWLEDGED, which asserts something the classifier
# never established (the live Goop Kitchen case, company 38, surfaced
# this - event 109 recorded COMPANY_ACKNOWLEDGED for a reply the
# classifier genuinely could not place at the time). IN_PROGRESS/SUBMITTED
# keep COMPANY_ACKNOWLEDGED - both genuinely represent the company
# acknowledging the request. ---

def test_unknown_response_records_unclassified_reply_received_not_acknowledged(db):
    company = _company(db)
    reply = _msg("m2", "Thanks for your email!", "privacy@widgetco.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_status == DeletionStatus.UNKNOWN_RESPONSE
    event = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).one()
    assert event.event_type == EventType.UNCLASSIFIED_REPLY_RECEIVED
    assert event.event_type != EventType.COMPANY_ACKNOWLEDGED


def test_in_progress_still_records_company_acknowledged(db):
    """Unlike UNKNOWN_RESPONSE, IN_PROGRESS genuinely represents the
    company acknowledging the request (a ticket/case, "reviewing it",
    etc.) - this mapping is unchanged by the UNKNOWN_RESPONSE fix."""
    company = _company(db)
    reply = _msg("m2", "We are currently reviewing your request.", "privacy@widgetco.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_status == DeletionStatus.IN_PROGRESS
    event = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).one()
    assert event.event_type == EventType.COMPANY_ACKNOWLEDGED


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


# --- Regression: real MALK Organics reply, fresh processing ---
# (see response_classify.py's IN_PROGRESS_PATTERNS - "received your
# request" and "we will get back to you" were too narrow to match this
# real, common helpdesk-auto-reply phrasing)

def test_malk_style_acknowledgment_classifies_in_progress_on_first_processing(db):
    """A brand-new MALK-style acknowledgment must deterministically
    classify as IN_PROGRESS (never UNKNOWN_RESPONSE) the first time it's
    ever processed - proves the classifier gap itself is fixed, not just
    worked around by reconciliation."""
    company = _company(db, deletion_method="EMAIL_REQUEST")
    reply = _msg("m2", MALK_ACKNOWLEDGMENT_TEXT, "hello@malkorganics.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_status == DeletionStatus.IN_PROGRESS
    assert company.waiting_on == WaitingOn.COMPANY
    assert company.next_followup_at is not None  # chase is active


# --- Regression: a previously-processed MALK-style reply stuck as
# UNKNOWN_RESPONSE (classified by the OLD, narrower patterns) must be
# safely reclassifiable using ONLY the already-stored evidence - no new
# Gmail fetch, no duplicate MailMessage/evidence, no resend. ---

def _epoch_ms(dt: datetime.datetime) -> int:
    # Timezone-safe: treats the naive datetime as already-UTC, matching
    # mail._occurred_at's own datetime.utcfromtimestamp reconstruction -
    # avoids .timestamp()'s local-timezone interpretation of naive datetimes.
    return int((dt - datetime.datetime(1970, 1, 1)).total_seconds() * 1000)


def _seed_stale_unknown_response(db, company, gmail_message_id="m2", text=MALK_ACKNOWLEDGMENT_TEXT, occurred_at=None):
    """Reproduces exactly the stuck state a real pre-fix MALK case is in:
    an inbound MailMessage already stored (classified UNKNOWN_RESPONSE
    back when the old patterns were in effect), the company's dedup
    cursor already pointing at it, and deletion_status/evidence already
    set to the old, wrong classification - all WITHOUT going through
    check_company_response, so no Gmail call is ever made to set this up.
    occurred_at controls the message's REAL historical timestamp (default:
    a fixed point far in the past, like a genuinely old stuck case)."""
    occurred_at = occurred_at or datetime.datetime(2024, 1, 1, 0, 0, 0)
    old_classification = ResponseClassification(
        status=DeletionStatus.UNKNOWN_RESPONSE, confidence="low", quote=text[:200], reasons=["no known pattern matched"],
    )
    message = _msg(gmail_message_id, text, "hello@malkorganics.com", _epoch_ms(occurred_at))
    row = mail.record_inbound_mail_message(db, company, message, text, old_classification)
    company.deletion_last_response_message_id = gmail_message_id
    company.deletion_status = DeletionStatus.UNKNOWN_RESPONSE
    company.deletion_evidence = {
        "type": "gmail_reply", "quote": text[:200], "confidence": "low",
        "classified_at": datetime.datetime.utcnow().isoformat(),
    }
    db.commit()
    return row


def test_stale_unknown_response_is_safely_reclassified(db):
    company = _company(db, deletion_method="EMAIL_REQUEST")
    message_row = _seed_stale_unknown_response(db, company)
    existing_message_count = db.query(MailMessage).filter(MailMessage.company_id == company.id).count()
    existing_event_count_before = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count()

    with patch("app.google_oauth.fetch_thread_messages") as mock_fetch:
        changed = reclassify_stale_unknown_response(db, company, ResponseClassifier())

    assert changed is True
    mock_fetch.assert_not_called()  # never re-fetches Gmail - already-stored evidence only

    assert company.deletion_status == DeletionStatus.IN_PROGRESS
    assert company.deletion_evidence["reclassified"] is True
    assert company.waiting_on == WaitingOn.COMPANY
    assert company.next_followup_at is not None  # chase now active, scheduled fresh

    # Never reprocessed as if new, never duplicated:
    assert company.deletion_last_response_message_id == message_row.gmail_message_id
    assert db.query(MailMessage).filter(MailMessage.company_id == company.id).count() == existing_message_count
    assert db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count() == existing_event_count_before + 1

    # The mailbox letter's own understanding is corrected too.
    db.refresh(message_row)
    assert message_row.classification_status == DeletionStatus.IN_PROGRESS


# --- Anchoring reclassification's chase schedule to the REAL historical
# reply timestamp, not the moment of reconciliation - a classifier mistake
# Baker's Dozen made must never hand a company an extra 24 hours. ---

def test_stale_reclassification_of_old_reply_becomes_overdue(db):
    """A reply from 3 days ago, only just reclassified today, must be
    scheduled from ITS real timestamp - landing in the past (overdue),
    not 24h from right now."""
    reply_time = datetime.datetime.utcnow() - datetime.timedelta(days=3)
    company = _company(db, deletion_method="EMAIL_REQUEST")
    _seed_stale_unknown_response(db, company, occurred_at=reply_time)

    reclassify_stale_unknown_response(db, company, ResponseClassifier())

    expected = reply_time + datetime.timedelta(hours=config.FOLLOWUP_INTERVAL_HOURS)
    assert abs((company.next_followup_at - expected).total_seconds()) < 2
    assert company.next_followup_at < datetime.datetime.utcnow()  # already overdue
    assert company in chase_engine.get_companies_due_for_followup(db)


def test_stale_reclassification_never_sends_multiple_catchup_followups(db):
    """However many days overdue, the worker must send exactly ONE
    catch-up follow-up, never one per missed day - the due-check only
    ever asks 'is next_followup_at <= now', it never counts missed
    windows."""
    reply_time = datetime.datetime.utcnow() - datetime.timedelta(days=10)  # very overdue
    company = _company(db, deletion_method="EMAIL_REQUEST")
    _seed_stale_unknown_response(db, company, occurred_at=reply_time)

    reclassify_stale_unknown_response(db, company, ResponseClassifier())

    with patch("app.chase_engine._send_followup_email", return_value={"id": "sent-1", "threadId": "thread123"}) as mock_send:
        sent_count = chase_engine.process_followups(db, creds=MagicMock(), gmail_address="me@gmail.com")

    assert sent_count == 1
    assert mock_send.call_count == 1
    assert company.followup_attempt == 1


def test_stale_reclassification_catchup_send_resumes_normal_24h_cadence(db):
    """After the single overdue catch-up follow-up actually sends, the
    NEXT window is +24h from that real send time - never backdated, never
    still anchored to the original stale reply."""
    reply_time = datetime.datetime.utcnow() - datetime.timedelta(days=5)
    company = _company(db, deletion_method="EMAIL_REQUEST")
    _seed_stale_unknown_response(db, company, occurred_at=reply_time)
    reclassify_stale_unknown_response(db, company, ResponseClassifier())

    send_started = datetime.datetime.utcnow()
    with patch("app.chase_engine._send_followup_email", return_value={"id": "sent-1", "threadId": "thread123"}):
        chase_engine.process_followups(db, creds=MagicMock(), gmail_address="me@gmail.com")
    send_finished = datetime.datetime.utcnow()

    expected_min = send_started + datetime.timedelta(hours=config.FOLLOWUP_INTERVAL_HOURS)
    expected_max = send_finished + datetime.timedelta(hours=config.FOLLOWUP_INTERVAL_HOURS)
    assert expected_min <= company.next_followup_at <= expected_max
    # And it's no longer due - cadence has genuinely resumed, not repeated.
    assert company not in chase_engine.get_companies_due_for_followup(db)


def test_stale_reclassification_of_recent_reply_schedules_normally(db):
    """A reply that's only 2 hours old (reclassified same-day) is NOT yet
    overdue - it schedules a normal future window from its own real
    timestamp, exactly as if it had just been correctly classified live."""
    reply_time = datetime.datetime.utcnow() - datetime.timedelta(hours=2)
    company = _company(db, deletion_method="EMAIL_REQUEST")
    _seed_stale_unknown_response(db, company, occurred_at=reply_time)

    reclassify_stale_unknown_response(db, company, ResponseClassifier())

    expected = reply_time + datetime.timedelta(hours=config.FOLLOWUP_INTERVAL_HOURS)
    assert abs((company.next_followup_at - expected).total_seconds()) < 2
    assert company.next_followup_at > datetime.datetime.utcnow()  # not due yet
    assert company not in chase_engine.get_companies_due_for_followup(db)


def test_reclassification_is_noop_when_classifier_still_uncertain(db):
    """A message that genuinely still doesn't match anything must NOT be
    touched - no fabricated status, no wasted event, no chase state
    invented off nothing."""
    company = _company(db, deletion_method="EMAIL_REQUEST")
    _seed_stale_unknown_response(db, company, text="Thanks for your email!")
    events_before = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count()

    changed = reclassify_stale_unknown_response(db, company, ResponseClassifier())

    assert changed is False
    assert company.deletion_status == DeletionStatus.UNKNOWN_RESPONSE
    assert company.waiting_on is None
    assert db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count() == events_before


def test_reclassification_ignores_companies_with_other_statuses(db):
    """Only genuinely-stuck UNKNOWN_RESPONSE cases are candidates - a
    company sitting in any other status (even one a human might disagree
    with) is never silently re-derived."""
    _company(db, deletion_method="EMAIL_REQUEST", deletion_status=DeletionStatus.IN_PROGRESS, domain="already-fine.com")
    due = get_companies_with_stale_unknown_response(db)
    assert due == []


def test_get_companies_with_stale_unknown_response_finds_the_stuck_case(db):
    company = _company(db, deletion_method="EMAIL_REQUEST")
    _seed_stale_unknown_response(db, company)
    due = get_companies_with_stale_unknown_response(db)
    assert due == [company]


def test_process_stale_unknown_responses_batch_entry_point(db):
    c1 = _company(db, deletion_method="EMAIL_REQUEST", domain="fixable.com")
    _seed_stale_unknown_response(db, c1, gmail_message_id="fixable-1")
    c2 = _company(db, deletion_method="EMAIL_REQUEST", domain="still-unclear.com")
    _seed_stale_unknown_response(db, c2, gmail_message_id="unclear-1", text="Thanks!")

    count = process_stale_unknown_responses(db, ResponseClassifier())

    assert count == 1
    assert c1.deletion_status == DeletionStatus.IN_PROGRESS
    assert c2.deletion_status == DeletionStatus.UNKNOWN_RESPONSE  # untouched, still genuinely unclear


# =========================================================================
# Legacy fallback: real pre-mailbox-persistence UNKNOWN_RESPONSE cases
# (the real MALK Organics case - company 55 - has a stored
# deletion_last_response_message_id, a historical COMPANY_ACKNOWLEDGED
# DeletionEvent carrying the exact quote, and ZERO MailMessage rows at
# all, because it was classified before MailMessage persistence existed.
# The MailMessage-based reconciliation above can never find it - not a
# bug, an accurate reflection of what the tracker captured at the time.)
# =========================================================================

def _seed_legacy_unknown_response(
    db, company, message_id="legacy-m1", text=MALK_ACKNOWLEDGMENT_TEXT,
    event_type=EventType.COMPANY_ACKNOWLEDGED, occurred_at=None, quote=None,
):
    """Reproduces the EXACT real MALK legacy shape: a historical
    DeletionEvent recorded before MailMessage persistence existed, with
    NO corresponding MailMessage row at all - the absence is the point,
    never something this helper (or the code under test) papers over."""
    quote = text[:200] if quote is None else quote
    event = record_event(
        db, company.id, event_type,
        evidence={"quote": quote, "confidence": "low", "message_id": message_id},
    )
    if occurred_at is not None:
        event.occurred_at = occurred_at
    company.deletion_last_response_message_id = message_id
    company.deletion_status = DeletionStatus.UNKNOWN_RESPONSE
    company.deletion_evidence = {
        "type": "gmail_reply", "quote": quote, "confidence": "low",
        "classified_at": datetime.datetime.utcnow().isoformat(),
    }
    db.commit()
    return event


# --- A: exact real MALK legacy shape ---

def test_legacy_reconciliation_reclassifies_real_malk_shape(db):
    reply_time = datetime.datetime.utcnow() - datetime.timedelta(days=3)
    company = _company(db, deletion_method="EMAIL_REQUEST")
    _seed_legacy_unknown_response(db, company, message_id="1a068922612316c0", occurred_at=reply_time)
    assert db.query(MailMessage).filter(MailMessage.company_id == company.id).count() == 0

    changed = reclassify_stale_unknown_response(db, company, ResponseClassifier())

    assert changed is True
    assert company.deletion_status == DeletionStatus.IN_PROGRESS
    assert company.waiting_on == WaitingOn.COMPANY
    assert company.deletion_evidence["reclassified"] is True
    assert company.deletion_evidence["legacy_reconciliation"] is True
    assert company.deletion_evidence["source_message_id"] == "1a068922612316c0"

    expected = reply_time + datetime.timedelta(hours=config.FOLLOWUP_INTERVAL_HOURS)
    assert abs((company.next_followup_at - expected).total_seconds()) < 2
    assert company.next_followup_at < datetime.datetime.utcnow()  # already overdue

    # Cursor untouched, and still no MailMessage fabricated:
    assert company.deletion_last_response_message_id == "1a068922612316c0"
    assert db.query(MailMessage).filter(MailMessage.company_id == company.id).count() == 0


# --- B: matching legacy event exists but message_id differs -> no-op ---

def test_legacy_reconciliation_ignores_event_with_different_message_id(db):
    company = _company(db, deletion_method="EMAIL_REQUEST")
    _seed_legacy_unknown_response(db, company, message_id="other-message-id")
    company.deletion_last_response_message_id = "the-actual-cursor-message-id"
    db.commit()

    changed = reclassify_stale_unknown_response(db, company, ResponseClassifier())
    assert changed is False
    assert company.deletion_status == DeletionStatus.UNKNOWN_RESPONSE
    assert company.waiting_on is None


# --- C: legacy event has no quote -> no-op ---

def test_legacy_reconciliation_ignores_event_without_quote(db):
    company = _company(db, deletion_method="EMAIL_REQUEST")
    _seed_legacy_unknown_response(db, company, message_id="m-no-quote", quote="")

    changed = reclassify_stale_unknown_response(db, company, ResponseClassifier())
    assert changed is False
    assert company.deletion_status == DeletionStatus.UNKNOWN_RESPONSE


# --- D: arbitrary/outbound event with matching-looking evidence -> no-op ---

def test_legacy_reconciliation_ignores_non_candidate_event_types(db):
    """An EMAIL_SENT event that happens to carry a matching message_id and
    a quote-shaped field must never be mistaken for an inbound-reply
    classification - only the exact set of event types
    check_company_response itself ever uses for a classified reply are
    eligible candidates."""
    company = _company(db, deletion_method="EMAIL_REQUEST", deletion_last_response_message_id="m-outbound")
    company.deletion_status = DeletionStatus.UNKNOWN_RESPONSE
    db.commit()
    record_event(
        db, company.id, EventType.EMAIL_SENT,
        evidence={"message_id": "m-outbound", "quote": MALK_ACKNOWLEDGMENT_TEXT, "gmail_message_id": "m-outbound"},
    )
    db.commit()

    changed = reclassify_stale_unknown_response(db, company, ResponseClassifier())
    assert changed is False
    assert company.deletion_status == DeletionStatus.UNKNOWN_RESPONSE


# --- E: legacy quote remains genuinely UNKNOWN -> no-op ---

def test_legacy_reconciliation_noop_when_still_unknown(db):
    company = _company(db, deletion_method="EMAIL_REQUEST")
    _seed_legacy_unknown_response(db, company, message_id="m-still-unclear", text="Thanks for your email!")

    changed = reclassify_stale_unknown_response(db, company, ResponseClassifier())
    assert changed is False
    assert company.deletion_status == DeletionStatus.UNKNOWN_RESPONSE


# --- F: existing current MailMessage path still works unchanged, and is
# always preferred over the legacy fallback when both exist ---

def test_message_row_path_is_preferred_over_legacy_event(db):
    company = _company(db, deletion_method="EMAIL_REQUEST")
    message_row = _seed_stale_unknown_response(db, company, gmail_message_id="dual-1")
    # A legacy-looking event for the SAME message id, with DIFFERENT text -
    # if the legacy path won by mistake, this text would end up as the quote.
    record_event(
        db, company.id, EventType.COMPANY_ACKNOWLEDGED,
        evidence={"message_id": "dual-1", "quote": "DIFFERENT TEXT should never be used"},
    )
    db.commit()

    changed = reclassify_stale_unknown_response(db, company, ResponseClassifier())

    assert changed is True
    assert "DIFFERENT TEXT" not in str(company.deletion_evidence)
    assert company.deletion_evidence.get("legacy_reconciliation") is None
    db.refresh(message_row)
    assert message_row.classification_status == DeletionStatus.IN_PROGRESS


# --- G: reconciliation run twice -> one transition, one new audit event,
# no duplicates (idempotent even on a direct repeated call, not just via
# the batch query's own UNKNOWN_RESPONSE filter) ---

def test_legacy_reconciliation_is_idempotent_across_repeated_calls(db):
    company = _company(db, deletion_method="EMAIL_REQUEST")
    _seed_legacy_unknown_response(db, company, message_id="m-idempotent")
    events_before = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count()  # the original legacy event

    first = reclassify_stale_unknown_response(db, company, ResponseClassifier())
    assert first is True
    events_after_first = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count()
    assert events_after_first == events_before + 1

    second = reclassify_stale_unknown_response(db, company, ResponseClassifier())
    assert second is False  # no longer UNKNOWN_RESPONSE - not eligible anymore
    events_after_second = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count()
    assert events_after_second == events_after_first  # no duplicate audit event

    # Also exercise the actual batch entry point the worker uses:
    third_batch_count = process_stale_unknown_responses(db, ResponseClassifier())
    assert third_batch_count == 0
    assert db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count() == events_after_first


# --- H: existing non-UNKNOWN case -> untouched, even on a direct call ---

def test_legacy_reconciliation_direct_call_on_non_unknown_company_is_noop(db):
    company = _company(
        db, deletion_method="EMAIL_REQUEST", deletion_status=DeletionStatus.IN_PROGRESS,
        deletion_last_response_message_id="whatever",
    )
    db.commit()

    changed = reclassify_stale_unknown_response(db, company, ResponseClassifier())
    assert changed is False
    assert company.deletion_status == DeletionStatus.IN_PROGRESS


# --- I: legacy old reply overdue -> at most ONE catch-up follow-up, then
# +24h from actual successful send ---

def test_legacy_reconciliation_overdue_sends_one_catchup_then_resumes_cadence(db):
    reply_time = datetime.datetime.utcnow() - datetime.timedelta(days=6)
    company = _company(db, deletion_method="EMAIL_REQUEST")
    _seed_legacy_unknown_response(db, company, message_id="m-catchup", occurred_at=reply_time)
    reclassify_stale_unknown_response(db, company, ResponseClassifier())
    assert company in chase_engine.get_companies_due_for_followup(db)

    send_started = datetime.datetime.utcnow()
    with patch("app.chase_engine._send_followup_email", return_value={"id": "sent-1", "threadId": "thread123"}) as mock_send:
        sent_count = chase_engine.process_followups(db, creds=MagicMock(), gmail_address="me@gmail.com")
    send_finished = datetime.datetime.utcnow()

    assert sent_count == 1
    assert mock_send.call_count == 1  # exactly one catch-up, never one per missed day
    assert company.followup_attempt == 1

    expected_min = send_started + datetime.timedelta(hours=config.FOLLOWUP_INTERVAL_HOURS)
    expected_max = send_finished + datetime.timedelta(hours=config.FOLLOWUP_INTERVAL_HOURS)
    assert expected_min <= company.next_followup_at <= expected_max
    assert company not in chase_engine.get_companies_due_for_followup(db)  # cadence resumed, not repeated


# --- Provenance exclusion: a reconciliation-generated audit event must
# NEVER become eligible as the historical source for another
# reconciliation, even alongside the top-level UNKNOWN_RESPONSE status
# guard - enforced independently at the evidence-selection layer. ---

def test_legacy_finder_excludes_reconciliation_generated_events(db):
    company = _company(db, deletion_method="EMAIL_REQUEST")
    genuine = record_event(
        db, company.id, EventType.COMPANY_ACKNOWLEDGED,
        evidence={"message_id": "m-provenance", "quote": MALK_ACKNOWLEDGMENT_TEXT[:200], "confidence": "low"},
    )
    genuine.occurred_at = datetime.datetime.utcnow() - datetime.timedelta(days=5)
    db.commit()
    # A later, reconciliation-generated event for the SAME message id -
    # must never be treated as a legitimate historical source, even
    # though it otherwise looks like a perfectly good candidate (same
    # message_id, safe event type, non-empty quote).
    reconciled = record_event(
        db, company.id, EventType.COMPANY_ACKNOWLEDGED,
        evidence={
            "message_id": "m-provenance", "quote": "SHOULD NEVER BE SELECTED", "confidence": "high",
            "reclassified": True, "legacy_reconciliation": True,
        },
    )
    reconciled.occurred_at = datetime.datetime.utcnow() - datetime.timedelta(days=1)
    db.commit()

    company.deletion_last_response_message_id = "m-provenance"
    company.deletion_status = DeletionStatus.UNKNOWN_RESPONSE
    company.deletion_evidence = {"type": "gmail_reply", "quote": "x", "confidence": "low"}
    db.commit()

    found = _find_legacy_acknowledgment_event(db, company)
    assert found is not None
    assert found.id == genuine.id
    assert found.evidence["quote"] == MALK_ACKNOWLEDGMENT_TEXT[:200]

    # End-to-end: the reconciliation itself must be driven by the
    # genuine text, never the reconciliation-generated one.
    changed = reclassify_stale_unknown_response(db, company, ResponseClassifier())
    assert changed is True
    assert "SHOULD NEVER BE SELECTED" not in str(company.deletion_evidence)


# --- Deterministic ordering: if multiple genuine eligible events somehow
# exist for the same message_id, the EARLIEST one wins - occurred_at
# ascending, then id ascending as a stable tie-breaker. ---

def test_legacy_finder_picks_earliest_genuine_event_deterministically(db):
    company = _company(db, deletion_method="EMAIL_REQUEST")
    earlier = record_event(
        db, company.id, EventType.COMPANY_ACKNOWLEDGED,
        evidence={"message_id": "m-dup", "quote": "EARLIEST GENUINE TEXT", "confidence": "low"},
    )
    earlier.occurred_at = datetime.datetime.utcnow() - datetime.timedelta(days=10)
    db.commit()
    later = record_event(
        db, company.id, EventType.COMPANY_ACKNOWLEDGED,
        evidence={"message_id": "m-dup", "quote": "LATER GENUINE TEXT", "confidence": "low"},
    )
    later.occurred_at = datetime.datetime.utcnow() - datetime.timedelta(days=2)
    db.commit()

    company.deletion_last_response_message_id = "m-dup"
    company.deletion_status = DeletionStatus.UNKNOWN_RESPONSE
    company.deletion_evidence = {"type": "gmail_reply", "quote": "x", "confidence": "low"}
    db.commit()

    found = _find_legacy_acknowledgment_event(db, company)
    assert found is not None
    assert found.id == earlier.id
    assert found.evidence["quote"] == "EARLIEST GENUINE TEXT"


def test_legacy_finder_breaks_occurred_at_tie_by_id(db):
    """Two genuine events sharing the exact same occurred_at (possible
    with coarse timestamp precision) must still resolve deterministically
    - lower id (inserted first) wins, never database/query-order luck."""
    company = _company(db, deletion_method="EMAIL_REQUEST")
    same_time = datetime.datetime.utcnow() - datetime.timedelta(days=3)
    first = record_event(
        db, company.id, EventType.COMPANY_ACKNOWLEDGED,
        evidence={"message_id": "m-tie", "quote": "FIRST BY ID", "confidence": "low"},
    )
    first.occurred_at = same_time
    db.commit()
    second = record_event(
        db, company.id, EventType.COMPANY_ACKNOWLEDGED,
        evidence={"message_id": "m-tie", "quote": "SECOND BY ID", "confidence": "low"},
    )
    second.occurred_at = same_time
    db.commit()
    assert first.id < second.id

    company.deletion_last_response_message_id = "m-tie"
    company.deletion_status = DeletionStatus.UNKNOWN_RESPONSE
    company.deletion_evidence = {"type": "gmail_reply", "quote": "x", "confidence": "low"}
    db.commit()

    found = _find_legacy_acknowledgment_event(db, company)
    assert found is not None
    assert found.id == first.id


# =========================================================================
# Legacy-cursor recovery via the LIVE check path (check_company_response
# itself) - a second, distinct legacy bug from the MALK UNKNOWN_RESPONSE
# case above. The real Goop Kitchen case (company 38): deletion_status
# VERIFICATION_NEEDED, a stored deletion_last_response_message_id, ZERO
# MailMessage rows (predates mailbox persistence), and evidence that was
# corrupted at classification time (a fragment of Cookie Monster's OWN
# outgoing request text, not the company's real words). Re-fetching the
# SAME tracked thread and re-examining the message AT the cursor with
# today's extraction/classification - not just messages strictly after
# it - is what recovers this, entirely within check_company_response's
# existing pipeline (see _select_new_company_messages's include_cursor).
# =========================================================================

GOOP_ACCOUNT_CLOSED_TEXT = (
    "Thank you for reaching out. We've already deactivated the gK Insider account "
    "associated with your email as requested.\n\n"
    "Please rest assured that we take data privacy very seriously. Your information "
    "is protected, handled securely, and never shared publicly.\n\n"
    "We hope this information helps! Please let us know if you have any questions "
    "or concerns. We're always happy to help!"
)


def _seed_legacy_verification_needed_cursor(db, company, message_id="goop-m1"):
    """Reproduces the real Goop Kitchen shape: a stored cursor + corrupted
    VERIFICATION_NEEDED evidence (a fragment of Cookie Monster's own
    outgoing request, matched by "identity verification" in old, since-
    fixed quote-extraction) - and, critically, ZERO MailMessage rows,
    exactly like a case that predates mailbox persistence."""
    corrupted_quote = (
        "ructions for\n> completing any required identity verification.\n>\n"
        "> This request relates to the account/contact associated with:\n> lilyrose"
    )
    event = record_event(
        db, company.id, EventType.VERIFICATION_REQUESTED,
        evidence={"quote": corrupted_quote, "confidence": "low", "message_id": message_id},
    )
    event.occurred_at = datetime.datetime.utcnow() - datetime.timedelta(days=2)
    company.deletion_last_response_message_id = message_id
    company.deletion_status = DeletionStatus.VERIFICATION_NEEDED
    company.deletion_evidence = {
        "type": "gmail_reply", "quote": corrupted_quote, "confidence": "low",
        "classified_at": datetime.datetime.utcnow().isoformat(),
    }
    db.commit()
    return event


def test_legacy_cursor_recovery_reclassifies_real_goop_shape(db):
    company = _company(db, deletion_method="EMAIL_REQUEST", deletion_status=DeletionStatus.VERIFICATION_NEEDED)
    _seed_legacy_verification_needed_cursor(db, company, message_id="goop-m1")
    assert db.query(MailMessage).filter(MailMessage.company_id == company.id).count() == 0

    # A live re-fetch of the SAME tracked thread now correctly returns the
    # real message content at that same message id (today's fixed
    # extraction/quote-stripping, not the old corrupted capture).
    reply_time = _epoch_ms(datetime.datetime.utcnow() - datetime.timedelta(days=2))
    reply = _msg("goop-m1", GOOP_ACCOUNT_CLOSED_TEXT, "hello@goopkitchen.com", reply_time)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    assert company.deletion_status == DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED
    assert company.deletion_status != DeletionStatus.COMPLETED
    assert company.deletion_completed_at is None
    assert company.waiting_on == WaitingOn.COMPANY
    assert company.next_followup_at is not None
    assert company.deletion_evidence["legacy_cursor_recovered"] is True

    # Cursor unchanged (same message, just correctly reprocessed) - never
    # reset, never pointed at something else:
    assert company.deletion_last_response_message_id == "goop-m1"
    # No duplicate original deletion request - this mechanism never sends
    # anything; only a same-thread re-read.
    assert db.query(MailMessage).filter(MailMessage.company_id == company.id, MailMessage.direction == "outbound").count() == 0
    # Exactly one MailMessage row now exists (first time this company has
    # ever had one) - never duplicated on top of it.
    assert db.query(MailMessage).filter(MailMessage.company_id == company.id).count() == 1
    event = (
        db.query(DeletionEvent)
        .filter(DeletionEvent.company_id == company.id, DeletionEvent.event_type == EventType.ACCOUNT_CLOSED_DATA_UNVERIFIED)
        .one()
    )
    assert event.evidence["legacy_cursor_recovered"] is True
    # The old, corrupted VERIFICATION_REQUESTED event is untouched, still there:
    assert db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id, DeletionEvent.event_type == EventType.VERIFICATION_REQUESTED).count() == 1


def test_legacy_cursor_recovery_is_idempotent_across_repeated_checks(db):
    """Once the first live re-fetch creates the MailMessage row, the
    legacy-recovery boundary is gone for good - a second identical tick
    must behave as a completely normal 'nothing new' check, never
    re-triggering recovery or duplicating anything."""
    company = _company(db, deletion_method="EMAIL_REQUEST", deletion_status=DeletionStatus.VERIFICATION_NEEDED)
    _seed_legacy_verification_needed_cursor(db, company, message_id="goop-m2")
    reply_time = _epoch_ms(datetime.datetime.utcnow() - datetime.timedelta(days=2))
    reply = _msg("goop-m2", GOOP_ACCOUNT_CLOSED_TEXT, "hello@goopkitchen.com", reply_time)

    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_status == DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED
    events_after_first = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count()
    mailmessages_after_first = db.query(MailMessage).filter(MailMessage.company_id == company.id).count()

    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    assert company.deletion_status == DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED  # unchanged, not re-derived
    assert db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count() == events_after_first
    assert db.query(MailMessage).filter(MailMessage.company_id == company.id).count() == mailmessages_after_first


def test_legacy_cursor_recovery_fails_closed_when_still_ambiguous(db):
    """If the re-extracted real content genuinely doesn't match any
    known pattern, this must fail closed to UNKNOWN_RESPONSE - never
    invent a more confident status - while still creating the MailMessage
    row so the company isn't stuck re-triggering recovery forever."""
    company = _company(db, deletion_method="EMAIL_REQUEST", deletion_status=DeletionStatus.VERIFICATION_NEEDED)
    _seed_legacy_verification_needed_cursor(db, company, message_id="goop-m3")
    reply_time = _epoch_ms(datetime.datetime.utcnow() - datetime.timedelta(days=2))
    reply = _msg("goop-m3", "Thanks for your email!", "hello@goopkitchen.com", reply_time)

    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    assert company.deletion_status == DeletionStatus.UNKNOWN_RESPONSE
    assert db.query(MailMessage).filter(MailMessage.company_id == company.id).count() == 1


def test_legacy_cursor_recovery_never_triggers_once_a_mailmessage_row_exists(db):
    """Modern, MailMessage-backed state always takes precedence - even a
    company with exactly the same corrupted-looking VERIFICATION_NEEDED
    status and a cursor set must NOT be reprocessed once a MailMessage row
    already exists for that message id."""
    company = _company(db, deletion_method="EMAIL_REQUEST", deletion_status=DeletionStatus.VERIFICATION_NEEDED)
    event = _seed_legacy_verification_needed_cursor(db, company, message_id="goop-m4")
    db.add(MailMessage(
        company_id=company.id, direction="inbound", gmail_message_id="goop-m4",
        gmail_thread_id=company.deletion_thread_id, occurred_at=event.occurred_at,
        from_display="hello@goopkitchen.com", subject="Re: request", body_excerpt=event.evidence["quote"],
    ))
    db.commit()

    reply_time = _epoch_ms(datetime.datetime.utcnow() - datetime.timedelta(days=2))
    reply = _msg("goop-m4", GOOP_ACCOUNT_CLOSED_TEXT, "hello@goopkitchen.com", reply_time)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    assert company.deletion_status == DeletionStatus.VERIFICATION_NEEDED  # untouched
    assert db.query(MailMessage).filter(MailMessage.company_id == company.id).count() == 1  # the seeded row only


def test_legacy_cursor_recovery_never_treats_own_sent_message_as_recoverable(db):
    """include_cursor only widens WHICH position in the thread counts as
    'unseen' - it never bypasses _is_own_message filtering. If the
    message living at the cursor position is genuinely one Cookie Monster
    itself sent (e.g. the cursor was corrupted to point at the wrong
    entry), it must still be excluded, exactly like any other own
    message - never misread as a company reply."""
    company = _company(db, deletion_method="EMAIL_REQUEST", deletion_status=DeletionStatus.VERIFICATION_NEEDED)
    _seed_legacy_verification_needed_cursor(db, company, message_id="own-sent-1")
    own_sent = _msg("own-sent-1", "I am requesting deletion of my personal information.", "me@gmail.com", 1000, sent=True)

    with patch("app.google_oauth.fetch_thread_messages", return_value=[own_sent]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    assert company.deletion_status == DeletionStatus.VERIFICATION_NEEDED  # untouched - own message, never reclassified
    assert db.query(MailMessage).filter(MailMessage.company_id == company.id).count() == 0
    # Only the original (pre-seeded) corrupted event exists - no new
    # reconciliation event was ever recorded for an own message.
    assert db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count() == 1


def test_legacy_cursor_recovery_does_not_regress_malk_style_live_check(db):
    """The same general mechanism, applied to an UNKNOWN_RESPONSE company
    with zero MailMessage rows (the MALK shape), must ALSO recover
    correctly via the LIVE check path - complementing, not conflicting
    with, the dedicated offline reclassify_stale_unknown_response
    mechanism preserved above."""
    company = _company(db, deletion_method="EMAIL_REQUEST", deletion_status=DeletionStatus.UNKNOWN_RESPONSE)
    event = record_event(
        db, company.id, EventType.COMPANY_ACKNOWLEDGED,
        evidence={"quote": MALK_ACKNOWLEDGMENT_TEXT[:200], "confidence": "low", "message_id": "malk-live-1"},
    )
    event.occurred_at = datetime.datetime.utcnow() - datetime.timedelta(days=1)
    company.deletion_last_response_message_id = "malk-live-1"
    company.deletion_evidence = {"type": "gmail_reply", "quote": MALK_ACKNOWLEDGMENT_TEXT[:200], "confidence": "low"}
    db.commit()
    assert db.query(MailMessage).filter(MailMessage.company_id == company.id).count() == 0

    reply_time = _epoch_ms(datetime.datetime.utcnow() - datetime.timedelta(days=1))
    reply = _msg("malk-live-1", MALK_ACKNOWLEDGMENT_TEXT, "hello@malkorganics.com", reply_time)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    assert company.deletion_status == DeletionStatus.IN_PROGRESS
    assert company.waiting_on == WaitingOn.COMPANY
    assert db.query(MailMessage).filter(MailMessage.company_id == company.id).count() == 1


def test_legacy_cursor_recovery_schedules_chase_from_real_historical_timestamp(db):
    """Same 'no free 24 hours' rule as the offline reconciliation - the
    chase schedule is anchored to the message's REAL Gmail internalDate,
    not the moment this recovery happens to run."""
    reply_dt = datetime.datetime.utcnow() - datetime.timedelta(days=4)
    company = _company(db, deletion_method="EMAIL_REQUEST", deletion_status=DeletionStatus.VERIFICATION_NEEDED)
    _seed_legacy_verification_needed_cursor(db, company, message_id="goop-m5")
    reply_time_ms = int((reply_dt - datetime.datetime(1970, 1, 1)).total_seconds() * 1000)
    reply = _msg("goop-m5", GOOP_ACCOUNT_CLOSED_TEXT, "hello@goopkitchen.com", reply_time_ms)

    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    expected = reply_dt + datetime.timedelta(hours=config.FOLLOWUP_INTERVAL_HOURS)
    assert abs((company.next_followup_at - expected).total_seconds()) < 2
    assert company.next_followup_at < datetime.datetime.utcnow()  # already overdue
    assert company in chase_engine.get_companies_due_for_followup(db)


# --- Follow-up template / chase behavior for ACCOUNT_CLOSED_DATA_UNVERIFIED ---

def test_account_closed_data_unverified_followup_asks_about_data_deletion(db):
    company = _company(
        db, deletion_method="EMAIL_REQUEST", deletion_status=DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED,
        waiting_on=WaitingOn.COMPANY, next_followup_at=datetime.datetime(2020, 1, 1),
    )
    with patch("app.chase_engine._send_followup_email", return_value={"id": "sent-1", "threadId": "thread123"}) as mock_send:
        sent = chase_engine.process_followups(db, creds=MagicMock(), gmail_address="me@gmail.com")
    assert sent == 1
    body = mock_send.call_args.args[3]
    assert "personal data" in body.lower() or "personal information" in body.lower()
    assert "retained" in body.lower()
    assert "attempt" not in body.lower()  # internal counter never shown to the company


# --- Regression: the EXACT live Goop Kitchen reply, in a normal (non-
# legacy) modern check - end-to-end through check_company_response AND
# chase_engine, exercising the classifier fix for the mid-sentence line
# wrap + curly apostrophe that caused the live miss (commit 00553b7
# produced UNKNOWN_RESPONSE for this exact body instead of
# ACCOUNT_CLOSED_DATA_UNVERIFIED). No network access and no live-DB
# manipulation - a mocked single-thread fetch and an in-memory sqlite db,
# same as every other test in this module. ---

GOOP_LIVE_REPLY_EXACT = (
    "Hi Lily,\n\n"
    "Thank you for reaching out.  We’ve already deactivated the gK Insider\n"
    "account associated with lilyroseshaw@gmail.com as requested.\n\n"
    " Please rest assured that we take data privacy very seriously. Your\n"
    "information is protected, handled securely, and never shared publicly.\n\n"
    "We hope this information helps! Please let us know if you have any\n"
    "questions or concerns. We're always happy to help!\n\n"
    "Best,\n\n"
    "In Your Service | Guest Experience Team\n\n"
    "p: 310.954.1286\n\n"
    "goopkitchen.com @goopkitchen <https://instagram.com/goopkitchen>"
)


def test_real_goop_live_reply_classifies_and_schedules_correct_followup(db):
    company = _company(
        db, deletion_method="EMAIL_REQUEST", deletion_status=DeletionStatus.SUBMITTED,
        deletion_thread_id="thread123",
    )
    reply_time = _epoch_ms(datetime.datetime.utcnow() - datetime.timedelta(hours=2))
    reply = _msg("1a05f940a5f400f3", GOOP_LIVE_REPLY_EXACT, "hello@goopkitchen.com", reply_time)

    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    assert company.deletion_status == DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED
    assert company.deletion_status != DeletionStatus.UNKNOWN_RESPONSE
    assert company.deletion_completed_at is None
    assert company.waiting_on == WaitingOn.COMPANY
    assert company.next_followup_at is not None
    company.next_followup_at = datetime.datetime(2020, 1, 1)  # force it due, without sending anything yet
    db.commit()

    with patch("app.chase_engine._send_followup_email", return_value={"id": "sent-1", "threadId": "thread123"}) as mock_send:
        sent = chase_engine.process_followups(db, creds=MagicMock(), gmail_address="me@gmail.com")
    assert sent == 1
    body = mock_send.call_args.args[3]
    assert "personal data" in body.lower() or "personal information" in body.lower()
    assert "retained" in body.lower()


# =========================================================================
# Wiring reclassify_stale_unknown_response into the LIVE check_company_response
# path (a real gap a live check surfaced: a company's persisted
# UNKNOWN_RESPONSE MailMessage is never re-examined by a plain "Check for
# reply" click, since the message is already behind the dedup cursor and
# the manual button only ever looked for genuinely NEW content). Every
# test below uses a fabricated company (Widget Co, via _company()) and
# fabricated wording - MALK/Goop only ever appear as their own separate,
# already-existing regression fixtures elsewhere in this file. Nothing
# here keys off a company name, domain, or message id.
# =========================================================================

def _seed_stale_unknown_response_for(db, company, gmail_message_id, text, occurred_at=None):
    """Same shape as _seed_stale_unknown_response, but for an arbitrary
    already-created company and arbitrary text - lets these tests seed a
    persisted-but-outdated UNKNOWN_RESPONSE classification for any
    fabricated wording, not just the MALK fixture text."""
    occurred_at = occurred_at or datetime.datetime(2024, 1, 1)
    old_classification = ResponseClassification(
        status=DeletionStatus.UNKNOWN_RESPONSE, confidence="low", quote=text[:200], reasons=["no known pattern matched"],
    )
    message = _msg(gmail_message_id, text, "privacy@widgetco.com", _epoch_ms(occurred_at))
    mail.record_inbound_mail_message(db, company, message, text, old_classification)
    company.deletion_last_response_message_id = gmail_message_id
    company.deletion_status = DeletionStatus.UNKNOWN_RESPONSE
    company.deletion_evidence = {
        "type": "gmail_reply", "quote": text[:200], "confidence": "low",
        "classified_at": datetime.datetime.utcnow().isoformat(),
    }
    db.commit()
    return message


def test_manual_check_reclassifies_persisted_unknown_response_with_no_new_gmail_content(db):
    """A plain 'Check for reply' with NOTHING new in the thread must still
    re-examine an already-persisted UNKNOWN_RESPONSE using today's
    classifier, entirely from the stored MailMessage.body_excerpt - the
    live Gmail fetch here returns ONLY the same already-known message,
    proving no broader read is needed or used."""
    company = _company(db, deletion_method="EMAIL_REQUEST")
    old_message = _seed_stale_unknown_response_for(
        db, company, "m2", "We have deactivated your account as requested.",
    )
    assert db.query(MailMessage).filter(MailMessage.company_id == company.id).count() == 1

    with patch("app.google_oauth.fetch_thread_messages", return_value=[old_message]):
        outcome = check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    assert outcome == CHECK_RESULT_RECLASSIFIED
    assert company.deletion_status == DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED
    assert company.deletion_last_response_message_id == "m2"  # cursor unchanged - no new message
    assert db.query(MailMessage).filter(MailMessage.company_id == company.id).count() == 1  # no duplicate row
    event = (
        db.query(DeletionEvent)
        .filter(DeletionEvent.company_id == company.id, DeletionEvent.event_type == EventType.ACCOUNT_CLOSED_DATA_UNVERIFIED)
        .one()
    )
    assert event.evidence["reclassified"] is True


def test_manual_check_with_no_new_message_and_no_improvement_is_no_change(db):
    """Genuinely ambiguous wording that STILL doesn't match anything, even
    under the current classifier, must fail closed - CHECK_RESULT_NO_CHANGE,
    never CHECK_RESULT_RECLASSIFIED for a non-improvement."""
    company = _company(db, deletion_method="EMAIL_REQUEST")
    old_message = _seed_stale_unknown_response_for(db, company, "m2", "Thanks so much, appreciate it!")

    with patch("app.google_oauth.fetch_thread_messages", return_value=[old_message]):
        outcome = check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    assert outcome == CHECK_RESULT_NO_CHANGE
    assert company.deletion_status == DeletionStatus.UNKNOWN_RESPONSE


def test_repeated_reconciliation_after_success_is_idempotent(db):
    """Once reclassified, a second identical check must be a pure no-op -
    same status, same event count, same MailMessage count, reported as
    CHECK_RESULT_NO_CHANGE (not reclassified again)."""
    company = _company(db, deletion_method="EMAIL_REQUEST")
    old_message = _seed_stale_unknown_response_for(
        db, company, "m2", "We have deactivated your account as requested.",
    )

    with patch("app.google_oauth.fetch_thread_messages", return_value=[old_message]):
        first = check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert first == CHECK_RESULT_RECLASSIFIED
    events_after_first = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count()
    mail_after_first = db.query(MailMessage).filter(MailMessage.company_id == company.id).count()
    status_after_first = company.deletion_status

    with patch("app.google_oauth.fetch_thread_messages", return_value=[old_message]):
        second = check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    assert second == CHECK_RESULT_NO_CHANGE
    assert company.deletion_status == status_after_first
    assert db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count() == events_after_first
    assert db.query(MailMessage).filter(MailMessage.company_id == company.id).count() == mail_after_first


def test_own_outbound_followup_does_not_count_as_new_company_reply(db):
    """Cookie Monster's own already-sent chase follow-up living in the
    same thread must never be mistaken for a new company reply - it must
    be filtered out exactly like any other own-sent message, leaving the
    persisted UNKNOWN_RESPONSE eligible for reclassification instead."""
    company = _company(db, deletion_method="EMAIL_REQUEST")
    old_message = _seed_stale_unknown_response_for(
        db, company, "m2", "We have deactivated your account as requested.",
    )
    own_followup = _msg(
        "own-followup-1", "I'm following up on my data deletion request...", "me@gmail.com", 3000, sent=True,
    )

    with patch("app.google_oauth.fetch_thread_messages", return_value=[old_message, own_followup]):
        outcome = check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    assert outcome != CHECK_RESULT_NEW_MESSAGE
    assert outcome == CHECK_RESULT_RECLASSIFIED
    assert company.deletion_status == DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED
    assert company.deletion_last_response_message_id == "m2"  # never advanced to the own message


# --- Chase-timer semantics: a reclassification of OLD evidence must never
# manufacture a fresh grace period the company never earned, and a
# genuinely NEW but ambiguous/generic live reply must never postpone an
# already-active chase either - see chase_engine.on_reply_classified. ---

def test_reclassification_to_account_closed_does_not_grant_fresh_grace_period(db):
    """The exact scenario a live check surfaced: a company already
    WAITING_ON=COMPANY with an active follow-up schedule (one generic
    catch-up follow-up already sent) gets its stale UNKNOWN_RESPONSE
    reclassified to ACCOUNT_CLOSED_DATA_UNVERIFIED. waiting_on stays
    COMPANY, but next_followup_at must be completely UNCHANGED - never
    reset to a fresh 24h window merely because old evidence was
    reinterpreted."""
    already_scheduled = datetime.datetime(2024, 6, 1, 12, 0, 0)
    company = _company(
        db, deletion_method="EMAIL_REQUEST", waiting_on=WaitingOn.COMPANY, next_followup_at=already_scheduled,
    )
    old_message = _seed_stale_unknown_response_for(
        db, company, "m2", "We have deactivated your account as requested.",
    )

    with patch("app.google_oauth.fetch_thread_messages", return_value=[old_message]):
        outcome = check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    assert outcome == CHECK_RESULT_RECLASSIFIED
    assert company.deletion_status == DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED
    assert company.waiting_on == WaitingOn.COMPANY
    assert company.next_followup_at == already_scheduled  # completely unchanged - no fresh grace period


def _seed_processed_inbound(db, company, gmail_message_id, text, occurred_at=None):
    """A message the MODERN pipeline already fully processed - a real
    MailMessage row exists for it, exactly like check_company_response
    itself always creates for every message it examines. Distinct from
    _seed_stale_unknown_response_for's LEGACY shape (zero MailMessage
    rows) - this is what makes these tests exercise the ordinary
    'genuinely new message arrives next' path, not legacy-cursor
    recovery."""
    occurred_at = occurred_at or datetime.datetime(2024, 1, 1)
    db.add(MailMessage(
        company_id=company.id, direction="inbound", gmail_message_id=gmail_message_id,
        gmail_thread_id=company.deletion_thread_id, occurred_at=occurred_at,
        from_display="privacy@widgetco.com", subject="Re: request", body_excerpt=text,
    ))
    db.commit()


def test_ambiguous_new_reply_does_not_postpone_already_scheduled_chase(db):
    """A genuinely NEW (not reclassified) but ambiguous live reply must
    never push an already-active chase schedule further out - same rule
    as a reclassification, applied to the live path."""
    already_scheduled = datetime.datetime(2024, 6, 1, 12, 0, 0)
    company = _company(
        db, deletion_method="EMAIL_REQUEST", waiting_on=WaitingOn.COMPANY, next_followup_at=already_scheduled,
        deletion_last_response_message_id="m1",
    )
    msg_prior = _msg("m1", "We received your request.", "privacy@widgetco.com", 1000)
    _seed_processed_inbound(db, company, "m1", "We received your request.")
    new_ambiguous = _msg("m2", "Thanks so much, appreciate it!", "privacy@widgetco.com", 2000)

    with patch("app.google_oauth.fetch_thread_messages", return_value=[msg_prior, new_ambiguous]):
        outcome = check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    assert outcome == CHECK_RESULT_NEW_MESSAGE
    assert company.deletion_status == DeletionStatus.UNKNOWN_RESPONSE
    assert company.waiting_on == WaitingOn.COMPANY
    assert company.next_followup_at == already_scheduled  # unchanged - ambiguous content never postpones


def test_generic_acknowledgment_does_not_postpone_already_scheduled_chase(db):
    already_scheduled = datetime.datetime(2024, 6, 1, 12, 0, 0)
    company = _company(
        db, deletion_method="EMAIL_REQUEST", waiting_on=WaitingOn.COMPANY, next_followup_at=already_scheduled,
        deletion_last_response_message_id="m1",
    )
    msg_prior = _msg("m1", "We received your request.", "privacy@widgetco.com", 1000)
    _seed_processed_inbound(db, company, "m1", "We received your request.")
    generic_ack = _msg("m2", "We are currently reviewing your request.", "privacy@widgetco.com", 2000)

    with patch("app.google_oauth.fetch_thread_messages", return_value=[msg_prior, generic_ack]):
        outcome = check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    assert outcome == CHECK_RESULT_NEW_MESSAGE
    assert company.deletion_status == DeletionStatus.IN_PROGRESS
    assert company.next_followup_at == already_scheduled  # unchanged


def test_user_required_action_pauses_schedule_via_full_pipeline(db):
    already_scheduled = datetime.datetime(2024, 6, 1, 12, 0, 0)
    company = _company(
        db, deletion_method="EMAIL_REQUEST", waiting_on=WaitingOn.COMPANY, next_followup_at=already_scheduled,
        deletion_last_response_message_id="m1",
    )
    msg_prior = _msg("m1", "We received your request.", "privacy@widgetco.com", 1000)
    _seed_processed_inbound(db, company, "m1", "We received your request.")
    verification = _msg("m2", "Please verify your identity to continue.", "privacy@widgetco.com", 2000)

    with patch("app.google_oauth.fetch_thread_messages", return_value=[msg_prior, verification]):
        outcome = check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    assert outcome == CHECK_RESULT_NEW_MESSAGE
    assert company.deletion_status == DeletionStatus.VERIFICATION_NEEDED
    assert company.waiting_on == WaitingOn.USER
    assert company.next_followup_at is None  # paused - the user must act first


def test_ambiguous_reply_does_not_resume_chase_after_user_pause(db):
    """A generic/ambiguous reply arriving while the case is paused waiting
    on the USER must never silently resume automatic chasing on its own -
    only a materially specific classification (e.g.
    ACCOUNT_CLOSED_DATA_UNVERIFIED) may do that. See
    chase_engine._SCHEDULE_SIGNIFICANT_FOR_RESUME."""
    company = _company(
        db, deletion_method="EMAIL_REQUEST", waiting_on=WaitingOn.USER, next_followup_at=None,
        deletion_last_response_message_id="m1",
    )
    msg_prior = _msg("m1", "Please verify your identity.", "privacy@widgetco.com", 1000)
    _seed_processed_inbound(db, company, "m1", "Please verify your identity.")
    ambiguous_followup = _msg("m2", "Thanks so much, appreciate it!", "privacy@widgetco.com", 2000)

    with patch("app.google_oauth.fetch_thread_messages", return_value=[msg_prior, ambiguous_followup]):
        outcome = check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    assert outcome == CHECK_RESULT_NEW_MESSAGE
    assert company.deletion_status == DeletionStatus.UNKNOWN_RESPONSE
    assert company.next_followup_at is None  # still unscheduled - generic content never resumed it
