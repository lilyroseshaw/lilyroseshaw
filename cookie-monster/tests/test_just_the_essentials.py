"""Baker's Dozen V1 slice: JUST THE ESSENTIALS - recipe selection,
PrivacyAction materialization/tracking, CaseOutcome integration, and the
truthful preview route. No execution mechanism exists yet for any company
(see app/privacy_action.py's module docstring) - this proves the schema/
gating/outcome wiring is genuinely correct and non-fabricating, not that
anything gets automated yet. Fabricated companies for all generic tests;
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
from app.deletion_constants import (
    CaseState,
    DeletionMethod,
    DeletionStatus,
    EventType,
    NonessentialTrackingOutcome,
    OptOutOutcome,
    PrivacyActionStatus,
    PrivacyActionType,
    RecipeChoice,
)
from app.models import Company, DeletionEvent, PrivacyAction, PrivacyCase
from app.privacy_action import ensure_just_the_essentials_actions, just_the_essentials_review


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


# --- recipe selection supports JUST_THE_ESSENTIALS -------------------------

def test_recipe_selection_supports_just_the_essentials(client_db, client):
    company = _company(client_db)
    resp = client.post(
        f"/api/companies/{company.id}/privacy-case/recipe", data={"recipe": "JUST_THE_ESSENTIALS"}
    )
    assert resp.status_code == 200

    client_db.expire_all()
    case = client_db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
    assert case.selected_recipe == RecipeChoice.JUST_THE_ESSENTIALS


def test_selecting_just_the_essentials_materializes_both_actions(client_db, client):
    company = _company(client_db)
    client.post(f"/api/companies/{company.id}/privacy-case/recipe", data={"recipe": "JUST_THE_ESSENTIALS"})

    client_db.expire_all()
    case = client_db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
    actions = client_db.query(PrivacyAction).filter(PrivacyAction.privacy_case_id == case.id).all()
    assert {a.action_type for a in actions} == PrivacyActionType.ALL
    assert all(a.status == PrivacyActionStatus.NEEDS_RESEARCH for a in actions)


def test_selecting_full_clean_never_creates_privacy_actions(client_db, client):
    """Full Clean must remain untouched - it never gets PrivacyAction rows."""
    company = _company(client_db)
    client.post(f"/api/companies/{company.id}/privacy-case/recipe", data={"recipe": "FULL_CLEAN"})

    client_db.expire_all()
    case = client_db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
    assert client_db.query(PrivacyAction).filter(PrivacyAction.privacy_case_id == case.id).count() == 0


# --- zero execution side effects -------------------------------------------

def test_selecting_just_the_essentials_has_zero_execution_side_effects(client_db, client):
    company = _company(client_db)
    with patch("app.google_oauth.send_email") as mock_send:
        client.post(f"/api/companies/{company.id}/privacy-case/recipe", data={"recipe": "JUST_THE_ESSENTIALS"})
    mock_send.assert_not_called()

    client_db.expire_all()
    fetched = client_db.query(Company).filter(Company.id == company.id).one()
    assert fetched.deletion_status == DeletionStatus.READY  # unchanged
    assert fetched.deletion_requested_at is None
    assert fetched.deletion_thread_id is None
    assert fetched.waiting_on is None
    assert client_db.query(DeletionEvent).filter(DeletionEvent.event_type == EventType.EMAIL_SENT).count() == 0


def test_account_closure_not_requested_merely_from_selecting_just_the_essentials(client_db, client):
    """Selecting the recipe must never touch anything that could imply an
    account-deletion request went out."""
    company = _company(client_db, deletion_status=DeletionStatus.READY)
    before = (company.deletion_status, company.deletion_method, company.deletion_url, company.deletion_email)

    client.post(f"/api/companies/{company.id}/privacy-case/recipe", data={"recipe": "JUST_THE_ESSENTIALS"})

    client_db.expire_all()
    fetched = client_db.query(Company).filter(Company.id == company.id).one()
    after = (fetched.deletion_status, fetched.deletion_method, fetched.deletion_url, fetched.deletion_email)
    assert after == before


def test_full_clean_behavior_remains_unchanged(client_db, client):
    """Full Clean's own recipe-selection/gating behavior must be identical
    to before this milestone - selecting JUST_THE_ESSENTIALS support must
    not alter it."""
    company = _company(client_db)
    resp = client.post(f"/api/companies/{company.id}/privacy-case/recipe", data={"recipe": "FULL_CLEAN"})
    assert resp.status_code == 200
    client_db.expire_all()
    case = client_db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
    assert case.selected_recipe == RecipeChoice.FULL_CLEAN

    preview = client.get(f"/api/companies/{company.id}/deletion/preview")
    assert preview.status_code == 200
    assert "capability" in preview.json()


# --- unsupported/unknown mechanisms fail safely -----------------------------

def test_unsupported_mechanism_fails_safely_never_fabricated(client_db):
    """No verified mechanism exists for any company today - the classifier
    must say so honestly, never invent a URL/method."""
    company = _company(client_db, name="Amazon", domain="amazon.example")
    case = _case(client_db, company, RecipeChoice.JUST_THE_ESSENTIALS)
    actions = ensure_just_the_essentials_actions(client_db, case)

    for action in actions:
        assert action.status == PrivacyActionStatus.NEEDS_RESEARCH
        assert action.url is None
        assert action.method == "UNKNOWN"

    review = just_the_essentials_review(company, actions)
    for entry in review:
        assert entry["status"] == PrivacyActionStatus.NEEDS_RESEARCH
        assert "Amazon" in entry["reason"]
        assert "verified way" in entry["reason"]


def test_just_the_essentials_preview_route_never_fabricates(client_db, client):
    company = _company(client_db, name="Amazon", domain="amazon.example")
    client.post(f"/api/companies/{company.id}/privacy-case/recipe", data={"recipe": "JUST_THE_ESSENTIALS"})

    resp = client.get(f"/api/companies/{company.id}/just-the-essentials/preview")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["actions"]) == 2
    for entry in data["actions"]:
        assert entry["status"] == "NEEDS_RESEARCH"


def test_just_the_essentials_preview_gated_without_selection(client_db, client):
    company = _company(client_db)
    resp = client.get(f"/api/companies/{company.id}/just-the-essentials/preview")
    assert resp.status_code == 400


@pytest.mark.parametrize("other_recipe", [None, RecipeChoice.FULL_CLEAN, RecipeChoice.LEAVE_IT_BE])
def test_just_the_essentials_preview_gated_for_other_recipes(client_db, client, other_recipe):
    company = _company(client_db)
    if other_recipe is not None:
        _case(client_db, company, other_recipe)
    resp = client.get(f"/api/companies/{company.id}/just-the-essentials/preview")
    assert resp.status_code == 400


# --- portal opening is not completion; partial completion is not whole-recipe completion ---

def test_portal_opening_is_not_completion():
    """Setting a PrivacyAction's method/url (a future verified mechanism
    being opened) must never, by itself, read as CONFIRMED."""
    company = _company_obj("Fabricated Co", "fabricated.example")
    action = PrivacyAction(
        privacy_case_id=1, action_type=PrivacyActionType.SALE_SHARING_OPT_OUT,
        method=DeletionMethod.WEB_FORM, status=PrivacyActionStatus.USER_ACTION_REQUIRED,
        url="https://fabricated.example/privacy",
    )
    case = PrivacyCase(id=1, company_id=1, selected_recipe=RecipeChoice.JUST_THE_ESSENTIALS)
    outcome = derive_case_outcome(company, case, actions=[action])
    assert outcome.opt_out != OptOutOutcome.CONFIRMED
    assert outcome.overall == CaseState.NEEDS_USER


def test_partial_action_completion_cannot_mark_whole_recipe_complete():
    """One CONFIRMED action alongside a still-pending one must never
    resolve the overall case - a mixed outcome must stay WORKING/visible
    as incomplete, never falsely 'everything deleted'."""
    company = _company_obj("Fabricated Co", "fabricated.example")
    confirmed = PrivacyAction(
        privacy_case_id=1, action_type=PrivacyActionType.SALE_SHARING_OPT_OUT,
        status=PrivacyActionStatus.CONFIRMED,
    )
    pending = PrivacyAction(
        privacy_case_id=1, action_type=PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP,
        status=PrivacyActionStatus.NEEDS_RESEARCH,
    )
    case = PrivacyCase(id=1, company_id=1, selected_recipe=RecipeChoice.JUST_THE_ESSENTIALS)
    outcome = derive_case_outcome(company, case, actions=[confirmed, pending])
    assert outcome.opt_out == OptOutOutcome.CONFIRMED
    assert outcome.nonessential_tracking == NonessentialTrackingOutcome.UNRESOLVED
    assert outcome.overall != CaseState.RESOLVED
    assert outcome.overall == CaseState.WORKING


def test_all_actions_confirmed_resolves_the_case():
    company = _company_obj("Fabricated Co", "fabricated.example")
    actions = [
        PrivacyAction(privacy_case_id=1, action_type=t, status=PrivacyActionStatus.CONFIRMED)
        for t in sorted(PrivacyActionType.ALL)
    ]
    case = PrivacyCase(id=1, company_id=1, selected_recipe=RecipeChoice.JUST_THE_ESSENTIALS)
    outcome = derive_case_outcome(company, case, actions=actions)
    assert outcome.overall == CaseState.RESOLVED
    assert outcome.nonessential_tracking == NonessentialTrackingOutcome.CONFIRMED
    assert outcome.opt_out == OptOutOutcome.CONFIRMED


# --- account-retention intent remains represented ---------------------------

def test_account_retention_intent_is_never_lost_in_outcome():
    """JUST_THE_ESSENTIALS must never derive account=CLOSED or
    personal_data=DELETION_CONFIRMED from the recipe/actions alone - the
    account/personal-data axes stay purely evidence-derived from
    Company.deletion_status, untouched by this recipe's own actions."""
    company = _company_obj("Fabricated Co", "fabricated.example", deletion_status=DeletionStatus.NOT_STARTED)
    actions = [
        PrivacyAction(privacy_case_id=1, action_type=t, status=PrivacyActionStatus.CONFIRMED)
        for t in sorted(PrivacyActionType.ALL)
    ]
    case = PrivacyCase(id=1, company_id=1, selected_recipe=RecipeChoice.JUST_THE_ESSENTIALS)
    outcome = derive_case_outcome(company, case, actions=actions)
    assert outcome.account != "CLOSED"
    assert outcome.personal_data != "DELETION_CONFIRMED"


