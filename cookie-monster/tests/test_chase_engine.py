"""The 24-hour chase engine (app/chase_engine.py): once Baker's Dozen sends
an EMAIL_REQUEST deletion request, it must keep following up in the SAME
Gmail thread every 24 hours until a legitimate terminal outcome - without
the clock being reset merely by generic company chatter, and without ever
double-sending across a worker crash. See chase_engine.py's own docstring
for the full set of invariants these tests hold it to.
"""
import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import chase_engine, config
from app.db import Base
from app.deletion_constants import DeletionStatus, EventType, WaitingOn
from app.deletion_response_tracker import check_company_response
from app.models import Company, DeletionEvent, MailMessage
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
        deletion_method="EMAIL_REQUEST", deletion_status=DeletionStatus.SUBMITTED, deletion_thread_id="thread123",
    )
    defaults.update(overrides)
    company = Company(**defaults)
    db.add(company)
    db.commit()
    return company


def _outbound(db, company, gmail_message_id="orig-1", occurred_at=None, subject="CCPA/CPRA Deletion Request - Widget Co"):
    db.add(MailMessage(
        company_id=company.id, direction="outbound", gmail_message_id=gmail_message_id,
        gmail_thread_id=company.deletion_thread_id, occurred_at=occurred_at or datetime.datetime(2024, 1, 1),
        from_display="You", subject=subject, body_excerpt="I am requesting deletion...", read_at=datetime.datetime(2024, 1, 1),
    ))
    db.commit()


def _inbound(db, company, gmail_message_id="reply-1", occurred_at=None, from_display="Privacy Team <privacy@widgetco.com>"):
    db.add(MailMessage(
        company_id=company.id, direction="inbound", gmail_message_id=gmail_message_id,
        gmail_thread_id=company.deletion_thread_id, occurred_at=occurred_at or datetime.datetime(2024, 1, 2),
        from_display=from_display, subject="Re: request", body_excerpt="We're on it.",
    ))
    db.commit()


# --- derive_waiting_on: the state-derivation table ---

@pytest.mark.parametrize(
    "status,body,expected",
    [
        (DeletionStatus.SUBMITTED, "", WaitingOn.COMPANY),
        (DeletionStatus.IN_PROGRESS, "We have received your request and will get back to you.", WaitingOn.COMPANY),
        (DeletionStatus.VERIFICATION_NEEDED, "", WaitingOn.USER),
        (DeletionStatus.MORE_INFO_REQUIRED, "", WaitingOn.USER),
        (DeletionStatus.REJECTED, "", WaitingOn.ESCALATION_NEEDED),
        (DeletionStatus.COMPLETED, "", None),
        (DeletionStatus.FAILED, "", None),
    ],
)
def test_derive_waiting_on_table(status, body, expected):
    assert chase_engine.derive_waiting_on(status, body) == expected


def test_unknown_response_without_signal_stays_with_company():
    assert chase_engine.derive_waiting_on(DeletionStatus.UNKNOWN_RESPONSE, "Thanks for your email!") == WaitingOn.COMPANY


def test_unknown_response_with_verification_signal_routes_to_user():
    assert chase_engine.derive_waiting_on(
        DeletionStatus.UNKNOWN_RESPONSE, "Please verify your identity before we continue."
    ) == WaitingOn.USER


# --- on_request_sent: item 1 - initial request schedules +24h ---

def test_initial_request_schedules_24h_followup(db):
    company = _company(db, waiting_on=None, next_followup_at=None)
    sent_at = datetime.datetime(2024, 1, 1, 12, 0, 0)
    chase_engine.on_request_sent(company, sent_at)
    assert company.waiting_on == WaitingOn.COMPANY
    assert company.next_followup_at == sent_at + datetime.timedelta(hours=config.FOLLOWUP_INTERVAL_HOURS)
    assert company.followup_attempt == 0
    assert company.followups_paused is False


