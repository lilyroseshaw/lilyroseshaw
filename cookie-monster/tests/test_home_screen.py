"""THE BAKER'S DOZEN - MOBILE-FIRST HOME SCREEN FOUNDATION.

Coverage for the home screen's information architecture: companies are
grouped into Needs You / Working / Done sections purely by their already-
authoritative top_level_state (see app/top_level_state.py), Pantry stays
visually and numerically separate, and every existing action route/button
stays reachable regardless of which section now wraps its card.

Server-rendered structure via TestClient/BeautifulSoup for composition
checks, plus one real-browser Playwright check for the mobile viewport
(can't be verified from static HTML alone - actual layout/overflow is a
rendering concern, same reasoning as test_mailbox_ui.py's equivalent
check). Fabricated companies only.
"""
import datetime
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config
from app.db import Base
from app.deletion_constants import (
    DeletionMethod,
    DeletionStatus,
    PrivacyActionStatus,
    PrivacyActionType,
    RecipeChoice,
    WaitingOn,
)
from app.models import Company, PrivacyAction, PrivacyCase
from app.top_level_state import TopLevelState


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
        deletion_verified=True,
    )
    defaults.update(overrides)
    company = Company(**defaults)
    db.add(company)
    db.commit()
    return company


def _case(db, company, selected_recipe) -> PrivacyCase:
    case = PrivacyCase(company_id=company.id, selected_recipe=selected_recipe, recipe_selected_at=datetime.datetime(2026, 1, 1))
    db.add(case)
    db.commit()
    return case


def _action(db, case, action_type, status) -> PrivacyAction:
    action = PrivacyAction(
        privacy_case_id=case.id, action_type=action_type, method=DeletionMethod.ACCOUNT_SETTING,
        status=status, evidence={},
    )
    db.add(action)
    db.commit()
    return action


