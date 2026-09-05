"""Cleanup Recipes milestone, commit #4: Full Clean pre-commit UX -
recipe-choice, gating, and the review copy shown before the existing
preview/execute engine ever runs. Recipe selection and execution are
always separate requests; selecting FULL_CLEAN must never, by itself,
send or execute anything. Fabricated companies for all generic tests;
MALK/Goop appear only as historical regression fixtures.
"""
import datetime
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config
from app.case_outcome import derive_case_outcome
from app.db import Base
from app.deletion_constants import DeletionMethod, DeletionStatus, EventType, RecipeChoice
from app.models import Company, DeletionEvent, DeletionRecipe, OAuthToken, PrivacyCase
from app.privacy_case import full_clean_review_copy


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


def _company(db, name="Fabricated Co", domain="fabricated.example", **overrides) -> Company:
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


def _case(db, company, selected_recipe) -> PrivacyCase:
    case = PrivacyCase(company_id=company.id, selected_recipe=selected_recipe, recipe_selected_at=None)
    db.add(case)
    db.commit()
    return case


def _grant_send_scope(db) -> None:
    db.add(OAuthToken(
        gmail_address="me@gmail.com", encrypted_refresh_token="x",
        scopes_granted=" ".join(config.GMAIL_SCOPES + [config.GMAIL_SEND_SCOPE]),
    ))
    db.commit()


def _verified_recipe(db, domain, **overrides) -> DeletionRecipe:
    now = datetime.datetime.utcnow()
    defaults = dict(
        domain=domain, method=DeletionMethod.EMAIL_REQUEST, email="privacy@" + domain,
        status="VERIFIED", confidence="high", source_url=f"https://{domain}/privacy",
        verified_at=now, expires_at=now + datetime.timedelta(days=150), required_request_fields=[],
    )
    defaults.update(overrides)
    recipe = DeletionRecipe(**defaults)
    db.add(recipe)
    db.commit()
    return recipe


# --- 1. Selecting FULL_CLEAN alone never executes anything -----------------

def test_selecting_full_clean_alone_never_executes(client_db, client):
    company = _company(client_db)

    resp = client.post(f"/api/companies/{company.id}/privacy-case/recipe", data={"recipe": "FULL_CLEAN"})
    assert resp.status_code == 200

    client_db.expire_all()
    fetched = client_db.query(Company).filter(Company.id == company.id).one()
    case = client_db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
    assert case.selected_recipe == RecipeChoice.FULL_CLEAN
    assert fetched.deletion_status == DeletionStatus.READY  # unchanged - never SUBMITTED
    assert fetched.deletion_requested_at is None
    assert fetched.deletion_thread_id is None
    assert fetched.waiting_on is None
    assert fetched.next_followup_at is None
    assert client_db.query(DeletionEvent).filter(DeletionEvent.event_type == EventType.EMAIL_SENT).count() == 0


# --- 2. Full Clean preview fails closed without FULL_CLEAN selected --------

@pytest.mark.parametrize("recipe_state", [None, RecipeChoice.JUST_THE_ESSENTIALS, RecipeChoice.LEAVE_IT_BE])
def test_preview_fails_closed_without_full_clean(client_db, client, recipe_state):
    company = _company(client_db)
    if recipe_state is not None:
        _case(client_db, company, recipe_state)
    # else: no PrivacyCase at all - also must fail closed.

    resp = client.get(f"/api/companies/{company.id}/deletion/preview")
    assert resp.status_code == 400


def test_execute_fails_closed_without_full_clean(client_db, client):
    """Cannot bypass the gate by POSTing straight to execute, skipping
    preview entirely."""
    company = _company(client_db)
    resp = client.post(f"/api/companies/{company.id}/deletion/execute")
    assert resp.status_code == 400

    client_db.expire_all()
    fetched = client_db.query(Company).filter(Company.id == company.id).one()
    assert fetched.deletion_status == DeletionStatus.READY


# --- 3. FULL_CLEAN allows the preview path ---------------------------------

def test_preview_allowed_once_full_clean_selected(client_db, client):
    company = _company(client_db)
    _case(client_db, company, RecipeChoice.FULL_CLEAN)

    resp = client.get(f"/api/companies/{company.id}/deletion/preview")
    assert resp.status_code == 200
    assert "capability" in resp.json()


# --- 4/5/6/7: review copy content -------------------------------------------

def test_review_copy_generic_content(client_db):
    company = _company(client_db, name="Widget Co")
    copy = full_clean_review_copy(company, None)
    assert "Widget Co" in copy["summary"]
    assert "delete" in copy["summary"].lower()
    assert "personal information" in copy["explanation"].lower() or "personal data" in copy["explanation"].lower()
    assert "may close your account" in copy["explanation"].lower()
    assert "track" in copy["tracking_note"].lower()
    assert "evidence" in copy["tracking_note"].lower()


