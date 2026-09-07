"""Just the Essentials - DASHBOARD PROGRESS SUMMARY milestone.

The main dashboard company card previously showed a static "review cleanup
actions" caption for any JUST_THE_ESSENTIALS company, regardless of actual
PrivacyAction progress. This adds a compact, truthful one-line summary -
app.privacy_action.just_the_essentials_dashboard_summary - wired into the
existing READY/FAILED card blocks via
app.main._execution_plans_for_companies's new `jte_progress_summary` field.

READ-ONLY presentation only: no new persistence, no new privacy
automation, no change to CaseOutcome, Company.deletion_status, or the JTE
resolver/attestation endpoint. Fabricated companies only; no live network
access anywhere in this file.
"""
import datetime

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config
from app.db import Base
from app.deletion_constants import DeletionMethod, DeletionStatus, PrivacyActionStatus, PrivacyActionType, RecipeChoice
from app.models import Company, PrivacyAction, PrivacyCase
from app.privacy_action import just_the_essentials_dashboard_summary


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


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


@pytest.fixture()
def client():
    from app.main import app
    return TestClient(app, base_url="http://localhost:8000")


def _company(db, name="Fabricated Co", domain="fabricated-corp.com", **overrides) -> Company:
    defaults = dict(
        name=name, domain=domain, relationship_type="transactional", status="confirmed",
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        deletion_method=DeletionMethod.EMAIL_REQUEST, deletion_status=DeletionStatus.READY,
        deletion_email="privacy@" + domain, deletion_verified=True,
    )
    defaults.update(overrides)
    company = Company(**defaults)
    db.add(company)
    db.commit()
    return company


def _case(db, company, selected_recipe=RecipeChoice.JUST_THE_ESSENTIALS) -> PrivacyCase:
    case = PrivacyCase(company_id=company.id, selected_recipe=selected_recipe, recipe_selected_at=None)
    db.add(case)
    db.commit()
    return case


def _action(db, case, action_type, status, evidence=None) -> PrivacyAction:
    action = PrivacyAction(
        privacy_case_id=case.id, action_type=action_type, method=DeletionMethod.ACCOUNT_SETTING,
        status=status, evidence=evidence or {},
    )
    db.add(action)
    db.commit()
    return action


def _jte_actions(db, case, tracking_status, opt_out_status):
    return [
        _action(db, case, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP, tracking_status),
        _action(db, case, PrivacyActionType.SALE_SHARING_OPT_OUT, opt_out_status),
    ]


# --- pure summary function -------------------------------------------------


def test_both_needs_research(db):
    company = _company(db)
    case = _case(db, company)
    actions = _jte_actions(db, case, PrivacyActionStatus.NEEDS_RESEARCH, PrivacyActionStatus.NEEDS_RESEARCH)
    assert just_the_essentials_dashboard_summary(actions) == "2 need research"


def test_one_ready_one_needs_review(db):
    company = _company(db)
    case = _case(db, company)
    actions = _jte_actions(db, case, PrivacyActionStatus.USER_ACTION_REQUIRED, PrivacyActionStatus.NEEDS_REVIEW)
    assert just_the_essentials_dashboard_summary(actions) == "1 ready for you · 1 needs review"


def test_one_user_completed_one_needs_review(db):
    company = _company(db)
    case = _case(db, company)
    actions = _jte_actions(db, case, PrivacyActionStatus.USER_COMPLETED, PrivacyActionStatus.NEEDS_REVIEW)
    summary = just_the_essentials_dashboard_summary(actions)
    assert summary == "1 completed by you · 1 needs review"
    assert "by you" in summary


def test_both_user_completed(db):
    company = _company(db)
    case = _case(db, company)
    actions = _jte_actions(db, case, PrivacyActionStatus.USER_COMPLETED, PrivacyActionStatus.USER_COMPLETED)
    assert just_the_essentials_dashboard_summary(actions) == "2 of 2 privacy controls completed by you"


