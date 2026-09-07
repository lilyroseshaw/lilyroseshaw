"""Leave It Be -> The Pantry milestone: makes the third Cleanup Recipe
functional. Leave It Be is a USER DISPOSITION (Pantry membership), never
deletion, never completion, and never Cookie Jar/privacy-progress reward.
Pantry membership is PURELY DERIVED from PrivacyCase.selected_recipe ==
RecipeChoice.LEAVE_IT_BE - no separate is_pantry column exists or should
exist. Fabricated companies only.
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
from app.models import Company, DeletionEvent, PrivacyAction, PrivacyCase
from app.privacy_case import pantry_company_ids


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


def _select_recipe(client, company_id, recipe):
    return client.post(f"/api/companies/{company_id}/privacy-case/recipe", data={"recipe": recipe})


# --- 1. LEAVE_IT_BE appears as the third recipe -----------------------------

def test_leave_it_be_appears_as_third_recipe_in_picker(client_db, client):
    company = _company(client_db)
    resp = client.get("/dashboard")
    soup = BeautifulSoup(resp.text, "html.parser")

    picker = soup.find(id="deletion-modal-choose-recipe")
    assert picker is not None
    text = picker.get_text()
    assert "Full Clean" in text
    assert "Just the Essentials" in text
    assert "Leave It Be" in text
    assert "Keep things as they are" in text

    buttons = [b.get_text(strip=True) for b in soup.find_all(id=[
        "deletion-modal-recipe-submit", "deletion-modal-jte-submit", "deletion-modal-leave-it-be-submit",
    ])]
    assert "Choose Full Clean" in buttons
    assert "Choose Just the Essentials" in buttons
    assert "Choose Leave It Be" in buttons


# --- 2/3. selecting it persists selected_recipe + RECIPE_SELECTED audit ----

def test_selecting_leave_it_be_persists_selected_recipe(client_db, client):
    company = _company(client_db)
    resp = _select_recipe(client, company.id, "LEAVE_IT_BE")
    assert resp.status_code == 200

    client_db.expire_all()
    case = client_db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
    assert case.selected_recipe == RecipeChoice.LEAVE_IT_BE
    assert case.recipe_selected_at is not None


def test_selecting_leave_it_be_records_recipe_selected_event(client_db, client):
    company = _company(client_db)
    _select_recipe(client, company.id, "LEAVE_IT_BE")

    client_db.expire_all()
    event = (
        client_db.query(DeletionEvent)
        .filter(DeletionEvent.company_id == company.id, DeletionEvent.event_type == EventType.RECIPE_SELECTED)
        .one()
    )
    assert event.evidence["selected_recipe"] == RecipeChoice.LEAVE_IT_BE
    assert event.evidence["previous_recipe"] is None


def test_reselecting_leave_it_be_is_idempotent_no_op(client_db, client):
    """Same repository convention as Full Clean/JTE re-selection - a
    repeated identical choice never fabricates new audit history."""
    company = _company(client_db)
    _select_recipe(client, company.id, "LEAVE_IT_BE")
    client_db.expire_all()
    case = client_db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
    first_selected_at = case.recipe_selected_at

    _select_recipe(client, company.id, "LEAVE_IT_BE")
    client_db.expire_all()
    case = client_db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
    assert case.recipe_selected_at == first_selected_at
    assert client_db.query(DeletionEvent).filter(
        DeletionEvent.company_id == company.id, DeletionEvent.event_type == EventType.RECIPE_SELECTED
    ).count() == 1


# --- selection triggers NO execution/Gmail/research/follow-up --------------

def test_selecting_leave_it_be_has_zero_execution_side_effects(client_db, client):
    company = _company(client_db, deletion_status=DeletionStatus.READY)
    with patch("app.google_oauth.send_email") as mock_send:
        _select_recipe(client, company.id, "LEAVE_IT_BE")
    mock_send.assert_not_called()

    client_db.expire_all()
    fetched = client_db.query(Company).filter(Company.id == company.id).one()
    assert fetched.deletion_status == DeletionStatus.READY  # unchanged - never touched
    assert fetched.deletion_requested_at is None
    assert fetched.deletion_thread_id is None
    assert fetched.waiting_on is None
    assert fetched.next_followup_at is None
    assert client_db.query(DeletionEvent).filter(DeletionEvent.event_type == EventType.EMAIL_SENT).count() == 0
    assert client_db.query(DeletionEvent).filter(DeletionEvent.event_type == EventType.EXECUTION_STARTED).count() == 0
    # No JTE-style PrivacyAction materialization either - that's specific
    # to JUST_THE_ESSENTIALS and must never fire for LEAVE_IT_BE.
    client_db.expire_all()
    case = client_db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
    assert client_db.query(PrivacyAction).filter(PrivacyAction.privacy_case_id == case.id).count() == 0


def test_selecting_leave_it_be_never_starts_deletion_research(client_db, client):
    company = _company(client_db, deletion_status=DeletionStatus.NOT_STARTED, deletion_verified=False)
    _select_recipe(client, company.id, "LEAVE_IT_BE")

    client_db.expire_all()
    fetched = client_db.query(Company).filter(Company.id == company.id).one()
    assert fetched.deletion_status == DeletionStatus.NOT_STARTED  # never moved to METHOD_LOOKUP or beyond


def test_selecting_leave_it_be_does_not_execute_deletion_route(client_db, client):
    """Fails closed: LEAVE_IT_BE must never satisfy Full Clean's own gate."""
    company = _company(client_db)
    _select_recipe(client, company.id, "LEAVE_IT_BE")
    resp = client.post(f"/api/companies/{company.id}/deletion/execute")
    assert resp.status_code == 400