def test_review_copy_known_account_closure_is_stated_clearly(client_db):
    company = _company(client_db, name="Widget Co")
    recipe = _verified_recipe(client_db, company.domain, deletes_account=True)
    copy = full_clean_review_copy(company, recipe)
    assert "will close your account" in copy["explanation"].lower()
    assert "may close" not in copy["explanation"].lower()  # stronger, not hedged, when known


def test_review_copy_unknown_account_consequence_never_overclaims(client_db):
    company = _company(client_db, name="Widget Co")
    recipe = _verified_recipe(client_db, company.domain, deletes_account=None)
    copy = full_clean_review_copy(company, recipe)
    assert "may close your account" in copy["explanation"].lower()
    assert "will close your account" not in copy["explanation"].lower()


def test_review_copy_known_non_account_closure(client_db):
    company = _company(client_db, name="Widget Co")
    recipe = _verified_recipe(client_db, company.domain, deletes_account=False)
    copy = full_clean_review_copy(company, recipe)
    assert "isn't expected to close" in copy["explanation"].lower()
    assert "will close your account" not in copy["explanation"].lower()


def test_known_consequences_surfaced_without_fabrication(client_db, client):
    """Existing recipe.known_consequences wiring (already-proven, untouched)
    still flows through the execution plan unmodified."""
    company = _company(client_db, name="Widget Co")
    _verified_recipe(client_db, company.domain, known_consequences="Loyalty points will be forfeited.")
    _case(client_db, company, RecipeChoice.FULL_CLEAN)

    resp = client.get(f"/api/companies/{company.id}/deletion/preview")
    assert resp.status_code == 200
    assert resp.json()["consequences"] == "Loyalty points will be forfeited."


def test_dashboard_renders_full_clean_identity_and_copy(client_db, client):
    company = _company(client_db, name="Widget Co")
    _verified_recipe(client_db, company.domain, deletes_account=True)
    _case(client_db, company, RecipeChoice.FULL_CLEAN)

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    btn = soup.find("button", class_="delete-my-data-btn")
    assert btn is not None
    assert btn["data-full-clean-selected"] == "true"
    assert "Widget Co" in btn["data-recipe-summary"]
    assert "will close your account" in btn["data-recipe-explanation"].lower()
    assert "evidence" in btn["data-recipe-tracking"].lower()


def test_dashboard_shows_recipe_not_selected_for_new_company(client_db, client):
    _company(client_db, name="Fresh Co", domain="fresh.example")
    resp = client.get("/dashboard")
    soup = BeautifulSoup(resp.text, "html.parser")
    btn = soup.find("button", class_="delete-my-data-btn")
    assert btn is not None
    assert btn["data-full-clean-selected"] == "false"


# --- 8/9/10: execution capability semantics preserved after confirmation ---

def test_auto_executable_still_sends_after_full_clean_confirmation(client_db, client):
    company = _company(client_db, name="Widget Co", domain="widgetco.com")
    _verified_recipe(client_db, "widgetco.com")
    _grant_send_scope(client_db)
    _case(client_db, company, RecipeChoice.FULL_CLEAN)

    with patch("app.google_oauth.load_credentials") as mock_creds, \
         patch("app.google_oauth.send_email", return_value={"id": "msg-1", "threadId": "thread-1"}) as mock_send:
        mock_creds.return_value = object()
        resp = client.post(f"/api/companies/{company.id}/deletion/execute")
    assert resp.status_code in (200, 303)
    mock_send.assert_called_once()

    client_db.expire_all()
    fetched = client_db.query(Company).filter(Company.id == company.id).one()
    assert fetched.deletion_status == DeletionStatus.SUBMITTED
    assert fetched.deletion_thread_id == "thread-1"


def test_user_step_required_does_not_claim_submission(client_db, client):
    company = _company(client_db, name="Widget Co", domain="widgetco.com")
    _verified_recipe(client_db, "widgetco.com")
    # No send scope granted - degrades to USER_STEP_REQUIRED.
    _case(client_db, company, RecipeChoice.FULL_CLEAN)

    with patch("app.google_oauth.send_email") as mock_send:
        resp = client.post(f"/api/companies/{company.id}/deletion/execute")
    assert resp.status_code in (200, 303)
    mock_send.assert_not_called()

    client_db.expire_all()
    fetched = client_db.query(Company).filter(Company.id == company.id).one()
    assert fetched.deletion_status != DeletionStatus.SUBMITTED
    assert fetched.deletion_status == DeletionStatus.USER_ACTION_REQUIRED


def test_manual_handoff_does_not_claim_submission(client_db, client):
    company = _company(
        client_db, name="Widget Co", domain="widgetco.com",
        deletion_method=DeletionMethod.WEB_FORM, deletion_url="https://widgetco.com/privacy/delete",
    )
    _verified_recipe(client_db, "widgetco.com", method=DeletionMethod.WEB_FORM, email=None)
    _case(client_db, company, RecipeChoice.FULL_CLEAN)

    with patch("app.google_oauth.send_email") as mock_send:
        resp = client.post(f"/api/companies/{company.id}/deletion/execute")
    assert resp.status_code in (200, 303)
    mock_send.assert_not_called()

    client_db.expire_all()
    fetched = client_db.query(Company).filter(Company.id == company.id).one()
    assert fetched.deletion_status != DeletionStatus.SUBMITTED


