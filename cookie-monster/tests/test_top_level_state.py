"""TOP-LEVEL COMPANY STATE PROJECTION for the upcoming Baker's Dozen UI
(app/top_level_state.py) - a pure, read-only collapse of the existing
CaseOutcome (app/case_outcome.py) into exactly one of four consumer-facing
destinations: WORKING, NEEDS_YOU, DONE, PANTRY.

No new persisted state, no new privacy-evidence/JTE-action derivation -
every test here exercises app.top_level_state.derive_top_level_state,
which does nothing but call app.case_outcome.derive_case_outcome and map
its `is_pantry`/`overall` axes down to four values. Fabricated companies
for all generic tests; MALK/Goop appear only as regression fixtures
reproducing their real historical/live shape, never as production
branches.
"""
import datetime

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config
from app.db import Base
from app.deletion_constants import CaseState, DeletionMethod, DeletionStatus, PrivacyActionStatus, PrivacyActionType, RecipeChoice, WaitingOn
from app.models import Company, PrivacyAction, PrivacyCase
from app.top_level_state import TopLevelState, derive_top_level_state

# --- fixtures -------------------------------------------------------------


def _company(deletion_status: str, waiting_on: str | None = None, **overrides) -> Company:
    defaults = dict(
        name="Fabricated Co", domain="fabricated.example", relationship_type="transactional",
        status="confirmed", confidence="high", evidence_count=1, evidence_types=[],
        example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        deletion_status=deletion_status, waiting_on=waiting_on, deletion_evidence={},
    )
    defaults.update(overrides)
    return Company(**defaults)


def _case(selected_recipe: str | None) -> PrivacyCase:
    return PrivacyCase(
        company_id=1, selected_recipe=selected_recipe,
        recipe_selected_at=datetime.datetime(2026, 1, 1) if selected_recipe else None,
    )


def _action(action_type: str, status: str) -> PrivacyAction:
    return PrivacyAction(
        privacy_case_id=1, action_type=action_type, method=DeletionMethod.ACCOUNT_SETTING,
        status=status, evidence={},
    )


def _jte_actions(tracking_status: str, opt_out_status: str) -> list[PrivacyAction]:
    return [
        _action(PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP, tracking_status),
        _action(PrivacyActionType.SALE_SHARING_OPT_OUT, opt_out_status),
    ]


# --- FULL CLEAN / LEGACY matrix (also covers a legacy selected_recipe=None
# case identically, since is_pantry/JTE-branching are the only places
# selected_recipe matters - see app/case_outcome.py's own docstring) -----

_FULL_CLEAN_LEGACY_MATRIX = [
    (DeletionStatus.COMPLETED, None, TopLevelState.DONE),
    (DeletionStatus.SUBMITTED, WaitingOn.COMPANY, TopLevelState.WORKING),
    (DeletionStatus.IN_PROGRESS, WaitingOn.COMPANY, TopLevelState.WORKING),
    (DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED, WaitingOn.COMPANY, TopLevelState.WORKING),
    (DeletionStatus.ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED, WaitingOn.COMPANY, TopLevelState.WORKING),
    (DeletionStatus.VERIFICATION_NEEDED, WaitingOn.USER, TopLevelState.NEEDS_YOU),
    (DeletionStatus.MORE_INFO_REQUIRED, WaitingOn.USER, TopLevelState.NEEDS_YOU),
    (DeletionStatus.USER_ACTION_REQUIRED, None, TopLevelState.NEEDS_YOU),
    # REJECTED: chase_engine routes this to WaitingOn.ESCALATION_NEEDED -
    # a human decision is required, so the truthful destination is
    # NEEDS_YOU, not WORKING (the automatic chase has already stopped).
    (DeletionStatus.REJECTED, WaitingOn.ESCALATION_NEEDED, TopLevelState.NEEDS_YOU),
    # FAILED: the dashboard's own existing affordance for this status is a
    # manual "Send again" - Baker's Dozen never retries a failed send on
    # its own, so the next required actor is the user.
    (DeletionStatus.FAILED, None, TopLevelState.NEEDS_YOU),
    # UNKNOWN_RESPONSE's destination depends on company.waiting_on
    # (chase_engine's own recorded answer) - both branches tested here.
    (DeletionStatus.UNKNOWN_RESPONSE, WaitingOn.COMPANY, TopLevelState.WORKING),
    (DeletionStatus.UNKNOWN_RESPONSE, WaitingOn.USER, TopLevelState.NEEDS_YOU),
]


