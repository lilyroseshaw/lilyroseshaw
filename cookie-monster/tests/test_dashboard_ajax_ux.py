"""Regression tests for the dashboard's generic card-level AJAX layer
(app/static/dashboard.js): card-level actions (Confirm/Reject/Reset,
Research, Check for responses, ...) must update in place - no full-page
reload, no jump back to the top of the dashboard, and a visible in-flight
state on the clicked button - while a plain non-JS form submit (the
graceful fallback) must still work correctly, landing back on the same
company's card via the #company-{id} URL fragment.

Same Playwright-against-a-real-server approach as test_dashboard_modal_ui.py
(see that file's docstring for why a pure server-rendered-HTML assertion
can't catch this class of bug) - self-skips if Playwright/Chromium isn't
installed.
"""
import datetime
import os
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
from app.models import Company

playwright_sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
sync_playwright = playwright_sync_api.sync_playwright

_CHROMIUM_OVERRIDE = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")


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
    # Padding FIRST so the target card sits well below the fold - makes the
    # "scroll position preserved" assertion meaningful rather than trivial.
    # (dashboard orders by evidence_count desc, and ties resolve in
    # insertion order - the target must be created LAST to land at the
    # bottom of an all-equal-evidence list.)
    for i in range(30):
        db.add(Company(
            name=f"Filler {i}", domain=f"filler{i}.com", relationship_type="transactional", status="pending",
            confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
            first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        ))
    pending_co = Company(
        name="Pending Co", domain="pendingco.com", relationship_type="transactional", status="pending",
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
    )
    db.add(pending_co)
    db.commit()
    pending_id = pending_co.id
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

    yield f"http://127.0.0.1:{port}", pending_id

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


def test_confirm_updates_card_without_full_page_navigation(live_server, page):
    base_url, pending_id = live_server
    navigations = []
    page.on("framenavigated", lambda frame: navigations.append(frame.url) if frame == page.main_frame else None)

    page.goto(f"{base_url}/dashboard")
    navigations.clear()  # drop the initial goto's own navigation record

    confirm_selector = f'#company-{pending_id} form[action="/api/companies/{pending_id}/confirm"]'
    page.click(confirm_selector + " button")
    # "Reject" is already present even before confirming (a pending company
    # shows both Confirm and Reject) - the only thing that reliably only
    # happens AFTER the swap is the Confirm form/button disappearing.
    page.wait_for_selector(confirm_selector, state="detached")

    assert navigations == [], "a card-level AJAX action must never trigger a full page navigation"
    card_html = page.locator(f"#company-{pending_id}").inner_html()
    assert 'action="/api/companies/%d/confirm"' % pending_id not in card_html


def test_scroll_position_preserved_after_card_action(live_server, page):
    base_url, pending_id = live_server
    confirm_selector = f'#company-{pending_id} form[action="/api/companies/{pending_id}/confirm"]'
    page.goto(f"{base_url}/dashboard")
    page.locator(f"#company-{pending_id}").scroll_into_view_if_needed()
    scroll_before = page.evaluate("window.scrollY")
    assert scroll_before > 100, "test setup: the target card should be well below the fold"

    page.click(confirm_selector + " button")
    page.wait_for_selector(confirm_selector, state="detached")
    page.wait_for_timeout(100)

    scroll_after = page.evaluate("window.scrollY")
    assert abs(scroll_after - scroll_before) < 50, "scroll position must be preserved, not reset to the top"


def test_button_shows_loading_state_and_disables_while_in_flight(live_server, page):
    base_url, pending_id = live_server

    # Holds the request open (rather than sleeping inside the route
    # handler) so the in-flight state is observable without stalling
    # Playwright's own sync-API driver connection, which a blocking
    # time.sleep() inside a route handler does.
    held = {}
    page.route(f"**/api/companies/{pending_id}/confirm", lambda route: held.setdefault("route", route))

    page.goto(f"{base_url}/dashboard")
    confirm_selector = f'#company-{pending_id} form[action="/api/companies/{pending_id}/confirm"]'
    button = page.locator(confirm_selector + " button")
    button.click()

    page.wait_for_timeout(200)  # request is held open, definitely in flight
    assert button.is_disabled(), "the clicked button must be disabled while its request is in flight"
    assert button.text_content() == "Confirming…"

    held["route"].continue_()
    page.wait_for_selector(confirm_selector, state="detached")


def test_no_js_fallback_still_lands_on_the_same_card(live_server, browser):
    """With JS disabled entirely, the plain HTML form must still work -
    real page navigation, but landing back on this exact company's card
    via the #company-{id} fragment (main.py's _redirect_to_company_card),
    never dumped at the top of the dashboard."""
    base_url, pending_id = live_server
    context = browser.new_context(java_script_enabled=False)
    page = context.new_page()
    page.goto(f"{base_url}/dashboard")

    page.click(f'#company-{pending_id} form[action="/api/companies/{pending_id}/confirm"] button')
    page.wait_for_load_state("networkidle")

    assert page.url.endswith(f"#company-{pending_id}")
    assert 'action="/api/companies/%d/reject"' % pending_id in page.content()
    context.close()