def test_on_request_sent_no_op_for_non_email_method(db):
    company = _company(db, deletion_method="WEB_FORM", waiting_on=None)
    chase_engine.on_request_sent(company, datetime.datetime.utcnow())
    assert company.waiting_on is None
    assert company.next_followup_at is None


# --- on_reply_classified: items 4, 5, 6, 8, 9, 16 ---

def test_acknowledgment_does_not_postpone_already_scheduled_chase(db):
    """A generic company acknowledgment arriving while already COMPANY with
    a follow-up already scheduled must NEVER push next_followup_at further
    out - at most one scheduled automated chase per 24h cycle while the
    ball stays in the company's court."""
    scheduled = datetime.datetime(2024, 1, 2, 12, 0, 0)
    company = _company(db, waiting_on=WaitingOn.COMPANY, next_followup_at=scheduled)
    chase_engine.on_reply_classified(
        company, DeletionStatus.IN_PROGRESS, "Just checking in - still processing your request.",
        datetime.datetime(2024, 1, 2, 0, 0, 0),
    )
    assert company.waiting_on == WaitingOn.COMPANY
    assert company.next_followup_at == scheduled  # unchanged


def test_generic_processing_reply_does_not_postpone_scheduled_chase(db):
    scheduled = datetime.datetime(2024, 1, 2, 12, 0, 0)
    company = _company(db, waiting_on=WaitingOn.COMPANY, next_followup_at=scheduled)
    chase_engine.on_reply_classified(
        company, DeletionStatus.SUBMITTED, "We're processing your request.", datetime.datetime(2024, 1, 2, 1, 0, 0),
    )
    assert company.next_followup_at == scheduled


def test_first_entry_into_companys_court_schedules_fresh_window(db):
    """Unlike a repeat acknowledgment, a TRANSITION into COMPANY's court
    (from unscheduled, USER, or ESCALATION_NEEDED) does get a fresh 24h
    window - this is a genuinely new event, not chatter."""
    occurred_at = datetime.datetime(2024, 1, 5, 9, 0, 0)
    company = _company(db, waiting_on=None, next_followup_at=None)
    chase_engine.on_reply_classified(company, DeletionStatus.IN_PROGRESS, "We're on it.", occurred_at)
    assert company.waiting_on == WaitingOn.COMPANY
    assert company.next_followup_at == occurred_at + datetime.timedelta(hours=config.FOLLOWUP_INTERVAL_HOURS)


def test_user_required_reply_pauses_chase(db):
    company = _company(db, waiting_on=WaitingOn.COMPANY, next_followup_at=datetime.datetime(2024, 1, 2))
    chase_engine.on_reply_classified(
        company, DeletionStatus.VERIFICATION_NEEDED, "Please verify your identity.", datetime.datetime(2024, 1, 1, 6),
    )
    assert company.waiting_on == WaitingOn.USER
    assert company.next_followup_at is None


def test_completed_permanently_stops_chase(db):
    company = _company(db, waiting_on=WaitingOn.COMPANY, next_followup_at=datetime.datetime(2024, 1, 2), deletion_status=DeletionStatus.COMPLETED)
    chase_engine.on_reply_classified(company, DeletionStatus.COMPLETED, "Your data has been deleted.", datetime.datetime(2024, 1, 1, 6))
    assert company.waiting_on is None
    assert company.next_followup_at is None