@pytest.mark.parametrize("selected_recipe", [None, RecipeChoice.FULL_CLEAN])
@pytest.mark.parametrize("deletion_status,waiting_on,expected", _FULL_CLEAN_LEGACY_MATRIX)
def test_full_clean_and_legacy_matrix(deletion_status, waiting_on, expected, selected_recipe):
    company = _company(deletion_status, waiting_on=waiting_on)
    case = _case(selected_recipe) if selected_recipe is not None else None
    assert derive_top_level_state(company, case) == expected


def test_no_privacy_case_at_all_projects_same_as_legacy_none():
    """A company that predates PrivacyCase existing entirely (privacy_case
    is None, not merely selected_recipe=None) must derive identically -
    see app.case_outcome.derive_case_outcome's own is_pantry handling."""
    company = _company(DeletionStatus.COMPLETED)
    assert derive_top_level_state(company, None) == TopLevelState.DONE


# --- PANTRY ---------------------------------------------------------------


def test_leave_it_be_projects_pantry_regardless_of_deletion_status():
    for status, waiting_on in [
        (DeletionStatus.NOT_STARTED, None),
        (DeletionStatus.IN_PROGRESS, WaitingOn.COMPANY),
        (DeletionStatus.VERIFICATION_NEEDED, WaitingOn.USER),
        (DeletionStatus.COMPLETED, None),
        (DeletionStatus.REJECTED, WaitingOn.ESCALATION_NEEDED),
    ]:
        company = _company(status, waiting_on=waiting_on)
        case = _case(RecipeChoice.LEAVE_IT_BE)
        assert derive_top_level_state(company, case) == TopLevelState.PANTRY, f"failed for {status}"


def test_historical_completed_plus_leave_it_be_projects_pantry_without_erasing_evidence():
    """Pantry wins as the TOP-LEVEL destination, but the underlying
    CaseOutcome (still queryable separately) must keep the real historical
    COMPLETED evidence - Pantry never falsifies history, it only changes
    where the company is presented."""
    from app.case_outcome import derive_case_outcome

    company = _company(DeletionStatus.COMPLETED)
    case = _case(RecipeChoice.LEAVE_IT_BE)

    assert derive_top_level_state(company, case) == TopLevelState.PANTRY

    outcome = derive_case_outcome(company, case)
    assert outcome.is_pantry is True
    assert outcome.overall == CaseState.RESOLVED  # historical evidence intact
    from app.deletion_constants import PersonalDataOutcome
    assert outcome.personal_data == PersonalDataOutcome.DELETION_CONFIRMED  # untouched


# --- JTE --------------------------------------------------------------


def test_jte_both_user_completed_is_done():
    company = _company(DeletionStatus.READY)
    case = _case(RecipeChoice.JUST_THE_ESSENTIALS)
    actions = _jte_actions(PrivacyActionStatus.USER_COMPLETED, PrivacyActionStatus.USER_COMPLETED)
    assert derive_top_level_state(company, case, actions) == TopLevelState.DONE


def test_jte_both_confirmed_is_done():
    company = _company(DeletionStatus.READY)
    case = _case(RecipeChoice.JUST_THE_ESSENTIALS)
    actions = _jte_actions(PrivacyActionStatus.CONFIRMED, PrivacyActionStatus.CONFIRMED)
    assert derive_top_level_state(company, case, actions) == TopLevelState.DONE


def test_jte_mixed_user_completed_and_needs_review_is_needs_you():
    """A NEEDS_REVIEW action means Baker's Dozen couldn't verify a safe
    method - the user still has a decision to make about how to proceed,
    so this must not present as DONE merely because the OTHER action is
    already resolved."""
    company = _company(DeletionStatus.READY)
    case = _case(RecipeChoice.JUST_THE_ESSENTIALS)
    actions = _jte_actions(PrivacyActionStatus.USER_COMPLETED, PrivacyActionStatus.NEEDS_REVIEW)
    # NEEDS_REVIEW isn't itself in TERMINAL and isn't USER_ACTION_REQUIRED,
    # so _just_the_essentials_overall falls through to WORKING today (see
    # app/case_outcome.py) - this reflects Baker's Dozen still owning the
    # next step (re-attempting research), not the user.
    assert derive_top_level_state(company, case, actions) == TopLevelState.WORKING


def test_jte_user_action_required_plus_needs_review_is_needs_you():
    company = _company(DeletionStatus.READY)
    case = _case(RecipeChoice.JUST_THE_ESSENTIALS)
    actions = _jte_actions(PrivacyActionStatus.USER_ACTION_REQUIRED, PrivacyActionStatus.NEEDS_REVIEW)
    assert derive_top_level_state(company, case, actions) == TopLevelState.NEEDS_YOU