# --- 11/12: explicit confirmation required; repeated preview never sends ---

def test_preview_alone_never_advances_status(client_db, client):
    company = _company(client_db, name="Widget Co", domain="widgetco.com")
    _verified_recipe(client_db, "widgetco.com")
    _grant_send_scope(client_db)
    _case(client_db, company, RecipeChoice.FULL_CLEAN)

    with patch("app.google_oauth.send_email") as mock_send:
        for _ in range(3):  # double/triple-click on "Delete my data" before ever confirming
            resp = client.get(f"/api/companies/{company.id}/deletion/preview")
            assert resp.status_code == 200
    mock_send.assert_not_called()

    client_db.expire_all()
    fetched = client_db.query(Company).filter(Company.id == company.id).one()
    assert fetched.deletion_status == DeletionStatus.READY


# --- 13: covered by test_execute_fails_closed_without_full_clean above -----

def test_mismatched_recipe_cannot_bypass_gate_via_direct_execute_post(client_db, client):
    company = _company(client_db, name="Widget Co", domain="widgetco.com")
    _verified_recipe(client_db, "widgetco.com")
    _grant_send_scope(client_db)
    _case(client_db, company, RecipeChoice.JUST_THE_ESSENTIALS)

    with patch("app.google_oauth.send_email") as mock_send:
        resp = client.post(f"/api/companies/{company.id}/deletion/execute")
    assert resp.status_code == 400
    mock_send.assert_not_called()


# --- 14: legacy in-flight cases (selected_recipe=None) keep functioning ----

def test_legacy_in_flight_case_unaffected_by_full_clean_gate(client_db, client):
    """A legacy company (no PrivacyCase, or selected_recipe=None) already
    past READY - actively being chased/tracked - must render fine and stay
    completely untouched. The gate only concerns STARTING a new execution
    via the READY/FAILED buttons, never maintaining an in-flight request."""
    legacy = _company(
        client_db, name="Legacy Co", domain="legacy.example",
        deletion_status=DeletionStatus.ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED,
        waiting_on="COMPANY", deletion_thread_id="thread-legacy",
        deletion_requested_at=datetime.datetime(2026, 1, 1),
    )
    before = (legacy.deletion_status, legacy.waiting_on, legacy.deletion_thread_id, legacy.deletion_requested_at)

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert client_db.query(PrivacyCase).filter(PrivacyCase.company_id == legacy.id).count() == 0

    client_db.expire_all()
    fetched = client_db.query(Company).filter(Company.id == legacy.id).one()
    after = (fetched.deletion_status, fetched.waiting_on, fetched.deletion_thread_id, fetched.deletion_requested_at)
    assert after == before


# --- 15: no completion awarded merely from selecting/previewing -----------

def test_no_completion_from_selecting_or_previewing_full_clean(client_db, client):
    company = _company(client_db, name="Widget Co", domain="widgetco.com")
    _verified_recipe(client_db, "widgetco.com")
    client.post(f"/api/companies/{company.id}/privacy-case/recipe", data={"recipe": "FULL_CLEAN"})
    client.get(f"/api/companies/{company.id}/deletion/preview")

    client_db.expire_all()
    fetched = client_db.query(Company).filter(Company.id == company.id).one()
    case = client_db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
    outcome = derive_case_outcome(fetched, case)
    assert outcome.personal_data != "DELETION_CONFIRMED"
    assert outcome.overall != "RESOLVED"


# --- 16: MALK/Goop regression shapes unaffected ----------------------------

def test_goop_regression_shape_unaffected_by_full_clean_commit():
    goop = Company(
        name="Goop Kitchen (fixture)", domain="goop-fixture.example", relationship_type="transactional",
        status="confirmed", confidence="high", evidence_count=1, evidence_types=[], example_subjects=[],
        detection_reasons=[], first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        deletion_status=DeletionStatus.ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED, waiting_on="COMPANY",
    )
    outcome = derive_case_outcome(goop, None)
    assert outcome.personal_data != "DELETION_CONFIRMED"
    assert outcome.overall == "WORKING"
    assert outcome.is_pantry is False


def test_malk_regression_shape_unaffected_by_full_clean_commit():
    malk = Company(
        name="MALK Organics (fixture)", domain="malk-fixture.example", relationship_type="transactional",
        status="confirmed", confidence="high", evidence_count=1, evidence_types=[], example_subjects=[],
        detection_reasons=[], first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        deletion_status=DeletionStatus.IN_PROGRESS, waiting_on="COMPANY",
    )
    outcome = derive_case_outcome(malk, None)
    assert outcome.personal_data != "DELETION_CONFIRMED"
    assert outcome.overall == "WORKING"
    assert outcome.is_pantry is False
