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
from app.deletion_response_tracker import (
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