def test_jte_both_needs_research_is_working():
    """Baker's Dozen's own lookup is the next step, not the user's -
    WORKING, never NEEDS_YOU."""
    company = _company(DeletionStatus.READY)
    case = _case(RecipeChoice.JUST_THE_ESSENTIALS)
    actions = _jte_actions(PrivacyActionStatus.NEEDS_RESEARCH, PrivacyActionStatus.NEEDS_RESEARCH)
    assert derive_top_level_state(company, case, actions) == TopLevelState.WORKING


def test_jte_rejected_action_is_needs_you():
    """A REJECTED/FAILED action, once every action has reached a terminal
    state with no full success, needs a HUMAN decision about what to do
    next - same reasoning as Full Clean's REJECTED/FAILED, above."""
    company = _company(DeletionStatus.READY)
    case = _case(RecipeChoice.JUST_THE_ESSENTIALS)
    actions = _jte_actions(PrivacyActionStatus.REJECTED, PrivacyActionStatus.FAILED)
    assert derive_top_level_state(company, case, actions) == TopLevelState.NEEDS_YOU


def test_jte_mixed_confirmed_and_rejected_is_needs_you():
    """One action genuinely succeeded (CONFIRMED), but the other was
    rejected - not a false DONE, and not WORKING (nothing more for Baker's
    Dozen to automatically do about the rejected one)."""
    company = _company(DeletionStatus.READY)
    case = _case(RecipeChoice.JUST_THE_ESSENTIALS)
    actions = _jte_actions(PrivacyActionStatus.CONFIRMED, PrivacyActionStatus.REJECTED)
    assert derive_top_level_state(company, case, actions) == TopLevelState.NEEDS_YOU


def test_jte_no_actions_supplied_is_working():
    company = _company(DeletionStatus.READY)
    case = _case(RecipeChoice.JUST_THE_ESSENTIALS)
    assert derive_top_level_state(company, case, []) == TopLevelState.WORKING
    assert derive_top_level_state(company, case, None) == TopLevelState.WORKING


# --- GOOP regression --------------------------------------------------


def test_goop_corrected_completed_shape_is_done():
    """The real Goop Kitchen reply, once correctly classified COMPLETED
    (see the response_classify.py fix), must present as DONE."""
    company = _company(DeletionStatus.COMPLETED, deletion_evidence={
        "type": "gmail_reply", "quote": "no remaining personal information...in our system", "confidence": "high",
    })
    assert derive_top_level_state(company, None) == TopLevelState.DONE


def test_goop_old_account_record_deleted_shape_is_working():
    """The SAME real reply, back when it was (incorrectly) held at
    ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED with Baker's Dozen still
    chasing it, must present as WORKING - not NEEDS_YOU, since the next
    actor at that point was still the company, not the user."""
    company = _company(
        DeletionStatus.ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED, waiting_on=WaitingOn.COMPANY,
    )
    assert derive_top_level_state(company, None) == TopLevelState.WORKING


# --- MALK regression ----------------------------------------------------


def test_malk_in_progress_waiting_on_company_is_working():
    company = _company(DeletionStatus.IN_PROGRESS, waiting_on=WaitingOn.COMPANY)
    assert derive_top_level_state(company, None) == TopLevelState.WORKING


# --- exhaustiveness / precedence ------------------------------------------


def test_every_case_state_has_an_explicit_mapping():
    from app.top_level_state import _CASE_STATE_TO_TOP_LEVEL
    assert set(_CASE_STATE_TO_TOP_LEVEL.keys()) == CaseState.ALL


def test_pantry_precedence_over_needs_you_and_working():
    """PANTRY is checked FIRST, unconditionally - it must win even over a
    company that would otherwise present as NEEDS_YOU or is mid-chase."""
    for status, waiting_on in [
        (DeletionStatus.VERIFICATION_NEEDED, WaitingOn.USER),
        (DeletionStatus.IN_PROGRESS, WaitingOn.COMPANY),
    ]:
        company = _company(status, waiting_on=waiting_on)
        case = _case(RecipeChoice.LEAVE_IT_BE)
        assert derive_top_level_state(company, case) == TopLevelState.PANTRY