# --- Pantry membership is derived, never a second persisted state ----------

def test_pantry_membership_is_derived_not_a_stored_column():
    columns = {c.name for c in PrivacyCase.__table__.columns}
    assert "is_pantry" not in columns
    assert columns == {"id", "company_id", "selected_recipe", "recipe_selected_at", "created_at", "updated_at"}


def test_pantry_company_ids_reflects_only_leave_it_be(client_db, client):
    leave_it_be_co = _company(client_db, name="Left Alone Co", domain="leftalone-corp.com")
    full_clean_co = _company(client_db, name="Cleaned Co", domain="cleaned-corp.com")
    jte_co = _company(client_db, name="Essentials Co", domain="essentials-corp.com")
    _select_recipe(client, leave_it_be_co.id, "LEAVE_IT_BE")
    _select_recipe(client, full_clean_co.id, "FULL_CLEAN")
    _select_recipe(client, jte_co.id, "JUST_THE_ESSENTIALS")

    ids = pantry_company_ids(client_db)
    assert ids == {leave_it_be_co.id}


# --- Pantry company appears in Pantry, not the active area -----------------

def test_pantry_company_appears_in_pantry_and_not_active_area(client_db, client):
    company = _company(client_db)
    _select_recipe(client, company.id, "LEAVE_IT_BE")

    resp = client.get("/dashboard")
    soup = BeautifulSoup(resp.text, "html.parser")

    # Exactly one element with this id anywhere on the page - never
    # duplicated between the active list and the Pantry.
    matches = soup.find_all(id=f"company-{company.id}")
    assert len(matches) == 1

    active_scope = soup.find(id="merge-select-scope")
    assert active_scope.find(id=f"company-{company.id}") is None

    pantry_section = soup.find(class_="pantry-section")
    assert pantry_section is not None
    assert pantry_section.find(id=f"company-{company.id}") is not None
    assert "Leave It Be" in pantry_section.get_text()
    assert "Change recipe" in pantry_section.get_text()


def test_pantry_empty_state_is_compact_when_no_companies_pantried(client_db, client):
    _company(client_db)  # exists, but never pantried
    resp = client.get("/dashboard")
    soup = BeautifulSoup(resp.text, "html.parser")
    pantry_section = soup.find(class_="pantry-section")
    assert pantry_section is not None
    assert "Nothing here yet." in pantry_section.get_text()


