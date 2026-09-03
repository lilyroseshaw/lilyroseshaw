"""UX regression: "Check for responses" used to dump the user at the top
of the dashboard with no feedback at all. It must now redirect back to
the SAME company's card and show what happened - without ever changing
deletion_status merely to have something to display.
"""
import base64
import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config
from app.db import Base
from app.deletion_constants import DeletionStatus
from app.models import Company, OAuthToken


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _msg(msg_id, body_text, from_addr, internal_date):
    return {
        "id": msg_id,
        "labelIds": ["INBOX"],
        "internalDate": str(internal_date),
        "payload": {
            "headers": [{"name": "From", "value": from_addr}],
            "mimeType": "text/plain",
            "body": {"data": _b64(body_text)},
        },
    }


def _client_db(tmp_path, monkeypatch):
    import app.db as dbmod

    path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(dbmod, "engine", engine)
    monkeypatch.setattr(dbmod, "SessionLocal", Session)
    monkeypatch.setattr(config, "DELETION_QUEUE_INTERVAL_SECONDS", 9999)
    return Session()


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


def _grant_readonly_scope(db) -> None:
    db.add(OAuthToken(
        gmail_address="me@gmail.com", encrypted_refresh_token="x",
        scopes_granted=" ".join(config.GMAIL_SCOPES + [config.GMAIL_READONLY_SCOPE]),
    ))
    db.commit()


def test_check_response_redirects_to_company_card_with_new_response_result(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch

    from fastapi.testclient import TestClient
    from app.main import app

    db = _client_db(tmp_path, monkeypatch)
    company = _grant_and_company(db)
    reply = _msg("m2", "Please verify your identity to proceed.", "privacy@goopkitchen.com", 2000)

    client = TestClient(app, base_url="http://localhost:8000")
    with patch("app.google_oauth.load_credentials", return_value=MagicMock()), \
         patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        resp = client.post(f"/api/companies/{company.id}/deletion/check-response", follow_redirects=False)

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert f"checked={company.id}" in location
    assert "check_result=new_response" in location
    assert "check_status=VERIFICATION_NEEDED" in location
    assert f"#company-{company.id}" in location

    dashboard_resp = client.get(location.split("#")[0])
    html = dashboard_resp.text
    assert "Response found" in html
    assert "verification required" in html.lower()
    assert f'id="company-{company.id}"' in html
    assert "just-updated" in html


def test_no_new_response_never_changes_status_and_says_so(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch

    from fastapi.testclient import TestClient
    from app.main import app

    db = _client_db(tmp_path, monkeypatch)
    company = _grant_and_company(db)
    original_status = company.deletion_status

    client = TestClient(app, base_url="http://localhost:8000")
    with patch("app.google_oauth.load_credentials", return_value=MagicMock()), \
         patch("app.google_oauth.fetch_thread_messages", return_value=[]):
        resp = client.post(f"/api/companies/{company.id}/deletion/check-response", follow_redirects=False)

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "check_result=no_new_response" in location

    dashboard_resp = client.get(location.split("#")[0])
    assert "No new response yet." in dashboard_resp.text

    db.refresh(company)
    assert company.deletion_status == original_status  # never changed merely to produce UI feedback


def test_check_failed_shown_without_touching_status(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch

    from fastapi.testclient import TestClient
    from app.main import app

    db = _client_db(tmp_path, monkeypatch)
    company = _grant_and_company(db)
    original_status = company.deletion_status

    client = TestClient(app, base_url="http://localhost:8000")
    with patch("app.google_oauth.load_credentials", return_value=MagicMock()), \
         patch("app.google_oauth.fetch_thread_messages", side_effect=ConnectionError("network down")):
        resp = client.post(f"/api/companies/{company.id}/deletion/check-response", follow_redirects=False)

    location = resp.headers["location"]
    assert "check_result=check_failed" in location

    dashboard_resp = client.get(location.split("#")[0])
    assert "check for a response right now" in dashboard_resp.text  # avoids the apostrophe Jinja HTML-escapes

    db.refresh(company)
    assert company.deletion_status == original_status


def _grant_and_company(db) -> Company:
    company = _company(db)
    _grant_readonly_scope(db)
    return company
