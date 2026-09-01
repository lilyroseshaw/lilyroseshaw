"""Tests for the manual Gmail thread-attach flow (the "smallest safe slice"
for the Lyft-style product gap: a deletion request submitted through a
company's own site/portal/form, not through an email Cookie Monster itself
sent, but which the company later confirmed by email).

Covers both layers:
- app/google_oauth.py: parse_gmail_message_ref, fetch_message_preview
  (pure parsing / metadata-only lookup, mocked Gmail API)
- app/main.py: the two-step preview -> confirm routes, via a real
  request/response round trip (FastAPI TestClient), including that the
  existing Phase 2 response tracker picks up an already-received company
  reply once a thread is attached - proving the hand-off needs no changes
  to deletion_response_tracker.py.
"""
import base64
import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config, google_oauth
from app.db import Base
from app.deletion_constants import DeletionStatus, EventSource, EventType
from app.deletion_response_tracker import check_company_response
from app.main import app
from app.models import Company, DeletionEvent, OAuthToken
from app.response_classify import ResponseClassifier

import app.db as dbmod


# --- google_oauth.parse_gmail_message_ref: pure parsing, no network ---

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("18abfa2e91234567", "18abfa2e91234567"),
        ("https://mail.google.com/mail/u/0/#inbox/18abfa2e91234567", "18abfa2e91234567"),
        ("https://mail.google.com/mail/u/0/#all/18abfa2e91234567", "18abfa2e91234567"),
        ("  18abfa2e91234567  ", "18abfa2e91234567"),
        ("https://mail.google.com/mail/u/0/#inbox/18abfa2e91234567/", "18abfa2e91234567"),
    ],
)
def test_parse_gmail_message_ref_common_formats(raw, expected):
    assert google_oauth.parse_gmail_message_ref(raw) == expected


def test_parse_gmail_message_ref_rejects_empty():
    with pytest.raises(ValueError):
        google_oauth.parse_gmail_message_ref("   ")


# --- google_oauth.fetch_message_preview: metadata-only, single known ID ---

def test_fetch_message_preview_returns_headers_only_never_body():
    fake_message = {
        "id": "msg123",
        "threadId": "thread123",
        "payload": {
            "headers": [
                {"name": "From", "value": "privacy@lyft.com"},
                {"name": "Subject", "value": "Your data deletion request"},
                {"name": "Date", "value": "Mon, 31 Aug 2026 10:00:00 -0700"},
            ]
        },
        # A real Gmail response could include a body here - the function
        # must never read/return it even if the API returned one.
        "snippet": "should not leak into the result",
    }
    mock_service = MagicMock()
    mock_service.users.return_value.messages.return_value.get.return_value.execute.return_value = fake_message

    with patch("app.google_oauth.build", return_value=mock_service):
        result = google_oauth.fetch_message_preview(MagicMock(), "msg123")

    assert result == {
        "thread_id": "thread123",
        "message_id": "msg123",
        "from": "privacy@lyft.com",
        "subject": "Your data deletion request",
        "date": "Mon, 31 Aug 2026 10:00:00 -0700",
    }
    # Requested format=metadata with an explicit header allow-list - never asked for the body.
    _, kwargs = mock_service.users.return_value.messages.return_value.get.call_args
    assert kwargs["format"] == "metadata"
    assert set(kwargs["metadataHeaders"]) == {"From", "Subject", "Date"}


# --- main.py routes: full request/response round trip ---

@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(dbmod, "engine", engine)
    monkeypatch.setattr(dbmod, "SessionLocal", Session)
    monkeypatch.setattr(config, "DELETION_QUEUE_INTERVAL_SECONDS", 9999)
    session = Session()
    yield session
    session.close()


def _lyft(db, **overrides) -> Company:
    defaults = dict(
        name="Lyft", domain="lyft.com", relationship_type="transactional", status="confirmed",
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        deletion_status=DeletionStatus.SUBMITTED, deletion_method="WEB_FORM",
        deletion_requested_at=datetime.datetime(2026, 8, 31),
        deletion_evidence={"type": "user_reported", "note": "Submitted before Cookie Monster tracked send evidence."},
    )
    defaults.update(overrides)
    company = Company(**defaults)
    db.add(company)
    db.commit()
    return company


def _grant_readonly(db):
    db.add(OAuthToken(
        gmail_address="me@gmail.com", encrypted_refresh_token="fake-encrypted",
        scopes_granted=f"{config.GMAIL_SCOPES[0]} {config.GMAIL_READONLY_SCOPE}",
    ))
    db.commit()