# --- evidence-backed outcomes remain distinct -------------------------------

def test_nonessential_tracking_and_opt_out_are_independent_axes():
    company = _company_obj("Fabricated Co", "fabricated.example")
    tracking_only = [
        PrivacyAction(
            privacy_case_id=1, action_type=PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP,
            status=PrivacyActionStatus.CONFIRMED,
        ),
        PrivacyAction(
            privacy_case_id=1, action_type=PrivacyActionType.SALE_SHARING_OPT_OUT,
            status=PrivacyActionStatus.NEEDS_RESEARCH,
        ),
    ]
    case = PrivacyCase(id=1, company_id=1, selected_recipe=RecipeChoice.JUST_THE_ESSENTIALS)
    outcome = derive_case_outcome(company, case, actions=tracking_only)
    assert outcome.nonessential_tracking == NonessentialTrackingOutcome.CONFIRMED
    assert outcome.opt_out == OptOutOutcome.UNKNOWN  # unaffected by tracking's outcome


def test_derive_case_outcome_without_actions_preserves_old_conservative_default():
    """Backward compatibility: omitting `actions` must behave exactly as
    it did before PrivacyAction existed - never CONFIRMED, never RESOLVED
    from the recipe alone."""
    company = _company_obj("Fabricated Co", "fabricated.example")
    case = PrivacyCase(id=1, company_id=1, selected_recipe=RecipeChoice.JUST_THE_ESSENTIALS)
    outcome = derive_case_outcome(company, case)
    assert outcome.nonessential_tracking == NonessentialTrackingOutcome.UNRESOLVED
    assert outcome.opt_out == OptOutOutcome.UNKNOWN