def _get_soup(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    return BeautifulSoup(resp.text, "html.parser")


def _section(soup, heading_id):
    heading = soup.find(id=heading_id)
    if heading is None:
        return None
    return heading.find_parent("section")


# --- NEEDS YOU ---------------------------------------------------------


def test_needs_you_section_renders_only_when_non_empty(client_db, client):
    soup = _get_soup(client)
    assert _section(soup, "section-needs-you") is None

    _company(client_db, deletion_status=DeletionStatus.VERIFICATION_NEEDED, waiting_on=WaitingOn.USER)
    soup = _get_soup(client)
    section = _section(soup, "section-needs-you")
    assert section is not None


def test_needs_you_card_shows_company_reason_and_one_primary_action(client_db, client):
    company = _company(
        client_db, name="Amazon", deletion_status=DeletionStatus.VERIFICATION_NEEDED, waiting_on=WaitingOn.USER,
    )
    soup = _get_soup(client)
    section = _section(soup, "section-needs-you")
    card = section.find("article", {"data-id": str(company.id)})
    assert card is not None
    text = card.get_text()
    assert "Amazon" in text
    assert "verify your identity" in text  # one clear sentence explaining what's needed
    assert card.get("data-top-level-state") == TopLevelState.NEEDS_YOU


# --- WORKING -------------------------------------------------------------


def test_working_section_shows_calm_no_action_copy(client_db, client):
    company = _company(
        client_db, name="MALK Organics", deletion_status=DeletionStatus.IN_PROGRESS,
        deletion_thread_id="thread1", waiting_on=WaitingOn.COMPANY,
    )
    soup = _get_soup(client)
    section = _section(soup, "section-working")
    assert section is not None
    card = section.find("article", {"data-id": str(company.id)})
    assert card is not None
    text = card.get_text()
    assert "no action needed" in text.lower()
    assert card.get("data-top-level-state") == TopLevelState.WORKING


# --- DONE ------------------------------------------------------------


def test_done_section_company_confirmed_says_confirmed_by_company(client_db, client):
    company = _company(
        client_db, name="Goop", deletion_status=DeletionStatus.COMPLETED,
        deletion_completed_at=datetime.datetime(2024, 1, 1),
        deletion_evidence={"type": "gmail_reply", "quote": "we deleted your data"},
    )
    soup = _get_soup(client)
    section = _section(soup, "section-done")
    card = section.find("article", {"data-id": str(company.id)})
    text = card.get_text()
    assert "Deletion confirmed" in text
    assert "confirmed by Goop" in text
    assert card.get("data-top-level-state") == TopLevelState.DONE


def test_done_section_user_resolved_jte_never_claims_company_confirmation(client_db, client):
    """Both JTE actions USER_COMPLETED -> CaseOutcome.USER_RESOLVED ->
    TopLevelState.DONE - the card must say "completed by you", never
    "confirmed"/"confirmed by" (that phrase is reserved for real company/
    system evidence, see app/case_outcome.py's USER_RESOLVED docstring)."""
    company = _company(client_db, name="Widget Co", deletion_status=DeletionStatus.READY)
    case = _case(client_db, company, RecipeChoice.JUST_THE_ESSENTIALS)
    _action(client_db, case, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP, PrivacyActionStatus.USER_COMPLETED)
    _action(client_db, case, PrivacyActionType.SALE_SHARING_OPT_OUT, PrivacyActionStatus.USER_COMPLETED)

    soup = _get_soup(client)
    section = _section(soup, "section-done")
    assert section is not None
    card = section.find("article", {"data-id": str(company.id)})
    assert card is not None
    text = card.get_text()
    assert "completed by you" in text
    assert "confirmed by" not in text
    assert card.get("data-top-level-state") == TopLevelState.DONE


# --- PANTRY --------------------------------------------------------------


def test_pantry_section_stays_separate_and_preserves_change_recipe(client_db, client):
    company = _company(client_db, name="Old Co", deletion_status=DeletionStatus.COMPLETED)
    _case(client_db, company, RecipeChoice.LEAVE_IT_BE)

    soup = _get_soup(client)
    # Never duplicated into any of the three active sections:
    for heading_id in ("section-needs-you", "section-working", "section-done"):
        section = _section(soup, heading_id)
        if section is not None:
            assert section.find("article", {"data-id": str(company.id)}) is None

    pantry_section = soup.find(class_="pantry-section")
    assert pantry_section is not None
    card = pantry_section.find("article", {"data-id": str(company.id)})
    assert card is not None
    assert card.get("data-top-level-state") == TopLevelState.PANTRY
    button = card.find("button", class_="delete-my-data-btn")
    assert button is not None
    assert button.get_text(strip=True) == "Change recipe"
    assert "failure" not in card.get_text().lower()
    assert "incomplete" not in card.get_text().lower()


def test_pantry_excluded_from_home_summary_counts(client_db, client):
    _company(client_db, name="Active Working Co", domain="active-working.example", deletion_status=DeletionStatus.IN_PROGRESS, deletion_thread_id="t1", waiting_on=WaitingOn.COMPANY)
    pantried = _company(client_db, name="Pantried Co", domain="pantried-co.example", deletion_status=DeletionStatus.IN_PROGRESS, deletion_thread_id="t2", waiting_on=WaitingOn.COMPANY)
    _case(client_db, pantried, RecipeChoice.LEAVE_IT_BE)

    soup = _get_soup(client)
    summary = soup.find(class_="home-summary")
    nums = [el.get_text() for el in summary.find_all(class_="home-summary-num")]
    assert nums == ["0", "1", "0"]  # needs you, working, done - Pantried Co never counted


# --- top_level_state is the single source of section placement ----------


def test_company_appears_in_exactly_one_active_section(client_db, client):
    """A company's top_level_state is looked up once and used consistently
    - it can never appear in more than one of Needs You/Working/Done."""
    company = _company(client_db, name="Solo Co", deletion_status=DeletionStatus.VERIFICATION_NEEDED, waiting_on=WaitingOn.USER)
    soup = _get_soup(client)
    matches = [
        heading_id for heading_id in ("section-needs-you", "section-working", "section-done")
        if (sec := _section(soup, heading_id)) is not None and sec.find("article", {"data-id": str(company.id)}) is not None
    ]
    assert matches == ["section-needs-you"]


# --- existing functionality remains reachable -----------------------------


def test_key_action_routes_remain_reachable_from_their_new_sections(client_db, client):
    ready_co = _company(client_db, name="Ready Co", domain="ready-co.example", deletion_status=DeletionStatus.READY)
    working_co = _company(
        client_db, name="Working Co", domain="working-co.example", deletion_status=DeletionStatus.SUBMITTED,
        deletion_thread_id="thread-x", waiting_on=WaitingOn.COMPANY,
    )
    soup = _get_soup(client)

    working_section = _section(soup, "section-working")
    ready_card = working_section.find("article", {"data-id": str(ready_co.id)})
    assert ready_card.find("button", class_="delete-my-data-btn") is not None  # recipe review/selection + Full Clean entry point

    working_card = working_section.find("article", {"data-id": str(working_co.id)})
    assert working_card.find("form", attrs={"action": f"/api/companies/{working_co.id}/deletion/pause-followups"}) is not None

    # JTE review/actions entry point still reachable via the same button:
    jte_co = _company(client_db, name="JTE Co", domain="jte-co.example", deletion_status=DeletionStatus.READY)
    _case(client_db, jte_co, RecipeChoice.JUST_THE_ESSENTIALS)
    soup = _get_soup(client)
    working_section = _section(soup, "section-working")
    jte_card = working_section.find("article", {"data-id": str(jte_co.id)})
    jte_btn = jte_card.find("button", class_="delete-my-data-btn")
    assert jte_btn is not None
    assert jte_btn.get_text(strip=True) == "Review cleanup"


def test_check_for_reply_button_reachable_in_new_sections(client_db, client):
    company = _company(
        client_db, deletion_status=DeletionStatus.SUBMITTED, deletion_thread_id="thread-y", waiting_on=WaitingOn.COMPANY,
    )
    # response_tracking_enabled requires readonly Gmail scope - simulate by
    # monkeypatching the same helper the route itself calls.
    import app.main as main_module
    orig = main_module.google_oauth.has_readonly_scope
    main_module.google_oauth.has_readonly_scope = lambda db: True
    try:
        soup = _get_soup(client)
    finally:
        main_module.google_oauth.has_readonly_scope = orig
    working_section = _section(soup, "section-working")
    card = working_section.find("article", {"data-id": str(company.id)})
    form = card.find("form", class_="check-response-form")
    assert form is not None


# --- mobile viewport --------------------------------------------------

_CHROMIUM_OVERRIDE = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
playwright_sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
sync_playwright = playwright_sync_api.sync_playwright


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

    db = dbmod.SessionLocal()
    _company(db, name="Needs You Co", domain="needs-you-co.example", deletion_status=DeletionStatus.VERIFICATION_NEEDED, waiting_on=WaitingOn.USER)
    _company(db, name="Working Co", domain="working-co-live.example", deletion_status=DeletionStatus.IN_PROGRESS, deletion_thread_id="t1", waiting_on=WaitingOn.COMPANY)
    _company(db, name="Done Co With A Genuinely Long Company Name LLC", domain="done-co-long-name.example", deletion_status=DeletionStatus.COMPLETED, deletion_completed_at=datetime.datetime(2024, 1, 1))
    pantried = _company(db, name="Pantried Co", domain="pantried-co-live.example", deletion_status=DeletionStatus.READY)
    case = PrivacyCase(company_id=pantried.id, selected_recipe=RecipeChoice.LEAVE_IT_BE)
    db.add(case)
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

    yield f"http://127.0.0.1:{port}"

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


def test_home_screen_has_no_horizontal_overflow_at_mobile_width(live_server, browser):
    page = browser.new_page(viewport={"width": 390, "height": 844})
    page.goto(f"{live_server}/dashboard")
    body_width = page.evaluate("document.body.scrollWidth")
    assert body_width <= 391, f"home screen overflows horizontally at 390px: {body_width}px"

    # Expand the Pantry too - collapsed content must not overflow either.
    page.click(".pantry-section summary")
    body_width = page.evaluate("document.body.scrollWidth")
    assert body_width <= 391, f"pantry section overflows horizontally at 390px: {body_width}px"
    page.close()


def test_home_screen_sections_visible_and_ordered_on_mobile(live_server, browser):
    page = browser.new_page(viewport={"width": 390, "height": 844})
    page.goto(f"{live_server}/dashboard")
    headings = page.locator(".section-heading").all_inner_texts()
    assert any("Needs you" in h for h in headings)
    assert any("Working" in h for h in headings)
    assert any("Done" in h for h in headings)
    page.close()