def test_completed_stops_chase_via_full_check_company_response_path(db):
    """Integration-level regression for the real bug found while wiring
    this up: check_company_response sets deletion_status to the new
    (already-terminal) value BEFORE calling on_reply_classified - a naive
    is_chase_eligible() check based on the now-current status would skip
    clearing waiting_on/next_followup_at entirely, leaving a completed
    case looking perpetually 'due' to the background worker."""
    import base64

    def _msg(msg_id, body_text, from_addr, internal_date):
        return {
            "id": msg_id, "labelIds": ["INBOX"], "internalDate": str(internal_date),
            "payload": {"headers": [{"name": "From", "value": from_addr}], "mimeType": "text/plain",
                        "body": {"data": base64.urlsafe_b64encode(body_text.encode()).decode()}},
        }

    company = _company(db, waiting_on=WaitingOn.COMPANY, next_followup_at=datetime.datetime(2024, 1, 2))
    reply = _msg("m2", "Your personal data has been deleted from our systems.", "privacy@widgetco.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_status == DeletionStatus.COMPLETED
    assert company.waiting_on is None
    assert company.next_followup_at is None
    # And the background worker must never pick it up again:
    assert company not in chase_engine.get_companies_due_for_followup(db)


def test_denial_enters_escalation_needed(db):
    company = _company(db, waiting_on=WaitingOn.COMPANY, next_followup_at=datetime.datetime(2024, 1, 2), deletion_status=DeletionStatus.REJECTED)
    chase_engine.on_reply_classified(company, DeletionStatus.REJECTED, "We are unable to fulfill this request.", datetime.datetime(2024, 1, 1, 6))
    assert company.waiting_on == WaitingOn.ESCALATION_NEEDED
    assert company.next_followup_at is None  # paused, not chased - and NOT represented as a successful resolution


def test_unknown_without_signal_keeps_chase_active(db):
    scheduled = datetime.datetime(2024, 1, 2, 12, 0, 0)
    company = _company(db, waiting_on=WaitingOn.COMPANY, next_followup_at=scheduled)
    chase_engine.on_reply_classified(company, DeletionStatus.UNKNOWN_RESPONSE, "Thanks!", datetime.datetime(2024, 1, 2, 1))
    assert company.waiting_on == WaitingOn.COMPANY
    assert company.next_followup_at == scheduled  # already scheduled - unchanged, not paused, not lost


# --- on_user_action_completed: items 7 ---

def test_user_action_completed_always_resets_to_fresh_24h_window(db):
    company = _company(db, waiting_on=WaitingOn.USER, next_followup_at=None)
    completed_at = datetime.datetime(2024, 1, 3, 8, 0, 0)
    chase_engine.on_user_action_completed(company, completed_at)
    assert company.waiting_on == WaitingOn.COMPANY
    assert company.next_followup_at == completed_at + datetime.timedelta(hours=config.FOLLOWUP_INTERVAL_HOURS)


# --- pause/resume: item 10 ---

def test_pause_and_resume_followups(db):
    company = _company(db, waiting_on=WaitingOn.COMPANY, next_followup_at=datetime.datetime(2024, 1, 2))
    chase_engine.pause_followups(company)
    assert company.followups_paused is True
    assert company.waiting_on == WaitingOn.COMPANY  # pausing doesn't change WHO it's waiting on

    resume_at = datetime.datetime(2024, 1, 5, 0, 0, 0)
    chase_engine.resume_followups(company, resume_at)
    assert company.followups_paused is False


def test_resume_schedules_fresh_window_if_unscheduled(db):
    company = _company(db, waiting_on=WaitingOn.COMPANY, next_followup_at=None, followups_paused=True)
    resume_at = datetime.datetime(2024, 1, 5, 0, 0, 0)
    chase_engine.resume_followups(company, resume_at)
    assert company.next_followup_at == resume_at + datetime.timedelta(hours=config.FOLLOWUP_INTERVAL_HOURS)


def test_paused_company_is_never_due(db):
    company = _company(db, waiting_on=WaitingOn.COMPANY, next_followup_at=datetime.datetime(2020, 1, 1), followups_paused=True)
    due = chase_engine.get_companies_due_for_followup(db)
    assert company not in due


# --- send_followup: items 2, 3, 12 ---

def test_due_followup_sends_in_same_thread(db):
    company = _company(db, waiting_on=WaitingOn.COMPANY, next_followup_at=datetime.datetime(2020, 1, 1), followup_attempt=0)
    _outbound(db, company)
    with patch("app.chase_engine._send_followup_email", return_value={"id": "sent-1", "threadId": "thread123"}) as mock_send:
        result = chase_engine.send_followup(db, company, creds=MagicMock(), gmail_address="me@gmail.com")
    assert result is True
    assert mock_send.call_args.args[4] == "thread123"  # thread_id positional arg - same tracked thread, never a new one


def test_successful_send_schedules_next_24h_and_advances_attempt(db):
    sent_before = datetime.datetime.utcnow()
    company = _company(db, waiting_on=WaitingOn.COMPANY, next_followup_at=datetime.datetime(2020, 1, 1), followup_attempt=1)
    _outbound(db, company)
    with patch("app.chase_engine._send_followup_email", return_value={"id": "sent-2", "threadId": "thread123"}):
        chase_engine.send_followup(db, company, creds=MagicMock(), gmail_address="me@gmail.com")
    assert company.followup_attempt == 2
    assert company.next_followup_at >= sent_before + datetime.timedelta(hours=config.FOLLOWUP_INTERVAL_HOURS)
    assert company.followup_locked_at is None
    mm = db.query(MailMessage).filter(MailMessage.gmail_message_id == "sent-2").one()
    assert mm.direction == "outbound"
    event = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id, DeletionEvent.event_type == EventType.FOLLOWUP_SENT).one()
    assert event.evidence["attempt"] == 2
    assert event.evidence["recovered"] is False