# --- Pantry is never counted as completed/deleted/rewarded ------------------

def test_pantry_company_excluded_from_active_workload_counts(client_db, client):
    """A company that already had a deletion method found (READY) before
    the user chose Leave It Be must not keep reading as active workload -
    see main.py's _status_counts."""
    ready_pantry_co = _company(client_db, name="Ready Pantry Co", domain="readypantry-corp.com", deletion_status=DeletionStatus.READY)
    ready_active_co = _company(client_db, name="Ready Active Co", domain="readyactive-corp.com", deletion_status=DeletionStatus.READY)
    _select_recipe(client, ready_pantry_co.id, "LEAVE_IT_BE")

    from app.main import _status_counts
    counts = _status_counts(client_db)
    assert counts["methods_ready"] == 1  # only the still-active company counts
    assert counts["total"] == 2  # discovery-level count is untouched


def test_pantry_company_completed_evidence_still_counts_as_genuine_evidence(client_db, client):
    """The opposite direction of the safety rule: a REAL, evidence-backed
    completion must never be erased from the count just because the user
    later chose Leave It Be for that company - "never change evidence-
    derived truth"."""
    completed_co = _company(
        client_db, name="Completed Co", domain="completed-corp.com",
        deletion_status=DeletionStatus.COMPLETED, deletion_completed_at=datetime.datetime(2024, 1, 1),
    )
    _select_recipe(client, completed_co.id, "LEAVE_IT_BE")

    from app.main import _status_counts
    counts = _status_counts(client_db)
    assert counts["deleted"] == 1


def test_pantry_not_treated_as_completed_in_outcome():
    """derive_case_outcome must never report a Pantry disposition as a
    resolved/rewarded privacy outcome by itself."""
    company = Company(
        name="Fabricated Co", domain="fabricated-corp.com", relationship_type="transactional",
        status="confirmed", confidence="high", evidence_count=1, evidence_types=[], example_subjects=[],
        detection_reasons=[], first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        deletion_status=DeletionStatus.NOT_STARTED,
    )
    case = PrivacyCase(id=1, company_id=1, selected_recipe=RecipeChoice.LEAVE_IT_BE)
    outcome = derive_case_outcome(company, case)
    assert outcome.is_pantry is True
    assert outcome.overall != "RESOLVED"
    assert outcome.personal_data != "DELETION_CONFIRMED"


# --- changing recipe removes from Pantry, no auto-execution -----------------

def test_changing_from_leave_it_be_to_full_clean_removes_from_pantry(client_db, client):
    company = _company(client_db)
    _select_recipe(client, company.id, "LEAVE_IT_BE")
    assert pantry_company_ids(client_db) == {company.id}

    with patch("app.google_oauth.send_email") as mock_send:
        resp = _select_recipe(client, company.id, "FULL_CLEAN")
    assert resp.status_code == 200
    mock_send.assert_not_called()

    client_db.expire_all()
    assert pantry_company_ids(client_db) == set()
    case = client_db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
    assert case.selected_recipe == RecipeChoice.FULL_CLEAN

    fetched = client_db.query(Company).filter(Company.id == company.id).one()
    assert fetched.deletion_status == DeletionStatus.READY  # unchanged by the recipe switch itself
    assert fetched.deletion_requested_at is None


def test_changing_from_leave_it_be_to_just_the_essentials_returns_to_active_workflow(client_db, client):
    company = _company(client_db)
    _select_recipe(client, company.id, "LEAVE_IT_BE")
    _select_recipe(client, company.id, "JUST_THE_ESSENTIALS")

    client_db.expire_all()
    assert pantry_company_ids(client_db) == set()
    case = client_db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
    assert case.selected_recipe == RecipeChoice.JUST_THE_ESSENTIALS
    # JTE's own materialization step still runs normally, since this is
    # the SAME select_company_recipe route/logic as any other selection.
    assert client_db.query(PrivacyAction).filter(PrivacyAction.privacy_case_id == case.id).count() == 2

    resp = client.get("/dashboard")
    soup = BeautifulSoup(resp.text, "html.parser")
    active_scope = soup.find(id="merge-select-scope")
    assert active_scope.find(id=f"company-{company.id}") is not None
    pantry_section = soup.find(class_="pantry-section")
    assert pantry_section.find(id=f"company-{company.id}") is None