# --- no existing legacy Goop/MALK state is rewritten; no Pantry introduced ---

def test_legacy_goop_shape_unaffected_by_just_the_essentials():
    goop = _company_obj(
        "Goop Kitchen (fixture)", "goop-fixture.example",
        deletion_status=DeletionStatus.ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED, waiting_on="COMPANY",
    )
    outcome = derive_case_outcome(goop, None)
    assert outcome.personal_data != "DELETION_CONFIRMED"
    assert outcome.overall == "WORKING"
    assert outcome.is_pantry is False
    assert outcome.nonessential_tracking is None  # no recipe selected - not applicable, not UNRESOLVED
    assert outcome.opt_out is None


def test_malk_legacy_shape_unaffected_by_just_the_essentials():
    malk = _company_obj(
        "MALK Organics (fixture)", "malk-fixture.example",
        deletion_status=DeletionStatus.IN_PROGRESS, waiting_on="COMPANY",
    )
    outcome = derive_case_outcome(malk, None)
    assert outcome.personal_data != "DELETION_CONFIRMED"
    assert outcome.overall == "WORKING"
    assert outcome.is_pantry is False


def test_no_pantry_behavior_introduced(client_db, client):
    """LEAVE_IT_BE must not be offered/rendered anywhere in this
    milestone's dashboard, and selecting JUST_THE_ESSENTIALS must never
    set is_pantry."""
    company = _company(client_db)
    client.post(f"/api/companies/{company.id}/privacy-case/recipe", data={"recipe": "JUST_THE_ESSENTIALS"})

    resp = client.get("/dashboard")
    soup = BeautifulSoup(resp.text, "html.parser")
    assert soup.find(id="deletion-modal-choose-recipe") is not None
    assert "Leave It Be" not in resp.text
    assert "Choose Just the Essentials" in resp.text
    assert "Choose Full Clean" in resp.text

    client_db.expire_all()
    case = client_db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
    outcome = derive_case_outcome(
        client_db.query(Company).filter(Company.id == company.id).one(), case,
        actions=client_db.query(PrivacyAction).filter(PrivacyAction.privacy_case_id == case.id).all(),
    )
    assert outcome.is_pantry is False


def _company_obj(name, domain, **overrides):
    defaults = dict(
        name=name, domain=domain, relationship_type="transactional", status="confirmed",
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        deletion_status=DeletionStatus.NOT_STARTED,
    )
    defaults.update(overrides)
    return Company(**defaults)
