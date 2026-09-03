"""Regression coverage for a real bug found via live testing on goop
kitchen: a company's reply that merely QUOTED Cookie Monster's own
outgoing deletion request (which itself contains the phrase "identity
verification") was misclassified as VERIFICATION_NEEDED off that quoted
text - not anything goop kitchen actually wrote. strip_quoted_reply() and
_html_to_text()'s blockquote removal (app/deletion_response_tracker.py)
fix this generically: classification only ever sees a reply's genuinely
NEW content, never quoted history.
"""
import base64
import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.deletion_constants import DeletionStatus
from app.deletion_engine import build_structured_email
from app.deletion_response_tracker import (
    _html_to_text,
    check_company_response,
    extract_body_text,
    strip_quoted_reply,
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
        name="Goop Kitchen", domain="goopkitchen.com", relationship_type="transactional", status="confirmed",
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        deletion_status=DeletionStatus.SUBMITTED, deletion_thread_id="thread-38",
    )
    defaults.update(overrides)
    company = Company(**defaults)
    db.add(company)
    db.commit()
    return company


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _msg(msg_id, body_text, from_addr, internal_date, sent=False, mime="text/plain"):
    return {
        "id": msg_id,
        "labelIds": ["SENT"] if sent else ["INBOX"],
        "internalDate": str(internal_date),
        "payload": {
            "headers": [{"name": "From", "value": from_addr}],
            "mimeType": mime,
            "body": {"data": _b64(body_text)},
        },
    }


# Cookie Monster's REAL outgoing email text - the exact thing a company's
# reply is likely to quote back, including the "identity verification"
# phrase that VERIFICATION_NEEDED_PATTERNS looks for.
_OUR_OUTGOING_BODY = build_structured_email(
    Company(name="Goop Kitchen", domain="goopkitchen.com", deletion_email="privacy@goopkitchen.com"),
    None, "me@gmail.com",
)["body"]


def _quoted(body: str) -> str:
    return "\n".join("> " + line if line else ">" for line in body.splitlines())


# --- strip_quoted_reply() unit coverage ---

def test_strip_quoted_reply_removes_gt_prefixed_block():
    # The "On ... wrote:" header itself is ALSO a recognized quote boundary,
    # so cutoff happens there - one line earlier than the '>' block itself,
    # which is the more thorough (and still correct) result.
    text = "Thanks, got it.\n\nOn Mon, wrote:\n> original line one\n> original line two"
    assert strip_quoted_reply(text) == "Thanks, got it."


def test_strip_quoted_reply_removes_gt_block_without_header():
    text = "Thanks, got it.\n> original line one\n> original line two"
    assert strip_quoted_reply(text) == "Thanks, got it."


def test_strip_quoted_reply_stops_at_on_wrote_header():
    text = "New reply text here.\n\nOn Tue, Jan 2, 2024 at 3:00 PM Someone <a@b.com> wrote:\nNo gt-marker line here."
    assert strip_quoted_reply(text) == "New reply text here."


def test_strip_quoted_reply_stops_at_outlook_original_message_marker():
    text = "New reply text.\n\n-----Original Message-----\nFrom: someone"
    assert strip_quoted_reply(text) == "New reply text."


def test_strip_quoted_reply_leaves_text_unchanged_when_no_boundary_found():
    text = "Just a normal reply with no quoting at all."
    assert strip_quoted_reply(text) == text


def test_strip_quoted_reply_entirely_quoted_message_yields_empty():
    text = "On Mon, wrote:\n> all quoted, nothing new"
    assert strip_quoted_reply(text) == ""


# --- _html_to_text() blockquote stripping ---

def test_html_to_text_strips_blockquote():
    html = "<div>New reply text.</div><blockquote>quoted identity verification text</blockquote>"
    assert "identity verification" not in _html_to_text(html)
    assert "New reply text." in _html_to_text(html)


def test_html_to_text_strips_gmail_quote_class():
    html = '<div>New reply text.</div><div class="gmail_quote">quoted original message</div>'
    assert "quoted original message" not in _html_to_text(html)
    assert "New reply text." in _html_to_text(html)


# --- Full pipeline: the actual goop kitchen regression ---

