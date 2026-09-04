"""Regression coverage for the Baker's Dozen mailbox (app/mail.py +
deletion_response_tracker.py's hook into it + main.py's /mail routes).

Privacy boundary under test throughout: a MailMessage row is only ever
created from a message deletion_response_tracker.check_company_response()
already fetched via its single-thread-only google_oauth.fetch_thread_messages()
call - never a new read path, never anything for a company without a
tracked deletion_thread_id, never the sender's own outgoing message.
"""
import base64
import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config, mail
from app.db import Base
from app.deletion_constants import DeletionStatus, EventType
from app.deletion_response_tracker import check_company_response
from app.mail import MailSendError, MailState, ReplyKind
from app.models import Company, DeletionEvent, DeletionRecipe, MailMessage
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
        deletion_status=DeletionStatus.SUBMITTED, deletion_thread_id="thread123",
    )
    defaults.update(overrides)
    company = Company(**defaults)
    db.add(company)
    db.commit()
    return company


def _recipe(db, domain="goopkitchen.com", **overrides) -> DeletionRecipe:
    defaults = dict(domain=domain, email="privacy@goopkitchen.com")
    defaults.update(overrides)
    recipe = DeletionRecipe(**defaults)
    db.add(recipe)
    db.commit()
    return recipe


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _msg(msg_id, body_text, from_addr, internal_date, sent=False, message_id_header=None, subject="Re: your request"):
    headers = [{"name": "From", "value": from_addr}, {"name": "Subject", "value": subject}]
    if message_id_header:
        headers.append({"name": "Message-ID", "value": message_id_header})
    return {
        "id": msg_id,
        "labelIds": ["SENT"] if sent else ["INBOX"],
        "internalDate": str(internal_date),
        "payload": {
            "headers": headers,
            "mimeType": "text/plain",
            "body": {"data": _b64(body_text)},
        },
    }


# --- privacy boundary: tracked-thread-only, own messages excluded ---

def test_company_without_tracked_thread_gets_no_mail(db):
    company = _company(db, deletion_thread_id=None)
    assert mail.get_company_mail(db, company.id) == []
    assert mail.mailbox_entries(db) == []


