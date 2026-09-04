"""Real-browser regression for the locked "Check for reply" UX contract:
DEFAULT -> CHECKING (disabled, no double-click) -> NEW REPLY / NO NEW REPLY
/ ERROR, all shown in place on the card, with the error path NEVER falling
back to a native full-page reload the way every other card form does (see
app/static/dashboard.js's dedicated wireCheckResponseForms).

Same Playwright-against-a-real-server approach as test_dashboard_ajax_ux.py
- self-skips if Playwright/Chromium isn't available.
"""
import base64
import datetime
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db as dbmod
from app import config
from app.db import Base
from app.deletion_constants import DeletionStatus
from app.main import app
from app.models import Company, OAuthToken

playwright_sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
sync_playwright = playwright_sync_api.sync_playwright

import os

_CHROMIUM_OVERRIDE = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _msg(msg_id, body_text, from_addr, internal_date):
    return {
        "id": msg_id, "labelIds": ["INBOX"], "internalDate": str(internal_date),
        "payload": {"headers": [{"name": "From", "value": from_addr}], "mimeType": "text/plain",
                    "body": {"data": _b64(body_text)}},
    }


@pytest.fixture()
def live_server(monkeypatch):
    path = tempfile.mktemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(dbmod, "engine", engine)
    monkeypatch.setattr(dbmod, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(config, "DELETION_QUEUE_INTERVAL_SECONDS", 9999)
    monkeypatch.setattr(config, "GOOGLE_REDIRECT_URI", "http://127.0.0.1:8000/auth/callback")

    db = dbmod.SessionLocal()
    company = Company(
        name="Acme Co", domain="acme.com", relationship_type="transactional", status="confirmed",
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        deletion_method="EMAIL_REQUEST", deletion_status=DeletionStatus.SUBMITTED, deletion_thread_id="thread-cr-1",
        waiting_on="COMPANY", next_followup_at=datetime.datetime.utcnow() + datetime.timedelta(hours=20),
    )
    db.add(company)
    db.add(OAuthToken(
        gmail_address="me@gmail.com", encrypted_refresh_token="x",
        scopes_granted=" ".join(config.GMAIL_SCOPES + [config.GMAIL_READONLY_SCOPE]),
    ))
    db.commit()
    company_id = company.id
    db.close()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server_config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(server_config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "test server did not start in time"

    yield f"http://127.0.0.1:{port}", company_id

    server.should_exit = True
    thread.join(timeout=5)
    Path(path).unlink(missing_ok=True)


@pytest.fixture()
def browser():
    with sync_playwright() as p:
        launch_kwargs = {"args": ["--no-sandbox"]}
        if _CHROMIUM_OVERRIDE:
            launch_kwargs["executable_path"] = _CHROMIUM_OVERRIDE
        try:
            b = p.chromium.launch(**launch_kwargs)
        except Exception as exc:  # noqa: BLE001 - environment-dependent, not a code defect
            pytest.skip(f"Chromium not available for Playwright: {exc}")
        yield b
        b.close()


@pytest.fixture()
def page(browser):
    pg = browser.new_page()
    yield pg
    pg.close()


def _patch_creds(monkeypatch):
    from unittest.mock import MagicMock
    monkeypatch.setattr("app.google_oauth.load_credentials", lambda db: MagicMock())


def _selector(company_id):
    return f'#company-{company_id} form.check-response-form'


def test_checking_state_shown_immediately(live_server, page, monkeypatch):
    base_url, company_id = live_server
    _patch_creds(monkeypatch)

    held = {}
    page.route(f"**/api/companies/{company_id}/deletion/check-response", lambda route: held.setdefault("route", route))

    page.goto(f"{base_url}/dashboard")
    button = page.locator(_selector(company_id) + " button")
    button.click()

    page.wait_for_timeout(150)
    assert button.is_disabled()
    assert button.text_content() == "Checking…"
    result = page.locator(f"#company-{company_id} .check-response-result")
    assert result.text_content().strip() == "Checking…"

    held["route"].continue_()


def test_new_reply_visible_in_card_no_navigation(live_server, page, monkeypatch):
    from unittest.mock import patch

    base_url, company_id = live_server
    _patch_creds(monkeypatch)
    navigations = []
    page.on("framenavigated", lambda frame: navigations.append(frame.url) if frame == page.main_frame else None)

    page.goto(f"{base_url}/dashboard")
    navigations.clear()

    reply = _msg("m2", "We are currently reviewing your request.", "privacy@acme.com", 2000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        page.click(_selector(company_id) + " button")
        page.wait_for_selector(f"#company-{company_id} .check-response-result:has-text('New reply from Acme Co')")

    assert navigations == [], "a check-response click must never trigger a full page navigation"
    button = page.locator(_selector(company_id) + " button")
    assert button.text_content() == "Check for reply"
    assert not button.is_disabled()


def test_no_new_reply_visible_in_card(live_server, page, monkeypatch):
    from unittest.mock import patch

    base_url, company_id = live_server
    _patch_creds(monkeypatch)
    page.goto(f"{base_url}/dashboard")

    with patch("app.google_oauth.fetch_thread_messages", return_value=[]):
        page.click(_selector(company_id) + " button")
        page.wait_for_selector(f"#company-{company_id} .check-response-result:has-text('No new reply yet')")

    button = page.locator(_selector(company_id) + " button")
    assert not button.is_disabled()


def test_error_shown_in_place_without_reload(live_server, page, monkeypatch):
    base_url, company_id = live_server
    _patch_creds(monkeypatch)
    navigations = []
    page.on("framenavigated", lambda frame: navigations.append(frame.url) if frame == page.main_frame else None)

    page.goto(f"{base_url}/dashboard")
    navigations.clear()

    # Force the check-response POST itself to fail at the network level -
    # the dedicated handler must show an in-place error, NOT fall back to
    # a native form submit (which would show up here as a real navigation).
    page.route(f"**/api/companies/{company_id}/deletion/check-response", lambda route: route.abort())

    button = page.locator(_selector(company_id) + " button")
    button.click()
    page.wait_for_selector(f"#company-{company_id} .check-response-result:has-text(\"Couldn't check right now\")")

    assert navigations == [], "the error path must never fall back to a native page reload/navigation"
    assert not button.is_disabled()
    assert button.text_content() == "Check for reply"

    # And the underlying status must be untouched - still eligible to retry.
    db = dbmod.SessionLocal()
    company = db.get(Company, company_id)
    assert company.deletion_status == DeletionStatus.SUBMITTED
    db.close()


def test_double_click_while_checking_does_not_send_twice(live_server, page, monkeypatch):
    base_url, company_id = live_server
    _patch_creds(monkeypatch)

    call_count = {"n": 0}

    def _count_and_hold(route):
        call_count["n"] += 1
        held_routes.append(route)

    held_routes = []
    page.route(f"**/api/companies/{company_id}/deletion/check-response", _count_and_hold)

    page.goto(f"{base_url}/dashboard")
    button = page.locator(_selector(company_id) + " button")
    button.click()
    page.wait_for_timeout(100)
    # A second click while disabled must be a no-op at the browser level.
    button.click(force=True)
    page.wait_for_timeout(100)

    assert call_count["n"] == 1
    for route in held_routes:
        route.continue_()


def test_chase_state_block_and_pause_button(live_server, page, monkeypatch):
    base_url, company_id = live_server
    _patch_creds(monkeypatch)
    navigations = []
    page.on("framenavigated", lambda frame: navigations.append(frame.url) if frame == page.main_frame else None)

    page.goto(f"{base_url}/dashboard")
    navigations.clear()

    chase_block = page.locator(f"#company-{company_id} .chase-state")
    assert "WAITING ON ACME CO" in chase_block.inner_text().upper()
    assert "Next follow-up" in chase_block.inner_text()

    pause_button = page.locator(f"#company-{company_id} .chase-state form button")
    assert pause_button.text_content().strip() == "Pause follow-ups"
    pause_button.click()
    page.wait_for_selector(f"#company-{company_id} .chase-state:has-text('Follow-ups paused')")

    assert navigations == [], "pausing follow-ups must update the card in place, no full page navigation"
    resume_button = page.locator(f"#company-{company_id} .chase-state form button")
    assert resume_button.text_content().strip() == "Resume follow-ups"

    db = dbmod.SessionLocal()
    company = db.get(Company, company_id)
    assert company.followups_paused is True
    db.close()