def test_both_confirmed_distinguishable_from_both_user_completed(db):
    company = _company(db)
    case = _case(db, company)
    confirmed_actions = _jte_actions(db, case, PrivacyActionStatus.CONFIRMED, PrivacyActionStatus.CONFIRMED)
    confirmed_summary = just_the_essentials_dashboard_summary(confirmed_actions)

    company2 = _company(db, name="Other Co", domain="other-corp.com")
    case2 = _case(db, company2)
    user_actions = _jte_actions(db, case2, PrivacyActionStatus.USER_COMPLETED, PrivacyActionStatus.USER_COMPLETED)
    user_summary = just_the_essentials_dashboard_summary(user_actions)

    assert confirmed_summary != user_summary
    assert confirmed_summary == "2 of 2 privacy controls confirmed"
    assert "by you" not in confirmed_summary
    assert "by you" in user_summary


def test_mixed_confirmed_and_user_completed_names_both(db):
    company = _company(db)
    case = _case(db, company)
    actions = _jte_actions(db, case, PrivacyActionStatus.CONFIRMED, PrivacyActionStatus.USER_COMPLETED)
    summary = just_the_essentials_dashboard_summary(actions)
    assert summary == "2 of 2 privacy controls completed · 1 by you"


def test_rejected_and_user_completed_mixed_state_is_truthful(db):
    company = _company(db)
    case = _case(db, company)
    actions = _jte_actions(db, case, PrivacyActionStatus.REJECTED, PrivacyActionStatus.USER_COMPLETED)
    summary = just_the_essentials_dashboard_summary(actions)
    assert summary == "1 completed by you · 1 declined"
    # Never collapsed into a false "complete"/"resolved" state.
    assert "2 of 2" not in summary


@pytest.mark.parametrize("status", list(PrivacyActionStatus.ALL))
def test_no_forbidden_wording_appears_for_any_status(db, status):
    company = _company(db)
    case = _case(db, company)
    actions = _jte_actions(db, case, status, PrivacyActionStatus.NEEDS_REVIEW)
    summary = just_the_essentials_dashboard_summary(actions).lower()
    assert "deleted" not in summary
    # "couldn't be verified" (FAILED) is a truthful negative - the one
    # forbidden claim is a bare/positive "verified".
    assert "verified" not in summary or "couldn't be verified" in summary
    assert "company confirmed" not in summary
    assert "full clean" not in summary
    assert "recalled" not in summary


def test_user_completed_never_says_verified_or_confirmed_alone(db):
    company = _company(db)
    case = _case(db, company)
    actions = _jte_actions(db, case, PrivacyActionStatus.USER_COMPLETED, PrivacyActionStatus.USER_COMPLETED)
    summary = just_the_essentials_dashboard_summary(actions)
    assert "confirmed" not in summary
    assert "verified" not in summary


# --- dashboard card rendering (server-rendered HTML) ------------------------


def test_full_clean_card_unaffected(client_db, client):
    company = _company(client_db, deletion_status=DeletionStatus.READY)
    _case(client_db, company, selected_recipe=RecipeChoice.FULL_CLEAN)
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    card = soup.find("article", {"data-id": str(company.id)})
    assert card is not None
    assert "Delete my data" in card.get_text()
    assert "privacy controls" not in card.get_text()
    assert "Just the Essentials" not in card.get_text()


def test_pantry_unaffected(client_db, client):
    company = _company(client_db, deletion_status=DeletionStatus.READY)
    _case(client_db, company, selected_recipe=RecipeChoice.LEAVE_IT_BE)
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "privacy controls" not in resp.text


def test_legacy_no_recipe_unaffected(client_db, client):
    company = _company(client_db, deletion_status=DeletionStatus.READY)
    _case(client_db, company, selected_recipe=None)
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    card = soup.find("article", {"data-id": str(company.id)})
    assert "Delete my data" in card.get_text()
    assert "privacy controls" not in card.get_text()


def test_jte_ready_card_shows_progress_summary(client_db, client):
    company = _company(client_db, deletion_status=DeletionStatus.READY)
    case = _case(client_db, company)
    _jte_actions(client_db, case, PrivacyActionStatus.USER_ACTION_REQUIRED, PrivacyActionStatus.NEEDS_REVIEW)
    resp = client.get("/dashboard")
    soup = BeautifulSoup(resp.text, "html.parser")
    card = soup.find("article", {"data-id": str(company.id)})
    text = card.get_text()
    assert "1 ready for you" in text
    assert "1 needs review" in text
    assert "review cleanup actions" not in text
    assert "Review cleanup" in text  # single primary CTA unchanged
    assert "Delete my data" not in text