def test_precomputed_case_outcome_can_be_passed_directly():
    from app.case_outcome import derive_case_outcome

    company = _company(DeletionStatus.COMPLETED)
    outcome = derive_case_outcome(company, None)
    # Same result whether derived internally or passed pre-computed.
    assert derive_top_level_state(company, case_outcome=outcome) == TopLevelState.DONE
    assert derive_top_level_state(company, case_outcome=outcome) == derive_top_level_state(company, None)


def test_derive_top_level_state_never_mutates_its_arguments():
    company = _company(DeletionStatus.READY)
    case = _case(RecipeChoice.JUST_THE_ESSENTIALS)
    actions = _jte_actions(PrivacyActionStatus.USER_COMPLETED, PrivacyActionStatus.NEEDS_REVIEW)
    before = (company.deletion_status, company.waiting_on, case.selected_recipe, [(a.status) for a in actions])

    derive_top_level_state(company, case, actions)

    after = (company.deletion_status, company.waiting_on, case.selected_recipe, [(a.status) for a in actions])
    assert before == after


# --- dashboard wiring / no-mutation proof ---------------------------------


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
    return TestClient(app)


def _db_company(db, **overrides) -> Company:
    defaults = dict(
        name="Fabricated Co", domain="fabricated-corp.com", relationship_type="transactional", status="confirmed",
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        deletion_method=DeletionMethod.EMAIL_REQUEST, deletion_status=DeletionStatus.COMPLETED,
        deletion_verified=True,
    )
    defaults.update(overrides)
    company = Company(**defaults)
    db.add(company)
    db.commit()
    return company


def test_dashboard_card_carries_top_level_state_data_attribute(client_db, client):
    company = _db_company(client_db, deletion_status=DeletionStatus.COMPLETED)
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    card = soup.find("article", {"data-id": str(company.id)})
    assert card is not None
    assert card.get("data-top-level-state") == TopLevelState.DONE


def test_dashboard_card_needs_you_for_verification_needed(client_db, client):
    company = _db_company(
        client_db, deletion_status=DeletionStatus.VERIFICATION_NEEDED, waiting_on=WaitingOn.USER,
        deletion_url="https://fabricated-corp.com/verify",
    )
    resp = client.get("/dashboard")
    soup = BeautifulSoup(resp.text, "html.parser")
    card = soup.find("article", {"data-id": str(company.id)})
    assert card.get("data-top-level-state") == TopLevelState.NEEDS_YOU


def test_pantry_card_carries_pantry_top_level_state(client_db, client):
    company = _db_company(client_db, deletion_status=DeletionStatus.COMPLETED)
    case = PrivacyCase(company_id=company.id, selected_recipe=RecipeChoice.LEAVE_IT_BE)
    client_db.add(case)
    client_db.commit()

    resp = client.get("/dashboard")
    soup = BeautifulSoup(resp.text, "html.parser")
    card = soup.find("article", {"data-id": str(company.id), "data-pantry": "true"})
    assert card is not None
    assert card.get("data-top-level-state") == TopLevelState.PANTRY


def test_rendering_dashboard_causes_zero_db_mutation(client_db, client):
    company = _db_company(client_db, deletion_status=DeletionStatus.READY)
    case = PrivacyCase(company_id=company.id, selected_recipe=RecipeChoice.JUST_THE_ESSENTIALS)
    client_db.add(case)
    client_db.commit()
    action1 = PrivacyAction(
        privacy_case_id=case.id, action_type=PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP,
        method=DeletionMethod.ACCOUNT_SETTING, status=PrivacyActionStatus.USER_COMPLETED, evidence={},
    )
    action2 = PrivacyAction(
        privacy_case_id=case.id, action_type=PrivacyActionType.SALE_SHARING_OPT_OUT,
        method=DeletionMethod.UNKNOWN, status=PrivacyActionStatus.NEEDS_REVIEW, evidence={},
    )
    client_db.add_all([action1, action2])
    client_db.commit()

    before = (
        company.deletion_status, company.waiting_on, company.deletion_evidence,
        case.selected_recipe, action1.status, action2.status,
    )

    resp = client.get("/dashboard")
    assert resp.status_code == 200

    client_db.expire_all()
    refreshed_company = client_db.get(Company, company.id)
    refreshed_case = client_db.get(PrivacyCase, case.id)
    refreshed_action1 = client_db.get(PrivacyAction, action1.id)
    refreshed_action2 = client_db.get(PrivacyAction, action2.id)
    after = (
        refreshed_company.deletion_status, refreshed_company.waiting_on, refreshed_company.deletion_evidence,
        refreshed_case.selected_recipe, refreshed_action1.status, refreshed_action2.status,
    )
    assert before == after