def test_changing_to_leave_it_be_moves_company_into_pantry(client_db, client):
    company = _company(client_db)
    _select_recipe(client, company.id, "FULL_CLEAN")
    _select_recipe(client, company.id, "LEAVE_IT_BE")

    client_db.expire_all()
    assert pantry_company_ids(client_db) == {company.id}


# --- historical evidence survives recipe changes unchanged ------------------

def test_historical_evidence_survives_recipe_change_to_leave_it_be(client_db, client):
    """A company with REAL historical deletion evidence must keep that
    evidence exactly as it was - Leave It Be is only current disposition,
    never a rewrite of what already happened."""
    company = _company(
        client_db, deletion_status=DeletionStatus.COMPLETED,
        deletion_completed_at=datetime.datetime(2024, 3, 1),
        deletion_evidence={"type": "gmail_reply", "quote": "Your data has been deleted."},
    )
    from app.deletion_events import record_event
    record_event(client_db, company.id, EventType.COMPLETION_CONFIRMED, evidence={"note": "historical"})
    client_db.commit()
    events_before = client_db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).count()

    _select_recipe(client, company.id, "LEAVE_IT_BE")

    client_db.expire_all()
    fetched = client_db.query(Company).filter(Company.id == company.id).one()
    assert fetched.deletion_status == DeletionStatus.COMPLETED
    assert fetched.deletion_completed_at == datetime.datetime(2024, 3, 1)
    assert fetched.deletion_evidence == {"type": "gmail_reply", "quote": "Your data has been deleted."}
    # No historical event was rewritten or removed - only a new
    # RECIPE_SELECTED event was appended.
    events_after = client_db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).all()
    assert len(events_after) == events_before + 1
    assert any(e.event_type == EventType.COMPLETION_CONFIRMED for e in events_after)


def test_historical_evidence_survives_recipe_change_away_from_leave_it_be(client_db, client):
    company = _company(
        client_db, deletion_status=DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED,
        waiting_on="COMPANY",
    )
    _select_recipe(client, company.id, "LEAVE_IT_BE")
    _select_recipe(client, company.id, "FULL_CLEAN")

    client_db.expire_all()
    fetched = client_db.query(Company).filter(Company.id == company.id).one()
    assert fetched.deletion_status == DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED
    assert fetched.waiting_on == "COMPANY"


# --- Full Clean / Just the Essentials remain unchanged ----------------------

def test_full_clean_remains_unchanged(client_db, client):
    company = _company(client_db)
    resp = _select_recipe(client, company.id, "FULL_CLEAN")
    assert resp.status_code == 200
    client_db.expire_all()
    case = client_db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
    assert case.selected_recipe == RecipeChoice.FULL_CLEAN

    preview = client.get(f"/api/companies/{company.id}/deletion/preview")
    assert preview.status_code == 200
    assert "capability" in preview.json()


def test_just_the_essentials_remains_unchanged(client_db, client):
    company = _company(client_db)
    resp = _select_recipe(client, company.id, "JUST_THE_ESSENTIALS")
    assert resp.status_code == 200
    client_db.expire_all()
    case = client_db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
    assert case.selected_recipe == RecipeChoice.JUST_THE_ESSENTIALS
    assert client_db.query(PrivacyAction).filter(PrivacyAction.privacy_case_id == case.id).count() == 2

    preview = client.get(f"/api/companies/{company.id}/just-the-essentials/preview")
    assert preview.status_code == 200
    assert len(preview.json()["actions"]) == 2


# --- regression: Pantry -> Just the Essentials must present the JTE
# workflow, never Full Clean's "Delete my data" card projection -----------

