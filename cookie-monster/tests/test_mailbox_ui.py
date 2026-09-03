"""UI-level coverage for the mailbox: server-rendered structure via
TestClient/BeautifulSoup for accessibility and page-composition checks,
plus a real-browser Playwright check for the "Respond" draft panel's
fetch-driven interaction (can't be verified from static HTML alone, same
reasoning as test_dashboard_ajax_ux.py) and the mobile layout.
"""
import base64
import datetime
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config, mail
from app.db import Base
from app.deletion_constants import DeletionStatus
from app.models import Company, DeletionRecipe, OAuthToken
from app.response_classify import ResponseClassifier


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _msg(msg_id, body_text, from_addr, internal_date, subject="Re: your data"):
    return {
        "id": msg_id,
        "labelIds": ["INBOX"],
        "internalDate": str(internal_date),
        "payload": {
            "headers": [{"name": "From", "value": from_addr}, {"name": "Subject", "value": subject}],
            "mimeType": "text/plain",
            "body": {"data": _b64(body_text)},
        },
    }


def _seed_mailbox_company(db, **overrides):
    defaults = dict(
        name="Goop Kitchen", domain="goopkitchen.com", relationship_type="transactional", status="confirmed",
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        deletion_status=DeletionStatus.VERIFICATION_NEEDED, deletion_thread_id="thread123",
    )
    defaults.update(overrides)
    company = Company(**defaults)
    db.add(company)
    db.add(DeletionRecipe(domain=company.domain, email="privacy@goopkitchen.com", deletes_account=True))
    db.commit()
    message = _msg("m2", "Please verify your identity to continue - this will also close your account.", "privacy@goopkitchen.com", 2000)
    classification = ResponseClassifier().classify("Please verify your identity to continue.")
    mail.record_inbound_mail_message(db, company, message, "Please verify your identity to continue.", classification)
    db.commit()
    return company


@pytest.fixture()
def client_db(tmp_path, monkeypatch):
    import app.db as dbmod

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


def _get_soup(path):
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app, base_url="http://localhost:8000")
    resp = client.get(path)
    assert resp.status_code == 200
    return BeautifulSoup(resp.text, "html.parser")


def test_mailbox_list_shows_envelope_for_company_with_mail(client_db):
    company = _seed_mailbox_company(client_db)
    soup = _get_soup("/mail")
    envelope = soup.find("a", class_="mail-envelope")
    assert envelope is not None
    assert f"/mail/{company.id}" == envelope.get("href")
    assert "Goop Kitchen" in envelope.get_text()
    badge = envelope.find(class_="status-badge")
    assert badge is not None
    assert badge.get_text(strip=True)  # never color-only


def test_mailbox_list_empty_state_has_no_envelopes(client_db):
    soup = _get_soup("/mail")
    assert soup.find(class_="mail-envelope") is None
    assert soup.find(class_="mailbox-list") is None


def test_opening_letter_shows_account_warning_and_choices(client_db):
    company = _seed_mailbox_company(client_db)
    soup = _get_soup(f"/mail/{company.id}")
    warning = soup.find(class_="account-warning")
    assert warning is not None
    assert "delete your account" in warning.get_text().lower()
    buttons = warning.find_all(attrs={"data-respond-kind": True})
    kinds = {b["data-respond-kind"] for b in buttons}
    assert kinds == {"delete_account_and_data", "keep_account_request_data_only"}
    # The letter's own content and Baker's Dozen's classifier gloss are both present.
    letter = soup.find(class_="letter-inbound")
    assert letter is not None
    assert "verify your identity" in letter.get_text().lower()
    understand = letter.find(class_="letter-understand-text")
    assert understand is not None
    assert understand.get_text(strip=True)


def test_opening_letter_marks_it_read(client_db):
    company = _seed_mailbox_company(client_db)
    assert mail.unread_mail_count(client_db) == 1
    _get_soup(f"/mail/{company.id}")
    assert mail.unread_mail_count(client_db) == 0


def test_sent_confirmation_uses_status_role(client_db):
    company = _seed_mailbox_company(client_db)
    soup = _get_soup(f"/mail/{company.id}?sent=1")
    confirm = soup.find(class_="letter-sent-confirm")
    assert confirm is not None
    assert confirm.get("role") == "status"