def test_followup_template_never_shows_attempt_number_to_company():
    company = Company(name="Widget Co", domain="widgetco.com", deletion_requested_at=datetime.datetime(2024, 1, 1))
    for attempt in (1, 2, 3, 4):
        draft = chase_engine._followup_template(company, "privacy@widgetco.com", "Deletion Request", attempt)
        assert "attempt" not in draft["body"].lower()
        assert "#" not in draft["body"]
        assert "follow-up #" not in draft["body"].lower()
    # attempt 3+ must not say "follow-up #N" per the final locked copy
    draft3 = chase_engine._followup_template(company, "p@w.com", "Deletion Request", 3)
    assert "another follow-up" in draft3["body"].lower()


def test_crash_before_gmail_call_leaves_no_record_and_does_not_advance(db):
    """Simulates the send failing before Gmail ever returns a response -
    the lock must be cleared, nothing recorded as sent, attempt untouched."""
    company = _company(db, waiting_on=WaitingOn.COMPANY, next_followup_at=datetime.datetime(2020, 1, 1), followup_attempt=0)
    _outbound(db, company)
    with patch("app.chase_engine._send_followup_email", side_effect=ConnectionError("boom")):
        result = chase_engine.send_followup(db, company, creds=MagicMock(), gmail_address="me@gmail.com")
    assert result is False
    assert company.followup_attempt == 0
    assert company.followup_locked_at is None
    assert db.query(MailMessage).filter(MailMessage.direction == "outbound", MailMessage.company_id == company.id).count() == 1  # only the original request


# --- reconciliation: items 13, 14, 15 ---

def test_reconciliation_recovers_a_send_gmail_accepted_before_a_crash(db):
    """Crash-after-Gmail-success-but-before-DB-recording: the lock is
    stale, and the thread now shows a SENT message the local DB never
    recorded - reconciliation must recover it as the REAL, evidenced send
    and advance scheduling from ITS real timestamp, never resend."""
    lock_time = datetime.datetime.utcnow() - datetime.timedelta(minutes=config.FOLLOWUP_LOCK_STALE_MINUTES + 1)
    company = _company(db, waiting_on=WaitingOn.COMPANY, followup_attempt=0, followup_locked_at=lock_time)
    _outbound(db, company)
    sent_at = lock_time + datetime.timedelta(seconds=30)
    sent_internal_ms = int((sent_at - datetime.datetime(1970, 1, 1)).total_seconds() * 1000)
    gmail_message = {
        "id": "recovered-1", "labelIds": ["SENT"], "internalDate": str(sent_internal_ms),
        "payload": {"headers": [{"name": "Subject", "value": "Re: Deletion Request"}]},
    }
    with patch("app.google_oauth.fetch_thread_messages", return_value=[gmail_message]):
        resolved = chase_engine.reconcile_stale_followup_locks(db, creds=MagicMock(), gmail_address="me@gmail.com")
    assert resolved == 1
    assert company.followup_locked_at is None
    assert company.followup_attempt == 1
    event = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id, DeletionEvent.event_type == EventType.FOLLOWUP_SENT).one()
    assert event.evidence["recovered"] is True
    assert event.evidence["gmail_message_id"] == "recovered-1"
    mm = db.query(MailMessage).filter(MailMessage.gmail_message_id == "recovered-1").one_or_none()
    assert mm is not None