def test_quoted_identity_verification_language_does_not_trigger_verification_needed(db):
    """The core regression: a reply that quotes our OWN outgoing request
    (which itself asks about identity verification) must not be classified
    off that quoted text."""
    company = _company(db)
    reply_body = (
        "Thanks for reaching out, we've received your request and will process it shortly.\n\n"
        "On Mon, Jan 1, 2024 at 10:00 AM Cookie Monster User <me@gmail.com> wrote:\n"
        + _quoted(_OUR_OUTGOING_BODY)
    )
    assert "identity verification" in reply_body.lower()  # sanity: the trap is really there

    reply = _msg("m2", reply_body, "privacy@goopkitchen.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, MagicMock(), "me@gmail.com", ResponseClassifier())

    assert company.deletion_status != DeletionStatus.VERIFICATION_NEEDED
    assert company.deletion_status == DeletionStatus.IN_PROGRESS  # the genuinely new content: a plain ack


def test_incoming_acknowledgment_quoting_original_request_is_in_progress(db):
    """A generic ack that quotes the whole original request must classify
    off the ack, not the quote - and never overclaims a stronger status."""
    company = _company(db)
    reply_body = "Got it, thank you!\n\n" + _quoted(_OUR_OUTGOING_BODY)
    reply = _msg("m2", reply_body, "privacy@goopkitchen.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, MagicMock(), "me@gmail.com", ResponseClassifier())

    assert company.deletion_status not in (DeletionStatus.VERIFICATION_NEEDED, DeletionStatus.COMPLETED)


def test_genuinely_new_verification_request_still_triggers_verification_needed(db):
    """The fix must not become blind to a REAL verification request just
    because it's not quoted - only quoted content is stripped."""
    company = _company(db)
    reply_body = (
        "Before we can proceed, please verify your identity by replying with your account email.\n\n"
        "On Mon, Jan 1, 2024 at 10:00 AM Cookie Monster User <me@gmail.com> wrote:\n"
        + _quoted(_OUR_OUTGOING_BODY)
    )
    reply = _msg("m2", reply_body, "privacy@goopkitchen.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, MagicMock(), "me@gmail.com", ResponseClassifier())

    assert company.deletion_status == DeletionStatus.VERIFICATION_NEEDED
    event = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).one()
    assert "verify your identity" in event.evidence["quote"].lower()
    assert "identity verification" not in event.evidence["quote"].lower()  # never our own quoted phrase


def test_quoted_completion_language_does_not_trigger_completed(db):
    """A forwarded/quoted copy of a PAST completion notice (e.g. the user's
    own earlier message referenced it, or an auto-responder echoes an old
    status) must never be mistaken for a fresh completion claim."""
    company = _company(db)
    reply_body = (
        "We are still reviewing your request, ticket #4821 opened.\n\n"
        "On Mon, wrote:\n> Note: your data has been permanently deleted from our old test system."
    )
    reply = _msg("m2", reply_body, "privacy@goopkitchen.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, MagicMock(), "me@gmail.com", ResponseClassifier())

    assert company.deletion_status != DeletionStatus.COMPLETED
    assert company.deletion_status == DeletionStatus.IN_PROGRESS


def test_genuinely_new_completion_language_still_triggers_completed(db):
    company = _company(db)
    reply_body = (
        "Your personal information has been permanently deleted from our systems.\n\n"
        "On Mon, Jan 1, 2024 at 10:00 AM Cookie Monster User <me@gmail.com> wrote:\n"
        + _quoted(_OUR_OUTGOING_BODY)
    )
    reply = _msg("m2", reply_body, "privacy@goopkitchen.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, MagicMock(), "me@gmail.com", ResponseClassifier())

    assert company.deletion_status == DeletionStatus.COMPLETED


def test_stripping_down_to_nothing_falls_back_conservatively(db):
    """If the ENTIRE message is quoted (nothing new at all), classification
    must never claim a strong status off empty/insufficient evidence."""
    company = _company(db)
    reply_body = "On Mon, wrote:\n" + _quoted(_OUR_OUTGOING_BODY)
    reply = _msg("m2", reply_body, "privacy@goopkitchen.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, MagicMock(), "me@gmail.com", ResponseClassifier())

    assert company.deletion_status == DeletionStatus.UNKNOWN_RESPONSE
    event = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).one()
    assert event.evidence["confidence"] == "low"


# --- Own sent messages remain excluded ---

def test_own_sent_message_with_quoting_is_still_excluded(db):
    company = _company(db)
    own = _msg("m1", "please delete my data, thanks", "me@gmail.com", 1000, sent=True)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[own]):
        check_company_response(db, company, MagicMock(), "me@gmail.com", ResponseClassifier())
    assert company.deletion_status == DeletionStatus.SUBMITTED
    assert db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count() == 0


# --- No general inbox search introduced ---

def test_check_company_response_only_ever_reads_the_one_tracked_thread(db):
    company = _company(db)
    reply = _msg("m2", "We are reviewing your request.", "privacy@goopkitchen.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]) as mock_fetch:
        check_company_response(db, company, MagicMock(), "me@gmail.com", ResponseClassifier())
    mock_fetch.assert_called_once()
    args, _ = mock_fetch.call_args
    assert args[1] == "thread-38"  # exactly the one stored thread id, nothing else
