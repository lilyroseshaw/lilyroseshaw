"""Tests for the generic execution engine's first slice: EMAIL_REQUEST as
the one real AUTO_EXECUTABLE path, capability plumbing for WEB_FORM/
PRIVACY_PORTAL/ACCOUNT_SETTING (MANUAL_HANDOFF in this slice), and the
status/evidence/concurrency integrity guarantees around all of it.
"""
import datetime
import threading
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config, deletion_engine
from app.db import Base
from app.deletion_constants import (
    DeletionMethod,
    DeletionStatus,
    EventType,
    ExecutionCapability,
    RecipeStatus,
    is_system_verified,
)
from app.deletion_response_tracker import check_company_response, get_companies_due_for_check
from app.models import Company, DeletionEvent, DeletionRecipe, OAuthToken
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
        deletion_method=DeletionMethod.EMAIL_REQUEST,
        deletion_status=DeletionStatus.READY,
        deletion_email="privacy@widgetco.com",
        deletion_verified=True,
    )
    defaults.update(overrides)
    company = Company(**defaults)
    db.add(company)
    db.commit()
    return company


def _grant_send_scope(db) -> None:
    db.add(OAuthToken(
        gmail_address="me@gmail.com", encrypted_refresh_token="x",
        scopes_granted=" ".join(config.GMAIL_SCOPES + [config.GMAIL_SEND_SCOPE]),
    ))
    db.commit()


def _verified_recipe(db, domain, **overrides) -> DeletionRecipe:
    now = datetime.datetime.utcnow()
    defaults = dict(
        domain=domain, method=DeletionMethod.EMAIL_REQUEST, email="privacy@" + domain,
        status=RecipeStatus.VERIFIED, confidence="high", source_url=f"https://{domain}/privacy",
        verified_at=now, expires_at=now + datetime.timedelta(days=150), required_request_fields=[],
    )
    defaults.update(overrides)
    recipe = DeletionRecipe(**defaults)
    db.add(recipe)
    db.commit()
    return recipe


# --- 1. Verified EMAIL_REQUEST + approval + gmail.send success ---

def test_verified_email_request_sends_exactly_once_and_marks_submitted(db):
    company = _company(db)
    _grant_send_scope(db)

    with patch("app.google_oauth.load_credentials", return_value=MagicMock()), \
         patch("app.google_oauth.send_email", return_value={"id": "msg-1", "threadId": "thread-1"}) as mock_send:
        deletion_engine.execute_deletion(db, company)

    mock_send.assert_called_once()
    assert company.deletion_status == DeletionStatus.SUBMITTED
    assert company.deletion_thread_id == "thread-1"
    assert company.deletion_evidence["type"] == "gmail_send"
    assert company.deletion_evidence["gmail_message_id"] == "msg-1"
    assert is_system_verified(company.deletion_status, company.deletion_evidence)

    events = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).all()
    event_types = [e.event_type for e in events]
    assert EventType.EXECUTION_STARTED in event_types  # auditable BEFORE the send, not just after
    assert EventType.EMAIL_SENT in event_types


# --- 2. Send failure => not SUBMITTED ---

def test_send_failure_never_marks_submitted(db):
    company = _company(db)
    _grant_send_scope(db)

    with patch("app.google_oauth.load_credentials", return_value=MagicMock()), \
         patch("app.google_oauth.send_email", side_effect=RuntimeError("Gmail API exploded")):
        deletion_engine.execute_deletion(db, company)

    assert company.deletion_status == DeletionStatus.FAILED
    assert company.deletion_status not in DeletionStatus.SYSTEM_VERIFIED
    assert "Gmail API exploded" in company.deletion_error

    events = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).all()
    event_types = [e.event_type for e in events]
    assert EventType.EXECUTION_STARTED in event_types  # the attempt is still auditable
    assert EventType.EMAIL_SENT not in event_types


# --- 3. Double-click / concurrent approval => one send only ---