def test_reconciliation_first_negative_check_defers_rather_than_resends(db):
    """A single 'not found' result is NOT proof nothing was sent - Gmail's
    API gives no documented guarantee that a just-accepted send is
    IMMEDIATELY visible via threads().get(). Just past the initial stale
    threshold (but still well within the confirmation window), a negative
    check must defer - leave the lock in place, touch nothing else - not
    clear the lock and authorize a fresh, potentially duplicate, send."""
    lock_time = datetime.datetime.utcnow() - datetime.timedelta(minutes=config.FOLLOWUP_LOCK_STALE_MINUTES + 1)
    company = _company(db, waiting_on=WaitingOn.COMPANY, followup_attempt=1, followup_locked_at=lock_time, next_followup_at=None)
    _outbound(db, company)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[]):
        resolved = chase_engine.reconcile_stale_followup_locks(db, creds=MagicMock(), gmail_address="me@gmail.com")
    assert resolved == 0  # deferred, not resolved
    assert company.followup_locked_at == lock_time  # untouched - still stale, will be re-checked next tick
    assert company.followup_attempt == 1
    assert company.next_followup_at is None  # NOT rescheduled - a resend is not yet authorized
    assert db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id, DeletionEvent.event_type == EventType.FOLLOWUP_SENT).count() == 0


def test_reconciliation_confirmed_absent_after_full_window_permits_resend(db):
    """Only once the thread has shown nothing across the FULL
    FOLLOWUP_RECONCILE_CONFIRM_MINUTES window since the lock was first set
    is a 'not found' result trusted enough to clear the lock and permit a
    fresh send - the second, time-separated confirmation the safer design
    requires, WITHOUT advancing the attempt counter itself (the next real
    send does that)."""
    lock_time = datetime.datetime.utcnow() - datetime.timedelta(minutes=config.FOLLOWUP_RECONCILE_CONFIRM_MINUTES + 1)
    company = _company(db, waiting_on=WaitingOn.COMPANY, followup_attempt=1, followup_locked_at=lock_time)
    _outbound(db, company)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[]):
        resolved = chase_engine.reconcile_stale_followup_locks(db, creds=MagicMock(), gmail_address="me@gmail.com")
    assert resolved == 1
    assert company.followup_locked_at is None
    assert company.followup_attempt == 1  # unchanged - no resend was recorded
    assert db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id, DeletionEvent.event_type == EventType.FOLLOWUP_SENT).count() == 0
    assert company.next_followup_at is not None
    assert company.next_followup_at <= datetime.datetime.utcnow() + datetime.timedelta(minutes=config.FOLLOWUP_RECONCILE_RETRY_MINUTES + 1)


def test_reconciliation_repeated_negative_checks_eventually_confirm(db):
    """End-to-end across simulated ticks: a lock that stays unresolved (no
    evidence either way) keeps deferring - never double-sends - until the
    confirm window elapses, at which point it resolves exactly once."""
    lock_time = datetime.datetime.utcnow() - datetime.timedelta(minutes=config.FOLLOWUP_LOCK_STALE_MINUTES + 1)
    company = _company(db, waiting_on=WaitingOn.COMPANY, followup_attempt=0, followup_locked_at=lock_time, next_followup_at=None)
    _outbound(db, company)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[]):
        # Ticks while still within the confirmation window - must keep deferring.
        for _ in range(3):
            resolved = chase_engine.reconcile_stale_followup_locks(db, creds=MagicMock(), gmail_address="me@gmail.com")
            assert resolved == 0
            assert company.followup_locked_at is not None

        # Advance the (unchanged) lock past the confirmation window and re-check.
        company.followup_locked_at = datetime.datetime.utcnow() - datetime.timedelta(minutes=config.FOLLOWUP_RECONCILE_CONFIRM_MINUTES + 1)
        db.commit()
        resolved = chase_engine.reconcile_stale_followup_locks(db, creds=MagicMock(), gmail_address="me@gmail.com")
    assert resolved == 1
    assert company.followup_locked_at is None
    assert db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id, DeletionEvent.event_type == EventType.FOLLOWUP_SENT).count() == 0