@pytest.fixture()
def client(db):
    return TestClient(app, base_url="http://localhost:8000")


def _fake_preview(thread_id="thread-lyft-1", message_id="msg-lyft-1"):
    return {
        "thread_id": thread_id, "message_id": message_id,
        "from": "privacy@lyft.com", "subject": "Your Lyft data deletion request",
        "date": "Mon, 31 Aug 2026 12:00:00 -0700",
    }


def test_preview_requires_readonly_scope(db, client):
    company = _lyft(db)
    resp = client.post(
        f"/api/companies/{company.id}/deletion/attach-thread/preview",
        data={"gmail_ref": "msg-lyft-1"},
    )
    assert resp.status_code == 200
    assert "Response tracking is not enabled" in resp.text
    assert company.deletion_thread_id is None


def test_preview_valid_message_shows_headers_only(db, client):
    company = _lyft(db)
    _grant_readonly(db)
    with patch("app.google_oauth.load_credentials", return_value=MagicMock()), \
         patch("app.google_oauth.fetch_message_preview", return_value=_fake_preview()):
        resp = client.post(
            f"/api/companies/{company.id}/deletion/attach-thread/preview",
            data={"gmail_ref": "https://mail.google.com/mail/u/0/#inbox/msg-lyft-1"},
        )
    assert resp.status_code == 200
    assert "privacy@lyft.com" in resp.text
    assert "Your Lyft data deletion request" in resp.text
    # Not yet attached - preview alone must never save anything.
    db.refresh(company)
    assert company.deletion_thread_id is None


def test_preview_rejects_nonexistent_message(db, client):
    company = _lyft(db)
    _grant_readonly(db)

    class _FakeHttpError(HttpError):
        def __init__(self):
            self.resp = type("Resp", (), {"status": 404})()
            self.content = b"error"
            self.uri = "https://fake"

        def __str__(self):
            return "HttpError 404"

    with patch("app.google_oauth.load_credentials", return_value=MagicMock()), \
         patch("app.google_oauth.fetch_message_preview", side_effect=_FakeHttpError()):
        resp = client.post(
            f"/api/companies/{company.id}/deletion/attach-thread/preview",
            data={"gmail_ref": "does-not-exist"},
        )
    assert resp.status_code == 200
    assert "double-check the link/ID" in resp.text
    db.refresh(company)
    assert company.deletion_thread_id is None


def test_preview_ineligible_when_thread_already_attached(db, client):
    company = _lyft(db, deletion_thread_id="already-attached")
    _grant_readonly(db)
    resp = client.post(
        f"/api/companies/{company.id}/deletion/attach-thread/preview",
        data={"gmail_ref": "msg-lyft-1"},
    )
    assert "already has a tracked email thread" in resp.text


def test_preview_ineligible_for_terminal_status(db, client):
    company = _lyft(db, deletion_status=DeletionStatus.COMPLETED)
    _grant_readonly(db)
    resp = client.post(
        f"/api/companies/{company.id}/deletion/attach-thread/preview",
        data={"gmail_ref": "msg-lyft-1"},
    )
    assert "already resolved" in resp.text


def test_confirm_requires_prior_preview_style_validation(db, client):
    """Confirm re-validates independently - it must reject just as readily
    as preview does if eligibility no longer holds, e.g. hitting confirm
    directly without ever previewing is fine (it just re-resolves), but
    hitting it when a thread is ALREADY attached must be rejected."""
    company = _lyft(db, deletion_thread_id="already-attached")
    _grant_readonly(db)
    resp = client.post(
        f"/api/companies/{company.id}/deletion/attach-thread/confirm",
        data={"message_id": "msg-lyft-1"},
    )
    assert resp.status_code == 400
    assert "already has a tracked email thread" in resp.json()["detail"]


def test_confirm_stores_thread_id_and_records_user_event(db, client):
    company = _lyft(db)
    _grant_readonly(db)
    with patch("app.google_oauth.load_credentials", return_value=MagicMock()), \
         patch("app.google_oauth.fetch_message_preview", return_value=_fake_preview()):
        resp = client.post(
            f"/api/companies/{company.id}/deletion/attach-thread/confirm",
            data={"message_id": "msg-lyft-1"},
            follow_redirects=False,
        )
    assert resp.status_code == 303

    db.refresh(company)
    assert company.deletion_thread_id == "thread-lyft-1"

    events = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).all()
    assert len(events) == 1
    event = events[0]
    assert event.event_type == EventType.THREAD_ASSOCIATED
    assert event.source == EventSource.USER
    assert event.evidence["message_id"] == "msg-lyft-1"
    assert event.evidence["thread_id"] == "thread-lyft-1"
    assert event.evidence["from"] == "privacy@lyft.com"
    assert event.evidence["subject"] == "Your Lyft data deletion request"
    # No body/snippet anywhere in what was stored.
    assert "snippet" not in event.evidence
    assert all("body" not in str(k).lower() for k in event.evidence)