def test_pantry_to_just_the_essentials_card_shows_jte_next_action_not_delete_my_data(client_db, client):
    """Live-browser-shaped repro: a company (e.g. a TikTok Shop Creator
    fixture) was READY with a found web-form deletion method, sent to the
    Pantry via Leave It Be, then changed to Just the Essentials from
    there. The active card that comes back must present the Just the
    Essentials workflow - never "Delete my data" / "Deletion method
    ready" as though Full Clean were the selected recipe, even though
    Company.deletion_status/deletion_method (Full Clean's own historical
    fields) are still exactly READY/WEB_FORM underneath."""
    company = _company(
        client_db, name="TikTok Shop Creator", domain="tiktokshopcreator-fixture.com",
        deletion_method=DeletionMethod.WEB_FORM, deletion_status=DeletionStatus.READY,
        deletion_url="https://tiktokshopcreator-fixture.com/privacy/delete",
    )
    _select_recipe(client, company.id, "LEAVE_IT_BE")
    resp = _select_recipe(client, company.id, "JUST_THE_ESSENTIALS")
    assert resp.status_code == 200

    dash = client.get("/dashboard")
    soup = BeautifulSoup(dash.text, "html.parser")
    card = soup.find(id=f"company-{company.id}")
    assert card is not None

    card_text = card.get_text()
    assert "Delete my data" not in card_text
    assert "Deletion method ready" not in card_text
    assert "Just the Essentials selected" in card_text

    button = card.find(class_="delete-my-data-btn")
    assert button is not None
    assert button.get_text(strip=True) == "Review cleanup"
    assert button["data-selected-recipe"] == "JUST_THE_ESSENTIALS"

    # And it's genuinely wired to the JTE workflow, not a dead end.
    preview = client.get(f"/api/companies/{company.id}/just-the-essentials/preview")
    assert preview.status_code == 200
    assert len(preview.json()["actions"]) == 2


def test_pantry_to_just_the_essentials_failed_status_also_shows_jte_next_action(client_db, client):
    """Same projection bug, the other branch that renders the shared
    button: a company stuck in FAILED (a stale Full Clean send failure)
    must also present Just the Essentials, never "Send again"."""
    company = _company(
        client_db, deletion_status=DeletionStatus.FAILED, deletion_error="SMTP timeout",
        deletion_method=DeletionMethod.EMAIL_REQUEST,
    )
    _select_recipe(client, company.id, "LEAVE_IT_BE")
    _select_recipe(client, company.id, "JUST_THE_ESSENTIALS")

    dash = client.get("/dashboard")
    soup = BeautifulSoup(dash.text, "html.parser")
    card = soup.find(id=f"company-{company.id}")
    card_text = card.get_text()
    assert "Send again" not in card_text
    assert "Couldn't send" not in card_text
    assert "Just the Essentials selected" in card_text
    button = card.find(class_="delete-my-data-btn")
    assert button.get_text(strip=True) == "Review cleanup"


def test_pantry_to_just_the_essentials_triggers_no_external_action(client_db, client):
    """Changing FROM Leave It Be TO Just the Essentials must never itself
    research, submit, send Gmail, execute deletion, or opt out - it is
    the exact same intent-only select_recipe() path as any other recipe
    change."""
    company = _company(
        client_db, deletion_status=DeletionStatus.READY, deletion_method=DeletionMethod.WEB_FORM,
        deletion_url="https://fabricated-corp.com/privacy/delete",
    )
    _select_recipe(client, company.id, "LEAVE_IT_BE")
    with patch("app.google_oauth.send_email") as mock_send:
        resp = _select_recipe(client, company.id, "JUST_THE_ESSENTIALS")
    assert resp.status_code == 200
    mock_send.assert_not_called()

    client_db.expire_all()
    fetched = client_db.query(Company).filter(Company.id == company.id).one()
    assert fetched.deletion_requested_at is None
    assert fetched.deletion_thread_id is None
    assert fetched.waiting_on is None
    assert client_db.query(DeletionEvent).filter(DeletionEvent.event_type == EventType.EMAIL_SENT).count() == 0
    assert client_db.query(DeletionEvent).filter(DeletionEvent.event_type == EventType.EXECUTION_STARTED).count() == 0
    assert client_db.query(DeletionEvent).filter(
        DeletionEvent.event_type == EventType.PRIVACY_ACTION_METHOD_FOUND
    ).count() == 0  # JTE research is never auto-triggered by a recipe change either

    # The two PrivacyAction rows exist (materialized), but both still
    # honestly sit at NEEDS_RESEARCH - nothing was researched/opted out.
    case = client_db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
    actions = client_db.query(PrivacyAction).filter(PrivacyAction.privacy_case_id == case.id).all()
    assert len(actions) == 2
    assert all(a.status == "NEEDS_RESEARCH" for a in actions)


