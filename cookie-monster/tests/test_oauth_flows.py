"""Regression tests for the /auth/* routes' session-state handling.

Bug this file exists for: all three incremental-consent flows (login,
enable-sending, enable-response-tracking) used to write their pending
(state, scopes) into ONE shared session slot. Starting a second flow while
a first one was still waiting on Google's consent screen silently
overwrote the first flow's slot, so completing the FIRST flow afterward
failed at /auth/callback with "Invalid OAuth state or missing code" - even
though the user did nothing wrong. This is exactly what was reported after
using "Enable Response Tracking": a prior in-flight flow's state got
clobbered before its callback arrived.

The fix stores each flow's pending state under its own key
(request.session["oauth_pending"][flow_name]), so starting one flow never
erases another's.
"""
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db as dbmod
from app.db import Base
from app.main import app


@pytest.fixture()
def file_db(monkeypatch):
    """main.py's routes open their own sessions via app.db.get_session(),
    so they need a real file-backed engine, not an in-process :memory: one
    tied to a single connection."""
    path = tempfile.mktemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(dbmod, "engine", engine)
    monkeypatch.setattr(dbmod, "SessionLocal", sessionmaker(bind=engine))
    yield
    Path(path).unlink(missing_ok=True)


def _patched_google_oauth():
    return patch.multiple(
        "app.google_oauth",
        get_authorization_url=lambda scopes=None: ("https://accounts.google.com/o/oauth2/fake-login", "state-login"),
        get_granted_scopes=lambda db: set(),
        get_send_authorization_url=lambda db: ("https://accounts.google.com/o/oauth2/fake-send", "state-send"),
        get_response_tracking_authorization_url=lambda db: ("https://accounts.google.com/o/oauth2/fake-readonly", "state-readonly"),
        exchange_code_for_credentials=lambda code, scopes=None: object(),
        get_gmail_address=lambda creds: "me@gmail.com",
        save_credentials=lambda db, creds, gmail_address: None,
    )


def test_starting_a_second_flow_does_not_break_the_first_flows_callback(file_db):
    """Reproduces the exact reported bug: start the login flow, then -
    before its callback arrives - also start the enable-sending flow (e.g.
    a second tab, or clicking again). The FIRST flow's callback must still
    succeed."""
    with _patched_google_oauth():
        with TestClient(app) as client:
            r1 = client.get("/auth/login", follow_redirects=False)
            assert r1.status_code in (302, 307)

            # A second, different flow starts before flow 1 finishes.
            r2 = client.get("/auth/enable-sending", follow_redirects=False)
            assert r2.status_code in (302, 307)

            # Flow 1 completes at Google and comes back - must still work,
            # even though flow 2 was started in between.
            callback = client.get(
                "/auth/callback", params={"code": "fake-code", "state": "state-login"}, follow_redirects=False,
            )
            assert callback.status_code in (302, 307)
            assert callback.headers["location"] == "/"


def test_second_flow_can_also_complete_after_the_first(file_db):
    """The reverse order: after flow 1 (login) completes, flow 2 (send),
    started earlier, must still be able to complete too - its pending
    state must not have been consumed or dropped by flow 1's callback."""
    with _patched_google_oauth():
        with TestClient(app) as client:
            client.get("/auth/login", follow_redirects=False)
            client.get("/auth/enable-sending", follow_redirects=False)

            first = client.get(
                "/auth/callback", params={"code": "fake-code-1", "state": "state-login"}, follow_redirects=False,
            )
            assert first.status_code in (302, 307)

            second = client.get(
                "/auth/callback", params={"code": "fake-code-2", "state": "state-send"}, follow_redirects=False,
            )
            assert second.status_code in (302, 307)
            assert second.headers["location"] == "/"


def test_readonly_flow_independent_of_login_and_send(file_db):
    """All three flow types (login/send/readonly) pending at once - each
    must resolve independently."""
    with _patched_google_oauth():
        with TestClient(app) as client:
            client.get("/auth/login", follow_redirects=False)
            client.get("/auth/enable-sending", follow_redirects=False)
            client.get("/auth/enable-response-tracking", follow_redirects=False)

            resp = client.get(
                "/auth/callback", params={"code": "fake-code", "state": "state-readonly"}, follow_redirects=False,
            )
            assert resp.status_code in (302, 307)
            assert resp.headers["location"] == "/"


def test_callback_with_unknown_state_is_rejected(file_db):
    """A state that doesn't match ANY pending flow (tampering, a stale
    link, or a flow that was never started) must still be rejected - the
    fix must not weaken this validation."""
    with _patched_google_oauth():
        with TestClient(app) as client:
            client.get("/auth/login", follow_redirects=False)
            resp = client.get(
                "/auth/callback", params={"code": "fake-code", "state": "not-a-real-state"}, follow_redirects=False,
            )
            assert resp.status_code == 400


def test_callback_with_no_pending_flow_is_rejected(file_db):
    """Hitting /auth/callback with no flow ever started (e.g. a stale
    bookmark) must still be rejected, not accepted."""
    with _patched_google_oauth():
        with TestClient(app) as client:
            resp = client.get(
                "/auth/callback", params={"code": "fake-code", "state": "state-login"}, follow_redirects=False,
            )
            assert resp.status_code == 400


def test_callback_missing_code_is_rejected(file_db):
    with _patched_google_oauth():
        with TestClient(app) as client:
            client.get("/auth/login", follow_redirects=False)
            resp = client.get("/auth/callback", params={"state": "state-login"}, follow_redirects=False)
            assert resp.status_code == 400
