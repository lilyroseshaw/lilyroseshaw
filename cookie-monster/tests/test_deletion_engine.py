import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import deletion_engine
from app.db import Base
from app.deletion_constants import ActionCapability, DeletionMethod, DeletionStatus
from app.models import Company, OAuthToken


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _confirmed_company(db, **overrides) -> Company:
    defaults = dict(
        name="Test Co",
        domain="testco.com",
        relationship_type="transactional",
        status="confirmed",
        confidence="high",
        evidence_count=1,
        evidence_types=[],
        example_subjects=[],
        detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1),
        last_seen=datetime.datetime(2022, 1, 1),
        deletion_method=DeletionMethod.UNKNOWN,
        deletion_action_capability=ActionCapability.UNKNOWN,
        deletion_status=DeletionStatus.READY,
        deletion_verified=True,
    )
    defaults.update(overrides)
    company = Company(**defaults)
    db.add(company)
    db.commit()
    return company


# --- WEB_FORM / ACCOUNT_SETTING: never automated, always routes to the user ---

def test_web_form_never_auto_submits(db):
    company = _confirmed_company(
        db, deletion_method=DeletionMethod.WEB_FORM, deletion_url="https://testco.com/privacy"
    )
    deletion_engine.execute_deletion(db, company)
    assert company.deletion_status == DeletionStatus.USER_ACTION_REQUIRED
    assert company.deletion_status not in DeletionStatus.SYSTEM_VERIFIED


def test_account_setting_never_auto_submits(db):
    company = _confirmed_company(
        db, deletion_method=DeletionMethod.ACCOUNT_SETTING, deletion_url="https://testco.com/account/delete"
    )
    deletion_engine.execute_deletion(db, company)
    assert company.deletion_status == DeletionStatus.USER_ACTION_REQUIRED
    assert company.deletion_status not in DeletionStatus.SYSTEM_VERIFIED


def test_web_form_without_a_url_is_unknown_not_fabricated(db):
    company = _confirmed_company(db, deletion_method=DeletionMethod.WEB_FORM, deletion_url=None)
    deletion_engine.execute_deletion(db, company)
    assert company.deletion_status == DeletionStatus.UNKNOWN
    assert company.deletion_status not in DeletionStatus.SYSTEM_VERIFIED


# --- EMAIL_REQUEST: draft-only unless the separate send scope is granted ---

def test_email_request_without_send_scope_is_draft_only(db):
    company = _confirmed_company(
        db, deletion_method=DeletionMethod.EMAIL_REQUEST, deletion_email="privacy@testco.com"
    )
    with patch("app.google_oauth.has_send_scope", return_value=False):
        deletion_engine.execute_deletion(db, company)
    assert company.deletion_status == DeletionStatus.USER_ACTION_REQUIRED
    assert company.deletion_evidence["type"] == "draft_only"
    assert company.deletion_status not in DeletionStatus.SYSTEM_VERIFIED


def test_email_request_with_send_scope_sends_and_is_submitted(db):
    db.add(OAuthToken(gmail_address="me@gmail.com", encrypted_refresh_token="x", scopes_granted="gmail.metadata gmail.send"))
    db.commit()
    company = _confirmed_company(
        db, deletion_method=DeletionMethod.EMAIL_REQUEST, deletion_email="privacy@testco.com"
    )
    with patch("app.google_oauth.has_send_scope", return_value=True), \
         patch("app.google_oauth.load_credentials", return_value=MagicMock()), \
         patch("app.google_oauth.send_email", return_value={"id": "gmail-msg-123"}) as mock_send:
        deletion_engine.execute_deletion(db, company)

    mock_send.assert_called_once()
    assert company.deletion_status == DeletionStatus.SUBMITTED
    assert company.deletion_evidence["gmail_message_id"] == "gmail-msg-123"
    assert company.deletion_requested_at is not None


def test_email_send_failure_is_marked_failed_not_submitted(db):
    company = _confirmed_company(
        db, deletion_method=DeletionMethod.EMAIL_REQUEST, deletion_email="privacy@testco.com"
    )
    with patch("app.google_oauth.has_send_scope", return_value=True), \
         patch("app.google_oauth.load_credentials", return_value=MagicMock()), \
         patch("app.google_oauth.send_email", side_effect=RuntimeError("SMTP exploded")):
        deletion_engine.execute_deletion(db, company)

    assert company.deletion_status == DeletionStatus.FAILED
    assert "SMTP exploded" in company.deletion_error
    assert company.deletion_status not in DeletionStatus.SYSTEM_VERIFIED


# --- UNKNOWN: never submits anything ---

def test_unknown_method_never_submits(db):
    company = _confirmed_company(db, deletion_method=DeletionMethod.UNKNOWN, deletion_status=DeletionStatus.UNKNOWN)
    deletion_engine.execute_deletion(db, company)
    assert company.deletion_status == DeletionStatus.UNKNOWN
    assert company.deletion_status not in DeletionStatus.SYSTEM_VERIFIED


# --- Duplicate-click prevention ---

def test_duplicate_click_is_refused_without_force(db):
    company = _confirmed_company(
        db,
        deletion_method=DeletionMethod.EMAIL_REQUEST,
        deletion_status=DeletionStatus.SUBMITTED,
        deletion_evidence={"type": "gmail_send", "gmail_message_id": "abc"},
    )
    with pytest.raises(deletion_engine.DuplicateRequestWarning):
        deletion_engine.execute_deletion(db, company)
    # Status must be untouched by the refused attempt.
    assert company.deletion_status == DeletionStatus.SUBMITTED


def test_force_resend_after_duplicate_warning_proceeds(db):
    company = _confirmed_company(
        db,
        deletion_method=DeletionMethod.WEB_FORM,
        deletion_url="https://testco.com/privacy",
        deletion_status=DeletionStatus.COMPLETED,
    )
    deletion_engine.execute_deletion(db, company, force_resend=True)
    assert company.deletion_status == DeletionStatus.USER_ACTION_REQUIRED


def test_only_confirmed_companies_can_be_executed(db):
    company = _confirmed_company(db, status="pending", deletion_method=DeletionMethod.WEB_FORM)
    with pytest.raises(ValueError):
        deletion_engine.execute_deletion(db, company)


# --- Self-report path: COMPLETED, never SUBMITTED ---

def test_mark_user_completed_is_completed_not_submitted():
    company = Company(
        name="Test Co", domain="testco.com", relationship_type="transactional", status="confirmed",
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        deletion_status=DeletionStatus.USER_ACTION_REQUIRED,
    )
    deletion_engine.mark_user_completed(company, "Confirmation #4821")
    assert company.deletion_status == DeletionStatus.COMPLETED
    assert company.deletion_status not in DeletionStatus.SYSTEM_VERIFIED
    assert company.deletion_evidence["type"] == "user_reported"
    assert company.deletion_evidence["note"] == "Confirmation #4821"
    assert company.deletion_completed_at is not None