def test_concurrent_approval_sends_exactly_once(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'exec.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    setup_db = Session()
    company = _company(setup_db)
    company_id = company.id
    _grant_send_scope(setup_db)
    setup_db.close()

    started = threading.Event()
    release = threading.Event()
    call_count = {"n": 0}
    lock = threading.Lock()

    def slow_send(creds, to, subject, body):
        with lock:
            call_count["n"] += 1
        started.set()
        release.wait(timeout=5)
        return {"id": "msg-1", "threadId": "thread-1"}

    results = {}

    def run_first():
        db_a = Session()
        company_a = db_a.get(Company, company_id)
        try:
            deletion_engine.execute_deletion(db_a, company_a)
            results["first"] = "ok"
        except deletion_engine.ExecutionInFlightError:
            results["first"] = "in_flight"
        db_a.close()

    with patch("app.google_oauth.load_credentials", return_value=MagicMock()), \
         patch("app.google_oauth.send_email", side_effect=slow_send):
        thread_a = threading.Thread(target=run_first)
        thread_a.start()
        assert started.wait(timeout=5)

        db_b = Session()
        company_b = db_b.get(Company, company_id)
        try:
            deletion_engine.execute_deletion(db_b, company_b)
            results["second"] = "ok"
        except deletion_engine.ExecutionInFlightError:
            results["second"] = "in_flight"
        db_b.close()

        release.set()
        thread_a.join(timeout=5)

    assert call_count["n"] == 1  # only one Gmail send ever actually happened
    assert sorted([results["first"], results["second"]]) == ["in_flight", "ok"]

    verify_db = Session()
    company = verify_db.get(Company, company_id)
    assert company.deletion_status == DeletionStatus.SUBMITTED
    events = verify_db.query(DeletionEvent).filter(
        DeletionEvent.company_id == company_id, DeletionEvent.event_type == EventType.EMAIL_SENT
    ).all()
    assert len(events) == 1  # never double-recorded either
    verify_db.close()


# --- 4. Missing gmail.send scope => no send, USER_STEP_REQUIRED ---

def test_missing_send_scope_never_sends(db):
    company = _company(db)
    # No OAuthToken row at all - has_send_scope() is False by default.
    with patch("app.google_oauth.send_email") as mock_send:
        deletion_engine.execute_deletion(db, company)
        mock_send.assert_not_called()

    assert company.deletion_status == DeletionStatus.USER_ACTION_REQUIRED
    assert company.deletion_evidence["type"] == "draft_only"
    assert company.deletion_status not in DeletionStatus.SYSTEM_VERIFIED


# --- 5. Missing required identity field => no send, USER_STEP_REQUIRED ---

def test_missing_required_identity_field_never_sends(db):
    company = _company(db)
    _grant_send_scope(db)  # send IS enabled - proves the field check gates independently
    _verified_recipe(db, "widgetco.com", required_request_fields=["full_name"])

    with patch("app.google_oauth.load_credentials", return_value=MagicMock()), \
         patch("app.google_oauth.send_email") as mock_send:
        deletion_engine.execute_deletion(db, company)
        mock_send.assert_not_called()

    assert company.deletion_status == DeletionStatus.USER_ACTION_REQUIRED
    assert company.deletion_status not in DeletionStatus.SYSTEM_VERIFIED
    assert "full_name" in company.deletion_evidence["missing_identity_fields"]
    assert "full_name" in company.deletion_instructions


def test_account_email_alone_does_not_block_auto_send(db):
    """account_email is the one identity field Cookie Monster can always
    supply (the connected Gmail address) - it must never, by itself, force
    USER_STEP_REQUIRED."""
    company = _company(db)
    _grant_send_scope(db)
    _verified_recipe(db, "widgetco.com", required_request_fields=["account_email"])

    with patch("app.google_oauth.load_credentials", return_value=MagicMock()), \
         patch("app.google_oauth.send_email", return_value={"id": "msg-1", "threadId": "thread-1"}) as mock_send:
        deletion_engine.execute_deletion(db, company)

    mock_send.assert_called_once()
    assert company.deletion_status == DeletionStatus.SUBMITTED


# --- 6. Unverified recipe => execution refused ---

def test_unverified_recipe_execution_refused(db):
    company = _company(db, deletion_verified=False)
    _grant_send_scope(db)

    with patch("app.google_oauth.send_email") as mock_send:
        with pytest.raises(deletion_engine.UnverifiedRecipeError):
            deletion_engine.execute_deletion(db, company)
        mock_send.assert_not_called()

    # Refused BEFORE anything changed - not silently downgraded to some
    # other status, nothing committed as a side effect of the refusal.
    assert company.deletion_status == DeletionStatus.READY
    assert db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count() == 0


# --- 7. WEB_FORM / PRIVACY_PORTAL handoff => opening route does NOT mark SUBMITTED ---

def test_web_form_handoff_never_marks_submitted(db):
    company = _company(
        db, deletion_method=DeletionMethod.WEB_FORM, deletion_url="https://widgetco.com/privacy/delete",
    )
    deletion_engine.execute_deletion(db, company)

    assert company.deletion_status == DeletionStatus.USER_ACTION_REQUIRED
    assert company.deletion_status != DeletionStatus.SUBMITTED
    assert company.deletion_status not in DeletionStatus.SYSTEM_VERIFIED

    event = db.query(DeletionEvent).filter(
        DeletionEvent.company_id == company.id, DeletionEvent.event_type == EventType.PORTAL_OPENED
    ).one()
    assert event.evidence["url"] == "https://widgetco.com/privacy/delete"


def test_privacy_portal_classifies_as_manual_handoff(db):
    company = _company(
        db, deletion_method=DeletionMethod.PRIVACY_PORTAL, deletion_url="https://widgetco.com/portal",
    )
    plan = deletion_engine.classify_execution_capability(db, company)
    assert plan.capability == ExecutionCapability.MANUAL_HANDOFF
    assert plan.url == "https://widgetco.com/portal"
    assert plan.draft is None


# --- 8. Legacy user-reported evidence is preserved, never touched by recovery ---

def test_legacy_user_reported_submitted_evidence_is_never_migrated(db):
    company = _company(
        db,
        deletion_status=DeletionStatus.SUBMITTED,
        deletion_evidence={"type": "user_reported", "legacy": True, "note": None, "reported_at": "2023-01-01T00:00:00"},
    )
    assert not is_system_verified(company.deletion_status, company.deletion_evidence)

    recovered = deletion_engine.recover_stuck_submitting(db)  # must never touch a SUBMITTED (not SUBMITTING) row

    assert recovered == 0
    db.refresh(company)
    assert company.deletion_status == DeletionStatus.SUBMITTED
    assert company.deletion_evidence["type"] == "user_reported"
    assert company.deletion_evidence["legacy"] is True


def test_recover_stuck_submitting_flags_for_review_never_auto_resends(db):
    """A row stuck in SUBMITTING means a previous process died mid-send -
    whether Gmail actually sent it is genuinely unknown, so recovery must
    never silently make it retryable again (that risks a real duplicate
    email); it must surface for the user to check themselves."""
    company = _company(db, deletion_status=DeletionStatus.SUBMITTING)

    recovered = deletion_engine.recover_stuck_submitting(db)

    assert recovered == 1
    db.refresh(company)
    assert company.deletion_status == DeletionStatus.USER_ACTION_REQUIRED
    assert "interrupted" in company.deletion_error.lower()
    event = db.query(DeletionEvent).filter(
        DeletionEvent.company_id == company.id, DeletionEvent.event_type == EventType.EXECUTION_INTERRUPTED
    ).one()
    assert event.evidence["previous_status"] == "SUBMITTING"


# --- 9. Response tracker continues from the newly-created Gmail thread ---

def test_response_tracker_continues_from_new_deletion_thread(db):
    company = _company(db)
    _grant_send_scope(db)

    with patch("app.google_oauth.load_credentials", return_value=MagicMock()), \
         patch("app.google_oauth.send_email", return_value={"id": "msg-1", "threadId": "thread-99"}):
        deletion_engine.execute_deletion(db, company)

    assert company.deletion_thread_id == "thread-99"

    due = get_companies_due_for_check(db)
    assert company.id in [c.id for c in due]  # picked up automatically - no separate wiring needed

    reply_message = {
        "id": "reply-1",
        "labelIds": ["INBOX"],
        "internalDate": "1700000000000",
        "payload": {
            "headers": [{"name": "From", "value": "privacy@widgetco.com"}],
            "mimeType": "text/plain",
            "body": {"data": "V2UgaGF2ZSByZWNlaXZlZCB5b3VyIHJlcXVlc3Qu"},  # "We have received your request."
        },
    }
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply_message]):
        check_company_response(db, company, MagicMock(), "me@gmail.com", ResponseClassifier())

    assert company.deletion_last_response_message_id == "reply-1"
    assert company.deletion_status in DeletionStatus.ALL