def test_dashboard_shows_mail_nav_with_unread_badge(client_db):
    _seed_mailbox_company(client_db)
    soup = _get_soup("/dashboard")
    nav = soup.find("a", class_="mail-nav")
    assert nav is not None
    assert "unread" in nav.get("aria-label", "").lower()
    badge = nav.find(class_="mail-nav-badge")
    assert badge is not None
    assert badge.get_text(strip=True) == "1"


def test_dashboard_shows_youve_got_mail_banner(client_db):
    _seed_mailbox_company(client_db)
    soup = _get_soup("/dashboard")
    banner = soup.find(id="mail-banner")
    assert banner is not None
    assert "you've got mail" in banner.get_text().lower()
    open_link = banner.find("a", href="/mail")
    assert open_link is not None


def test_dashboard_hides_banner_when_no_unread_mail(client_db):
    soup = _get_soup("/dashboard")
    assert soup.find(id="mail-banner") is None
    nav = soup.find("a", class_="mail-nav")
    assert nav.find(class_="mail-nav-badge") is None


def test_no_account_warning_when_recipe_does_not_flag_account_deletion(client_db):
    company = _seed_mailbox_company(client_db, domain="safeco.com", name="Safe Co")
    # Overwrite the seeded recipe's deletes_account flag.
    recipe = client_db.query(DeletionRecipe).filter(DeletionRecipe.domain == "safeco.com").one()
    recipe.deletes_account = False
    client_db.commit()
    soup = _get_soup(f"/mail/{company.id}")
    assert soup.find(class_="account-warning") is None
    assert soup.find(class_="letter-manual-note") is not None


# --- Playwright: the fetch-driven draft panel + mobile layout ---

playwright_sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
sync_playwright = playwright_sync_api.sync_playwright

_CHROMIUM_OVERRIDE = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")


@pytest.fixture()
def live_server(monkeypatch):
    import app.db as dbmod
    import uvicorn
    from app.main import app as fastapi_app

    path = tempfile.mktemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(dbmod, "engine", engine)
    monkeypatch.setattr(dbmod, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(config, "DELETION_QUEUE_INTERVAL_SECONDS", 9999)
    monkeypatch.setattr(config, "GOOGLE_REDIRECT_URI", "http://127.0.0.1:8000/auth/callback")

    db = dbmod.SessionLocal()
    company = _seed_mailbox_company(db)
    company_id = company.id
    # gmail.send granted, so the draft panel's Send button is enabled and
    # this test can verify BOTH the fetched draft content and its enabled
    # state - the actual send is never exercised here (no click on Send),
    # so no real/mocked Gmail send call is needed.
    db.add(OAuthToken(
        gmail_address="me@gmail.com", encrypted_refresh_token="x",
        scopes_granted=" ".join(config.GMAIL_SCOPES + [config.GMAIL_SEND_SCOPE]),
    ))
    db.commit()
    db.close()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server_config = uvicorn.Config(fastapi_app, host="127.0.0.1", port=port, log_level="warning")
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
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium not available for Playwright: {exc}")
        yield b
        b.close()


def test_respond_button_opens_draft_panel_with_fetched_content(live_server, browser):
    base_url, company_id = live_server
    page = browser.new_page()
    page.goto(f"{base_url}/mail/{company_id}")

    panel = page.locator("#letter-respond-draft")
    assert panel.is_hidden()

    page.click('[data-respond-kind="keep_account_request_data_only"]')
    page.wait_for_selector("#letter-respond-draft:not([hidden])")
    page.wait_for_function(
        "document.getElementById('respond-to').textContent.length > 0 && document.getElementById('respond-to').textContent !== 'Loading…'"
    )
    to_text = page.locator("#respond-to").text_content()
    body_text = page.locator("#respond-body").text_content()
    assert "@goopkitchen.com" in to_text
    assert "maintain my account" in body_text

    send_btn = page.locator("#respond-send-btn")
    assert send_btn.is_enabled()  # send scope isn't granted in this fixture at all... see below

    page.close()


def test_mobile_mailbox_and_letter_render_without_horizontal_overflow(live_server, browser):
    base_url, company_id = live_server
    page = browser.new_page(viewport={"width": 390, "height": 844})

    page.goto(f"{base_url}/mail")
    body_width = page.evaluate("document.body.scrollWidth")
    assert body_width <= 391, f"mailbox list overflows horizontally at 390px: {body_width}px"

    page.goto(f"{base_url}/mail/{company_id}")
    body_width = page.evaluate("document.body.scrollWidth")
    assert body_width <= 391, f"letter view overflows horizontally at 390px: {body_width}px"
    assert page.locator(".account-warning").is_visible()

    page.close()