def test_pantry_to_just_the_essentials_preserves_historical_full_clean_evidence(client_db, client):
    """Company.deletion_method/deletion_status/deletion_url (Full Clean's
    own historical fields) must remain exactly as they are - current
    recipe intent changes which workflow is PRESENTED, never rewrites or
    erases what actually happened/was found."""
    company = _company(
        client_db, deletion_status=DeletionStatus.READY, deletion_method=DeletionMethod.WEB_FORM,
        deletion_url="https://fabricated-corp.com/privacy/delete", deletion_verified=True,
    )
    _select_recipe(client, company.id, "LEAVE_IT_BE")
    _select_recipe(client, company.id, "JUST_THE_ESSENTIALS")

    client_db.expire_all()
    fetched = client_db.query(Company).filter(Company.id == company.id).one()
    assert fetched.deletion_status == DeletionStatus.READY
    assert fetched.deletion_method == DeletionMethod.WEB_FORM
    assert fetched.deletion_url == "https://fabricated-corp.com/privacy/delete"
    assert fetched.deletion_verified is True


# --- symmetric invariant: recipe -> workflow projection ---------------------

def test_symmetric_invariant_full_clean_shows_delete_my_data(client_db, client):
    company = _company(client_db, deletion_status=DeletionStatus.READY)
    _select_recipe(client, company.id, "FULL_CLEAN")

    dash = client.get("/dashboard")
    soup = BeautifulSoup(dash.text, "html.parser")
    button = soup.find(id=f"company-{company.id}").find(class_="delete-my-data-btn")
    assert button.get_text(strip=True) == "Delete my data"
    assert button["data-selected-recipe"] == "FULL_CLEAN"


def test_symmetric_invariant_just_the_essentials_shows_review_cleanup(client_db, client):
    company = _company(client_db, deletion_status=DeletionStatus.READY)
    _select_recipe(client, company.id, "JUST_THE_ESSENTIALS")

    dash = client.get("/dashboard")
    soup = BeautifulSoup(dash.text, "html.parser")
    button = soup.find(id=f"company-{company.id}").find(class_="delete-my-data-btn")
    assert button.get_text(strip=True) == "Review cleanup"
    assert button["data-selected-recipe"] == "JUST_THE_ESSENTIALS"


def test_symmetric_invariant_leave_it_be_shows_in_pantry_not_active(client_db, client):
    company = _company(client_db, deletion_status=DeletionStatus.READY)
    _select_recipe(client, company.id, "LEAVE_IT_BE")

    dash = client.get("/dashboard")
    soup = BeautifulSoup(dash.text, "html.parser")
    assert soup.find(id="merge-select-scope").find(id=f"company-{company.id}") is None
    assert soup.find(class_="pantry-section").find(id=f"company-{company.id}") is not None


def test_symmetric_invariant_no_recipe_selected_keeps_legacy_delete_my_data(client_db, client):
    """selected_recipe=None (no PrivacyCase choice made yet) must keep
    exactly the pre-Pantry legacy behavior - "Delete my data", not a
    change in wording just because the Pantry milestone shipped."""
    company = _company(client_db, deletion_status=DeletionStatus.READY)

    dash = client.get("/dashboard")
    soup = BeautifulSoup(dash.text, "html.parser")
    button = soup.find(id=f"company-{company.id}").find(class_="delete-my-data-btn")
    assert button.get_text(strip=True) == "Delete my data"
    assert button["data-selected-recipe"] == ""