# --- 10. No general inbox search introduced by execution ---

def test_execution_never_reads_the_inbox(db):
    company = _company(db)
    _grant_send_scope(db)

    with patch("app.google_oauth.load_credentials", return_value=MagicMock()), \
         patch("app.google_oauth.send_email", return_value={"id": "msg-1", "threadId": "thread-1"}), \
         patch("app.google_oauth.fetch_thread_messages") as mock_fetch:
        deletion_engine.execute_deletion(db, company)
        mock_fetch.assert_not_called()  # sending never reads anything, targeted or otherwise


# --- Capability classification, generally ---

def test_classify_auto_executable_email_request(db):
    company = _company(db)
    _grant_send_scope(db)
    plan = deletion_engine.classify_execution_capability(db, company)
    assert plan.capability == ExecutionCapability.AUTO_EXECUTABLE
    assert plan.reason is None
    assert plan.draft["to"] == "privacy@widgetco.com"


def test_classify_user_step_required_without_send_scope(db):
    company = _company(db)
    plan = deletion_engine.classify_execution_capability(db, company)
    assert plan.capability == ExecutionCapability.USER_STEP_REQUIRED
    assert plan.reason
    assert plan.draft is not None  # still shows the exact email to send yourself


def test_classify_never_used_without_verification_check_upstream(db):
    """classify_execution_capability itself doesn't re-check deletion_verified
    (require_verified_recipe is the gate, called first by execute_deletion) -
    this documents that boundary so it's never accidentally skipped."""
    company = _company(db, deletion_verified=False)
    with pytest.raises(deletion_engine.UnverifiedRecipeError):
        deletion_engine.require_verified_recipe(company)