def test_jte_failed_card_shows_progress_summary(client_db, client):
    company = _company(client_db, deletion_status=DeletionStatus.FAILED, deletion_error="SMTP timeout")
    case = _case(client_db, company)
    _jte_actions(client_db, case, PrivacyActionStatus.USER_COMPLETED, PrivacyActionStatus.USER_COMPLETED)
    resp = client.get("/dashboard")
    soup = BeautifulSoup(resp.text, "html.parser")
    card = soup.find("article", {"data-id": str(company.id)})
    text = card.get_text()
    assert "2 of 2 privacy controls completed by you" in text
    assert "Review cleanup" in text


def test_jte_card_never_shows_multiple_action_buttons(client_db, client):
    company = _company(client_db, deletion_status=DeletionStatus.READY)
    case = _case(client_db, company)
    _jte_actions(client_db, case, PrivacyActionStatus.USER_COMPLETED, PrivacyActionStatus.NEEDS_REVIEW)
    resp = client.get("/dashboard")
    soup = BeautifulSoup(resp.text, "html.parser")
    card = soup.find("article", {"data-id": str(company.id)})
    buttons = card.find_all("button")
    primary_ctas = [b for b in buttons if b.get_text(strip=True) == "Review cleanup"]
    assert len(primary_ctas) == 1


def test_no_privacy_action_row_mutation_from_dashboard_render(client_db, client):
    company = _company(client_db, deletion_status=DeletionStatus.READY)
    case = _case(client_db, company)
    actions = _jte_actions(db=client_db, case=case, tracking_status=PrivacyActionStatus.NEEDS_RESEARCH, opt_out_status=PrivacyActionStatus.NEEDS_RESEARCH)
    before = [(a.id, a.status, a.evidence) for a in actions]
    client.get("/dashboard")
    client_db.expire_all()
    after = [(a.id, a.status, a.evidence) for a in client_db.query(PrivacyAction).filter(PrivacyAction.privacy_case_id == case.id).all()]
    assert sorted(before) == sorted(after)


# --- live-browser rendering (real Chromium) --------------------------------
#
# Confirms what a user actually SEES on the dashboard card, not just what
# the server sent - same rationale as test_dashboard_modal_ui.py. Skipped
# (not failed) when Playwright/Chromium isn't installed.

import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import uvicorn

playwright_sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
sync_playwright = playwright_sync_api.sync_playwright

_CHROMIUM_OVERRIDE = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")


@pytest.fixture()
def live_server(monkeypatch):
    import app.db as dbmod
    from app.main import app as fastapi_app

    path = tempfile.mktemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(dbmod, "engine", engine)
    monkeypatch.setattr(dbmod, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(config, "DELETION_QUEUE_INTERVAL_SECONDS", 9999)

    db = dbmod.SessionLocal()
    mixed_co = _company(db, name="Mixed Co", domain="mixed-co.com")
    mixed_case = _case(db, mixed_co)
    _jte_actions(db, mixed_case, PrivacyActionStatus.USER_ACTION_REQUIRED, PrivacyActionStatus.NEEDS_REVIEW)

    done_co = _company(db, name="Done Co", domain="done-co.com")
    done_case = _case(db, done_co)
    _jte_actions(db, done_case, PrivacyActionStatus.USER_COMPLETED, PrivacyActionStatus.USER_COMPLETED)
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

    yield f"http://127.0.0.1:{port}"

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


def test_mixed_state_shown_truthfully_in_browser(live_server, page):
    base_url = live_server
    page.goto(f"{base_url}/dashboard")
    card = page.locator("article.company-card", has_text="Mixed Co")
    text = card.inner_text()
    assert "1 ready for you" in text
    assert "1 needs review" in text
    assert "review cleanup actions" not in text
    assert card.get_by_role("button", name="Review cleanup").is_visible()


def test_both_user_completed_shown_truthfully_in_browser(live_server, page):
    base_url = live_server
    page.goto(f"{base_url}/dashboard")
    card = page.locator("article.company-card", has_text="Done Co")
    text = card.inner_text()
    assert "2 of 2 privacy controls completed by you" in text
    assert "Deleted" not in text
    assert "Verified" not in text
    assert "Company confirmed" not in text
    assert card.get_by_role("button", name="Review cleanup").is_visible()