def test_check_company_response_only_ever_calls_fetch_thread_messages(db):
    """The one Gmail read call this whole feature is allowed to make - a
    direct get on THIS company's own tracked thread id, never a search or
    list, and never a different thread."""
    company = _company(db)
    reply = _msg("m2", "We are currently reviewing your request.", "privacy@goopkitchen.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]) as fetch:
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert fetch.call_count == 1
    assert fetch.call_args[0][1] == "thread123"
    assert mail.get_company_mail(db, company.id)  # mail WAS created from that one fetch


def test_own_sent_message_never_becomes_mail(db):
    company = _company(db)
    sent = _msg("m1", "please delete my data", "me@gmail.com", 1000, sent=True)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[sent]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert mail.get_company_mail(db, company.id) == []


# --- quoted history excluded from what's persisted ---

def test_quoted_history_excluded_from_body_excerpt(db):
    company = _company(db)
    body = "We received your request.\n\nOn Mon, Jan 1, 2024 wrote:\n> please delete my data\n> identity verification needed"
    reply = _msg("m2", body, "privacy@goopkitchen.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    row = mail.get_company_mail(db, company.id)[0]
    assert "please delete my data" not in row.body_excerpt
    assert "We received your request." in row.body_excerpt


# --- duplicate Gmail message never creates duplicate mail ---

def test_duplicate_gmail_message_id_never_creates_duplicate_mail(db):
    company = _company(db)
    message = _msg("m2", "We received your request.", "privacy@goopkitchen.com", 2000)
    classification = ResponseClassifier().classify("We received your request.")
    mail.record_inbound_mail_message(db, company, message, "We received your request.", classification)
    db.commit()
    mail.record_inbound_mail_message(db, company, message, "We received your request.", classification)
    db.commit()
    assert db.query(MailMessage).filter(MailMessage.gmail_message_id == "m2").count() == 1


def test_already_processed_message_produces_no_second_mail_row(db):
    """The existing dedup marker (deletion_last_response_message_id) means
    check_company_response never re-processes a message at all - so a
    second identical tick never even reaches record_inbound_mail_message.
    Seeds a real MailMessage row for "m2" first, matching what the actual
    pipeline always creates for a message it has genuinely already
    processed - a cursor set with NO MailMessage row at all is the
    distinct legacy-recovery signature (see
    deletion_response_tracker.py's check_company_response), not this."""
    company = _company(db, deletion_last_response_message_id="m2")
    db.add(MailMessage(
        company_id=company.id, direction="inbound", gmail_message_id="m2",
        gmail_thread_id=company.deletion_thread_id, occurred_at=datetime.datetime(2022, 1, 2),
        from_display="privacy@goopkitchen.com", subject="Re: request", body_excerpt="We received your request.",
    ))
    db.commit()
    reply = _msg("m2", "We received your request.", "privacy@goopkitchen.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert len(mail.get_company_mail(db, company.id)) == 1  # only the seeded row - no second one created


# --- unread / read state ---

def test_new_inbound_mail_starts_unread(db):
    company = _company(db)
    reply = _msg("m2", "We received your request.", "privacy@goopkitchen.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    row = mail.get_company_mail(db, company.id)[0]
    assert row.read_at is None
    assert mail.mail_state_for_company(company, [row]) == MailState.UNREAD


def test_mark_inbound_read_clears_unread_state(db):
    company = _company(db)
    reply = _msg("m2", "We received your request.", "privacy@goopkitchen.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    marked = mail.mark_inbound_read(db, company.id)
    assert marked == 1
    row = mail.get_company_mail(db, company.id)[0]
    assert row.read_at is not None
    assert mail.mail_state_for_company(company, [row]) == MailState.READ


# --- action-needed state ---

def test_verification_needed_reply_is_action_needed(db):
    company = _company(db)
    reply = _msg("m2", "Please verify your identity to continue.", "privacy@goopkitchen.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    row = mail.get_company_mail(db, company.id)[0]
    mail.mark_inbound_read(db, company.id)
    db.refresh(row)
    assert mail.mail_state_for_company(company, [row]) == MailState.ACTION_NEEDED


# --- account-deletion vs data-deletion classification ---

def test_account_deletion_choice_requires_both_action_needed_and_recipe_flag(db):
    company = _company(db, deletion_status=DeletionStatus.VERIFICATION_NEEDED)
    recipe_no_flag = _recipe(db, deletes_account=False)
    assert mail.account_deletion_choice_available(company, recipe_no_flag) is False

    recipe_flag = DeletionRecipe(domain="other.com", deletes_account=True)
    other_company = _company(db, domain="other.com", deletion_status=DeletionStatus.VERIFICATION_NEEDED)
    assert mail.account_deletion_choice_available(other_company, recipe_flag) is True


def test_account_deletion_choice_unavailable_when_not_action_needed(db):
    company = _company(db, deletion_status=DeletionStatus.SUBMITTED)
    recipe = _recipe(db, deletes_account=True)
    assert mail.account_deletion_choice_available(company, recipe) is False


def test_account_deletion_choice_unavailable_with_no_recipe(db):
    company = _company(db, deletion_status=DeletionStatus.VERIFICATION_NEEDED)
    assert mail.account_deletion_choice_available(company, None) is False


def test_delete_and_keep_replies_are_meaningfully_different(db):
    company = _company(db, deletion_status=DeletionStatus.VERIFICATION_NEEDED)
    recipe = _recipe(db, deletes_account=True)
    delete_reply = mail.build_choice_reply(company, recipe, None, ReplyKind.DELETE_ACCOUNT_AND_DATA, "me@gmail.com")
    keep_reply = mail.build_choice_reply(company, recipe, None, ReplyKind.KEEP_ACCOUNT_DATA_ONLY, "me@gmail.com")
    assert delete_reply["body"] != keep_reply["body"]
    assert "closure of my account" in delete_reply["body"]
    assert "maintain my account" in keep_reply["body"]
    assert "closure of my account" not in keep_reply["body"]


def test_build_choice_reply_rejects_unknown_kind(db):
    company = _company(db, deletion_status=DeletionStatus.VERIFICATION_NEEDED)
    recipe = _recipe(db, deletes_account=True)
    with pytest.raises(ValueError):
        mail.build_choice_reply(company, recipe, None, "not_a_real_kind", "me@gmail.com")


# --- response draft requires explicit approval before anything sends ---

def test_preview_never_sends_or_creates_mail(db):
    company = _company(db, deletion_status=DeletionStatus.VERIFICATION_NEEDED)
    recipe = _recipe(db, deletes_account=True)
    draft = mail.build_choice_reply(company, recipe, None, ReplyKind.DELETE_ACCOUNT_AND_DATA, "me@gmail.com")
    assert draft["to"] == "privacy@goopkitchen.com"
    assert mail.get_company_mail(db, company.id) == []
    assert company.deletion_status == DeletionStatus.VERIFICATION_NEEDED  # untouched by building a preview


# --- send: failure never appears sent, success stores real evidence ---

def test_failed_send_creates_no_mail_row_and_leaves_status_untouched(db):
    company = _company(db, deletion_status=DeletionStatus.VERIFICATION_NEEDED)
    _recipe(db, deletes_account=True)
    inbound = _msg("m2", "Please verify your identity.", "privacy@goopkitchen.com", 2000, message_id_header="<abc@goop.com>")
    classification = ResponseClassifier().classify("Please verify your identity.")
    mail.record_inbound_mail_message(db, company, inbound, "Please verify your identity.", classification)
    db.commit()

    with patch("app.mail.google_oauth_send_reply", side_effect=RuntimeError("Gmail is down")):
        with pytest.raises(MailSendError):
            mail.send_mailbox_reply(db, company, ReplyKind.DELETE_ACCOUNT_AND_DATA, creds=MagicMock(), gmail_address="me@gmail.com")

    assert company.deletion_status == DeletionStatus.VERIFICATION_NEEDED  # never advanced on a failed send
    outbound = [m for m in mail.get_company_mail(db, company.id) if m.direction == "outbound"]
    assert outbound == []
    failed_events = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id, DeletionEvent.event_type == EventType.FAILED).all()
    assert len(failed_events) == 1


def test_successful_send_stores_gmail_evidence_and_advances_status(db):
    company = _company(db, deletion_status=DeletionStatus.VERIFICATION_NEEDED)
    _recipe(db, deletes_account=True)
    inbound = _msg("m2", "Please verify your identity.", "privacy@goopkitchen.com", 2000, message_id_header="<abc@goop.com>")
    classification = ResponseClassifier().classify("Please verify your identity.")
    mail.record_inbound_mail_message(db, company, inbound, "Please verify your identity.", classification)
    db.commit()

    fake_response = {"id": "sent-msg-1", "threadId": "thread123"}
    with patch("app.mail.google_oauth_send_reply", return_value=fake_response) as send:
        row = mail.send_mailbox_reply(db, company, ReplyKind.KEEP_ACCOUNT_DATA_ONLY, creds=MagicMock(), gmail_address="me@gmail.com")

    send.assert_called_once()
    assert row.direction == "outbound"
    assert row.gmail_message_id == "sent-msg-1"
    assert row.read_at is not None  # outbound rows are never "unread"
    assert company.deletion_status == DeletionStatus.IN_PROGRESS
    assert company.deletion_evidence["type"] == "gmail_send"
    assert company.deletion_evidence["gmail_message_id"] == "sent-msg-1"
    sent_event = db.query(DeletionEvent).filter(
        DeletionEvent.company_id == company.id, DeletionEvent.event_type == EventType.MAIL_REPLY_SENT
    ).one()
    assert sent_event is not None


def test_send_refuses_when_no_choice_available(db):
    company = _company(db, deletion_status=DeletionStatus.SUBMITTED)  # not action-needed
    with pytest.raises(ValueError):
        mail.send_mailbox_reply(db, company, ReplyKind.DELETE_ACCOUNT_AND_DATA, creds=MagicMock(), gmail_address="me@gmail.com")


# --- progress only ever changes on qualifying evidence ---

def test_recording_inbound_mail_alone_never_changes_deletion_status(db):
    """record_inbound_mail_message is a pure persistence step - the
    classifier-driven status transition happens in check_company_response,
    not here. A caller that only records mail must never accidentally
    move the pipeline forward."""
    company = _company(db, deletion_status=DeletionStatus.SUBMITTED)
    message = _msg("m2", "Your data has been deleted.", "privacy@goopkitchen.com", 2000)
    classification = ResponseClassifier().classify("Your data has been deleted.")
    mail.record_inbound_mail_message(db, company, message, "Your data has been deleted.", classification)
    db.commit()
    assert company.deletion_status == DeletionStatus.SUBMITTED  # untouched


def test_ambiguous_reply_never_falsely_marks_complete(db):
    company = _company(db)
    reply = _msg("m2", "Thanks for reaching out, we'll be in touch.", "privacy@goopkitchen.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_status != DeletionStatus.COMPLETED
    row = mail.get_company_mail(db, company.id)[0]
    assert row.classification_status != DeletionStatus.COMPLETED


def test_only_strong_completion_language_advances_to_completed_and_creates_mail(db):
    company = _company(db)
    reply = _msg("m2", "Your personal data has been permanently deleted from our systems.", "privacy@goopkitchen.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_status == DeletionStatus.COMPLETED
    row = mail.get_company_mail(db, company.id)[0]
    assert row.classification_status == DeletionStatus.COMPLETED
    assert mail.mail_state_for_company(company, [row]) == MailState.RESOLVED
    assert mail.mailbox_reason_label(company, MailState.RESOLVED) == "Deletion completed"


# --- mailbox_entries / reason labels ---

def test_mailbox_entries_orders_newest_first(db):
    a = _company(db, domain="a.com", name="A Co", deletion_thread_id="ta")
    b = _company(db, domain="b.com", name="B Co", deletion_thread_id="tb")
    old_msg = _msg("m-a", "We received your request.", "privacy@a.com", 1_000_000)
    new_msg = _msg("m-b", "We received your request.", "privacy@b.com", 2_000_000)
    classifier = ResponseClassifier()
    mail.record_inbound_mail_message(db, a, old_msg, "We received your request.", classifier.classify("We received your request."))
    mail.record_inbound_mail_message(db, b, new_msg, "We received your request.", classifier.classify("We received your request."))
    db.commit()
    entries = mail.mailbox_entries(db)
    assert [e["company"].id for e in entries] == [b.id, a.id]


def test_unread_mail_count_counts_only_unread_inbound(db):
    company = _company(db)
    reply = _msg("m2", "We received your request.", "privacy@goopkitchen.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert mail.unread_mail_count(db) == 1
    mail.mark_inbound_read(db, company.id)
    assert mail.unread_mail_count(db) == 0