def test_reconciliation_thread_fetch_failure_leaves_lock_for_next_try(db):
    lock_time = datetime.datetime.utcnow() - datetime.timedelta(minutes=config.FOLLOWUP_LOCK_STALE_MINUTES + 1)
    company = _company(db, waiting_on=WaitingOn.COMPANY, followup_locked_at=lock_time)
    with patch("app.google_oauth.fetch_thread_messages", side_effect=ConnectionError("down")):
        resolved = chase_engine.reconcile_stale_followup_locks(db, creds=MagicMock(), gmail_address="me@gmail.com")
    assert resolved == 0
    assert company.followup_locked_at is not None  # left in place - genuinely can't verify yet, try again later


# --- duplicate-tick / double-send protection: item 11 ---

def test_locked_company_is_excluded_from_due_list(db):
    company = _company(db, waiting_on=WaitingOn.COMPANY, next_followup_at=datetime.datetime(2020, 1, 1), followup_locked_at=datetime.datetime.utcnow())
    due = chase_engine.get_companies_due_for_followup(db)
    assert company not in due


def test_process_followups_does_not_double_send_across_two_ticks(db):
    company = _company(db, waiting_on=WaitingOn.COMPANY, next_followup_at=datetime.datetime(2020, 1, 1), followup_attempt=0)
    _outbound(db, company)
    with patch("app.chase_engine._send_followup_email", return_value={"id": "sent-x", "threadId": "thread123"}) as mock_send:
        first = chase_engine.process_followups(db, creds=MagicMock(), gmail_address="me@gmail.com")
        second = chase_engine.process_followups(db, creds=MagicMock(), gmail_address="me@gmail.com")
    assert first == 1
    assert second == 0  # already rescheduled 24h out - not due again on the very next tick
    assert mock_send.call_count == 1


# --- eligibility gating ---

def test_non_email_request_method_is_never_eligible(db):
    company = Company(name="X", domain="x.com", deletion_method="WEB_FORM", deletion_status=DeletionStatus.SUBMITTED, deletion_thread_id="t1")
    assert chase_engine.is_chase_eligible(company) is False


def test_no_tracked_thread_is_never_eligible(db):
    company = Company(name="X", domain="x.com", deletion_method="EMAIL_REQUEST", deletion_status=DeletionStatus.SUBMITTED, deletion_thread_id=None)
    assert chase_engine.is_chase_eligible(company) is False


# --- item 17: UNKNOWN_RESPONSE must still surface in the Mailbox, never disappear ---

def test_unknown_response_keeps_chase_active_and_stays_visible_in_mailbox(db):
    import base64

    from app import mail

    def _msg(msg_id, body_text, from_addr, internal_date):
        return {
            "id": msg_id, "labelIds": ["INBOX"], "internalDate": str(internal_date),
            "payload": {"headers": [{"name": "From", "value": from_addr}], "mimeType": "text/plain",
                        "body": {"data": base64.urlsafe_b64encode(body_text.encode()).decode()}},
        }

    company = _company(db, waiting_on=None, next_followup_at=None)
    reply = _msg("m2", "Thanks for reaching out!", "privacy@widgetco.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_status == DeletionStatus.UNKNOWN_RESPONSE
    assert company.waiting_on == WaitingOn.COMPANY  # no human-action signal - chase stays active
    assert company.next_followup_at is not None

    entries = mail.mailbox_entries(db)
    assert any(e["company"].id == company.id for e in entries)  # never disappears from the mailbox
