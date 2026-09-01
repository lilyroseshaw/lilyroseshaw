"""Regression tests for the "Delete my data" confirmation modal's actual
on-screen behavior.

Bug this file exists for: the modal (#deletion-modal) is server-rendered
with the `hidden` attribute and is only meant to become visible after a
user clicks a company's "Delete my data" button (dashboard.js). But
`.deletion-modal` in style.css sets `display: flex`, and nothing overrode
that for the `[hidden]` state - the browser's own `[hidden] { display:
none }` rule and the author rule `.deletion-modal { display: flex }` have
EQUAL CSS specificity, and author rules win ties against the UA
stylesheet. So the modal was visible on every dashboard load regardless of
the `hidden` attribute, and clicking Cancel - which correctly re-sets
`hidden` in the DOM - had no visible effect, because the CSS was never
looking at that attribute in the first place.

A pure server-rendered-HTML test (e.g. asserting the string "hidden"
appears in the response) would NOT have caught this - the attribute was
always present and correct; only the browser's actual visual rendering
was wrong. This uses a real headless browser (Playwright) against a real
running instance of the app to check what a user actually sees, not just
what the server sent.

Requires the Playwright Python package AND its Chromium browser to be
installed (`pip install playwright && playwright install chromium`) - if
either is missing, these tests are skipped rather than failing the suite,
since this is one extra, optional layer of UI coverage on top of the
(mandatory) pytest suite covering everything else.
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
from app.deletion_constants import DeletionMethod, DeletionStatus
from app.main import app
from app.models import Company

playwright_sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
sync_playwright = playwright_sync_api.sync_playwright

# Only set for this specific sandbox, where the pip-installed playwright's
# expected browser build doesn't match what's pre-cached on disk. On a
# normal machine (including a real dev's Mac after `playwright install
# chromium`), this is unset and Playwright auto-detects its own browser.
_CHROMIUM_OVERRIDE = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")


@pytest.fixture()
def live_server(monkeypatch):
    """Playwright drives a real browser making real HTTP requests, so it
    needs an actual running server - not an in-process ASGI TestClient."""
    path = tempfile.mktemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(dbmod, "engine", engine)
    monkeypatch.setattr(dbmod, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(config, "DELETION_QUEUE_INTERVAL_SECONDS", 9999)
    monkeypatch.setattr(config, "GOOGLE_REDIRECT_URI", "http://127.0.0.1:8000/auth/callback")

    db = dbmod.SessionLocal()
    company = Company(
        name="Widget Co", domain="widgetco.com", relationship_type="transactional", status="confirmed",
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        deletion_status=DeletionStatus.READY, deletion_method=DeletionMethod.WEB_FORM,
        deletion_url="https://widgetco.com/privacy/delete", deletion_verified=True,
    )
    db.add(company)
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
def page():
    with sync_playwright() as p:
        launch_kwargs = {"args": ["--no-sandbox"]}
        if _CHROMIUM_OVERRIDE:
            launch_kwargs["executable_path"] = _CHROMIUM_OVERRIDE
        try:
            browser = p.chromium.launch(**launch_kwargs)
        except Exception as exc:  # noqa: BLE001 - environment-dependent, not a code defect
            pytest.skip(f"Chromium not available for Playwright: {exc}")
        pg = browser.new_page()
        yield pg
        browser.close()


def test_dashboard_loads_with_modal_closed(live_server, page):
    base_url, _ = live_server
    page.goto(f"{base_url}/dashboard")
    modal = page.locator("#deletion-modal")
    assert modal.is_hidden(), "the deletion modal must not be visible just from opening the dashboard"


def test_clicking_delete_my_data_opens_the_modal(live_server, page):
    base_url, _ = live_server
    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    modal = page.locator("#deletion-modal")
    assert modal.is_visible(), "the modal should open after an explicit 'Delete my data' click"


def test_clicking_cancel_closes_the_modal(live_server, page):
    base_url, _ = live_server
    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    assert page.locator("#deletion-modal").is_visible()

    page.click("#deletion-modal-cancel")
    assert page.locator("#deletion-modal").is_hidden(), "Cancel must actually hide the modal, not just no-op"


def test_cancel_never_submits_a_deletion_request(live_server, page):
    base_url, company_id = live_server
    execute_requests = []
    page.on(
        "request",
        lambda req: execute_requests.append(req.url)
        if req.method == "POST" and "/deletion/execute" in req.url
        else None,
    )

    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    page.click("#deletion-modal-cancel")
    page.wait_for_timeout(200)  # give any errant request a moment to fire

    assert execute_requests == [], "Cancel must never POST to the deletion/execute endpoint"

    # And the company's actual status must be untouched - a request-level
    # check and a state-level check, so neither can silently mask a bug.
    page.goto(f"{base_url}/dashboard")
    assert "Deletion method ready" in page.content()
    assert page.locator("#deletion-modal").is_hidden()