def test_confirm_never_touches_original_submission_evidence(db, client):
    """The requirement: Lyft must remain historically recorded as
    user-reported/manual, even after a thread is attached."""
    company = _lyft(db)
    _grant_readonly(db)
    original_evidence = dict(company.deletion_evidence)
    original_status = company.deletion_status
    original_requested_at = company.deletion_requested_at

    with patch("app.google_oauth.load_credentials", return_value=MagicMock()), \
         patch("app.google_oauth.fetch_message_preview", return_value=_fake_preview()):
        client.post(
            f"/api/companies/{company.id}/deletion/attach-thread/confirm",
            data={"message_id": "msg-lyft-1"},
        )

    db.refresh(company)
    assert company.deletion_evidence == original_evidence
    assert company.deletion_evidence["type"] == "user_reported"
    assert company.deletion_status == original_status  # still SUBMITTED, not changed
    assert company.deletion_requested_at == original_requested_at


def test_confirm_never_changes_deletion_status_by_itself(db, client):
    """Attaching a thread must never itself mark IN_PROGRESS/COMPLETED/
    anything else - only actual response classification (a separate,
    later step) may do that."""
    company = _lyft(db)
    _grant_readonly(db)
    with patch("app.google_oauth.load_credentials", return_value=MagicMock()), \
         patch("app.google_oauth.fetch_message_preview", return_value=_fake_preview()):
        client.post(
            f"/api/companies/{company.id}/deletion/attach-thread/confirm",
            data={"message_id": "msg-lyft-1"},
        )
    db.refresh(company)
    assert company.deletion_status == DeletionStatus.SUBMITTED


def test_confirm_requires_readonly_scope(db, client):
    company = _lyft(db)
    resp = client.post(
        f"/api/companies/{company.id}/deletion/attach-thread/confirm",
        data={"message_id": "msg-lyft-1"},
    )
    assert resp.status_code == 400
    assert "not enabled" in resp.json()["detail"]
    db.refresh(company)
    assert company.deletion_thread_id is None


def test_confirm_is_a_distinct_step_from_preview(db, client):
    """Requirement 4: an explicit SECOND confirmation is required - preview
    alone (however many times) must never attach anything by itself."""
    company = _lyft(db)
    _grant_readonly(db)
    with patch("app.google_oauth.load_credentials", return_value=MagicMock()), \
         patch("app.google_oauth.fetch_message_preview", return_value=_fake_preview()):
        for _ in range(3):
            client.post(
                f"/api/companies/{company.id}/deletion/attach-thread/preview",
                data={"gmail_ref": "msg-lyft-1"},
            )
    db.refresh(company)
    assert company.deletion_thread_id is None
    assert db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count() == 0


# --- hand-off: once attached, the EXISTING (unchanged) response tracker
# picks up the already-received company reply ---

def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def test_attached_thread_is_picked_up_by_existing_response_tracker(db, client):
    """The whole point of the slice: after attaching, deletion_response_tracker.py
    (completely unmodified) must be able to classify the confirmation email
    that was ALREADY sitting in the thread before it was ever attached."""
    company = _lyft(db)
    _grant_readonly(db)
    with patch("app.google_oauth.load_credentials", return_value=MagicMock()), \
         patch("app.google_oauth.fetch_message_preview", return_value=_fake_preview(thread_id="thread-lyft-1")):
        client.post(
            f"/api/companies/{company.id}/deletion/attach-thread/confirm",
            data={"message_id": "msg-lyft-1"},
        )
    db.refresh(company)
    assert company.deletion_thread_id == "thread-lyft-1"

    # The confirmation email was already in the thread BEFORE it was
    # attached (deletion_last_response_message_id is still unset).
    already_received_reply = {
        "id": "msg-lyft-1", "labelIds": ["INBOX"], "internalDate": "1000",
        "payload": {
            "headers": [{"name": "From", "value": "privacy@lyft.com"}],
            "mimeType": "text/plain",
            "body": {"data": _b64("Your Lyft data has been deleted from our systems.")},
        },
    }
    with patch("app.google_oauth.fetch_thread_messages", return_value=[already_received_reply]):
        check_company_response(
            db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier(),
        )

    assert company.deletion_status == DeletionStatus.COMPLETED
    assert company.deletion_completed_at is not None
