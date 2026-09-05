"""Cleanup Recipes milestone, commit #3: recipe-selection persistence +
append-only RECIPE_SELECTED audit (app/privacy_case.py). No UI, no routes,
no execution, no Gmail, no chase behavior - selecting a recipe is USER
INTENT ONLY. Fabricated companies for all tests.
"""
import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import migrations
from app.db import Base
from app.deletion_constants import DeletionStatus, EventSource, EventType, RecipeChoice
from app.models import Company, DeletionEvent, PrivacyCase
from app.privacy_case import InvalidRecipeChoiceError, select_recipe


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _company(db, domain="fabricated.example", **overrides) -> Company:
    defaults = dict(
        name="Fabricated Co", domain=domain, relationship_type="transactional", status="confirmed",
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
    )
    defaults.update(overrides)
    company = Company(**defaults)
    db.add(company)
    db.commit()
    return company


def _case(db, company: Company) -> PrivacyCase:
    case = PrivacyCase(company_id=company.id, selected_recipe=None, recipe_selected_at=None)
    db.add(case)
    db.commit()
    return case


# --- 1-3: each recipe persists -------------------------------------------

@pytest.mark.parametrize("recipe", sorted(RecipeChoice.ALL))
def test_recipe_selection_persists(db, recipe):
    company = _company(db)
    case = _case(db, company)

    select_recipe(db, case, recipe)

    db.expire_all()
    fetched = db.query(PrivacyCase).filter(PrivacyCase.id == case.id).one()
    assert fetched.selected_recipe == recipe


# --- 4: recipe_selected_at is set -----------------------------------------

def test_recipe_selected_at_is_set(db):
    company = _company(db)
    case = _case(db, company)
    assert case.recipe_selected_at is None

    before = datetime.datetime.utcnow()
    select_recipe(db, case, RecipeChoice.FULL_CLEAN)
    after = datetime.datetime.utcnow()

    db.expire_all()
    fetched = db.query(PrivacyCase).filter(PrivacyCase.id == case.id).one()
    assert fetched.recipe_selected_at is not None
    assert before <= fetched.recipe_selected_at <= after


# --- 5-6: RECIPE_SELECTED event created, with correct fields --------------

def test_recipe_selected_event_created_with_correct_fields(db):
    company = _company(db)
    case = _case(db, company)

    select_recipe(db, case, RecipeChoice.JUST_THE_ESSENTIALS, source=EventSource.USER)

    events = db.query(DeletionEvent).filter(DeletionEvent.event_type == EventType.RECIPE_SELECTED).all()
    assert len(events) == 1
    event = events[0]
    assert event.company_id == company.id
    assert event.privacy_case_id == case.id
    assert event.event_type == EventType.RECIPE_SELECTED
    assert event.source == EventSource.USER
    assert event.evidence["selected_recipe"] == RecipeChoice.JUST_THE_ESSENTIALS
    assert event.evidence["previous_recipe"] is None


def test_default_source_is_user():
    """Recipe selection is always an explicit human choice - never
    system-inferred - so the default source is EventSource.USER, unlike
    record_event's own default of EventSource.SYSTEM."""
    import inspect

    from app.privacy_case import select_recipe as fn
    assert inspect.signature(fn).parameters["source"].default == EventSource.USER


# --- 7: re-selection updates current value, appends history, keeps prior event unchanged ---

def test_reselection_updates_value_and_appends_audit_history(db):
    company = _company(db)
    case = _case(db, company)

    select_recipe(db, case, RecipeChoice.FULL_CLEAN)
    db.expire_all()
    first_event = db.query(DeletionEvent).filter(DeletionEvent.event_type == EventType.RECIPE_SELECTED).one()
    first_event_snapshot = (
        first_event.id, first_event.company_id, first_event.privacy_case_id,
        first_event.evidence, first_event.occurred_at,
    )

    case = db.query(PrivacyCase).filter(PrivacyCase.id == case.id).one()
    select_recipe(db, case, RecipeChoice.JUST_THE_ESSENTIALS)

    db.expire_all()
    fetched_case = db.query(PrivacyCase).filter(PrivacyCase.id == case.id).one()
    assert fetched_case.selected_recipe == RecipeChoice.JUST_THE_ESSENTIALS

    events = (
        db.query(DeletionEvent)
        .filter(DeletionEvent.event_type == EventType.RECIPE_SELECTED)
        .order_by(DeletionEvent.id)
        .all()
    )
    assert len(events) == 2
    assert events[0].evidence == {"selected_recipe": RecipeChoice.FULL_CLEAN, "previous_recipe": None}
    assert events[1].evidence == {
        "selected_recipe": RecipeChoice.JUST_THE_ESSENTIALS, "previous_recipe": RecipeChoice.FULL_CLEAN,
    }
    # The prior event is untouched - never mutated or deleted.
    unchanged = (
        events[0].id, events[0].company_id, events[0].privacy_case_id,
        events[0].evidence, events[0].occurred_at,
    )
    assert unchanged == first_event_snapshot


# --- 8-9: invalid recipe rejected, no mutation, no event ------------------

@pytest.mark.parametrize("bad_recipe", [None, "NOT_A_RECIPE", "full_clean", "Full_Clean", "FOURTH_RECIPE"])
def test_invalid_recipe_rejected(db, bad_recipe):
    company = _company(db)
    case = _case(db, company)

    with pytest.raises(InvalidRecipeChoiceError):
        select_recipe(db, case, bad_recipe)

    db.expire_all()
    fetched = db.query(PrivacyCase).filter(PrivacyCase.id == case.id).one()
    assert fetched.selected_recipe is None
    assert fetched.recipe_selected_at is None
    assert db.query(DeletionEvent).filter(DeletionEvent.event_type == EventType.RECIPE_SELECTED).count() == 0


# --- 10-11: no Company mutation of any kind --------------------------------

def test_selection_does_not_mutate_company_fields(db):
    company = _company(
        db,
        deletion_status=DeletionStatus.SUBMITTED, waiting_on="COMPANY",
        deletion_thread_id="thread-123", deletion_last_response_message_id="msg-456",
        next_followup_at=datetime.datetime(2026, 1, 1), followup_attempt=2,
    )
    case = _case(db, company)

    snapshot_cols = (
        "status", "deletion_status", "deletion_evidence", "waiting_on", "next_followup_at",
        "followup_attempt", "last_followup_at", "followup_locked_at", "followups_paused",
        "deletion_thread_id", "deletion_last_response_message_id", "deletion_requested_at",
        "deletion_completed_at", "deletion_method", "deletion_verified",
    )
    before = {col: getattr(company, col) for col in snapshot_cols}

    select_recipe(db, case, RecipeChoice.LEAVE_IT_BE)

    db.expire_all()
    fetched_company = db.query(Company).filter(Company.id == company.id).one()
    after = {col: getattr(fetched_company, col) for col in snapshot_cols}
    assert after == before


# --- 12: no Gmail/chase function invoked (structural guarantee) -----------

def test_privacy_case_module_imports_no_gmail_or_chase_code():
    """A structural guarantee, not just a behavioral one: this module must
    never even IMPORT anything capable of sending Gmail or touching chase
    state, so a future edit can't accidentally wire either in without this
    test catching it. Parses actual import statements (not a raw text
    search) so the module's own docstring is free to name these modules
    when explaining what it deliberately avoids."""
    import ast

    import app.privacy_case as mod

    tree = ast.parse(open(mod.__file__).read())
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    forbidden = {"google_oauth", "chase_engine", "deletion_engine"}
    hit = forbidden & {m.rsplit(".", 1)[-1] for m in imported_modules}
    assert not hit, f"app/privacy_case.py must never import {hit}"


# --- 13: legacy case with selected_recipe=None stays None until explicit call ---

def test_legacy_case_stays_none_until_explicit_selection(db):
    company = _company(db)
    case = _case(db, company)  # simulates commit #1's backfill: selected_recipe=None
    assert case.selected_recipe is None

    db.expire_all()
    fetched = db.query(PrivacyCase).filter(PrivacyCase.id == case.id).one()
    assert fetched.selected_recipe is None  # nothing implicitly selected it


# --- 14: backfill still never creates RECIPE_SELECTED events --------------

def test_backfill_still_never_creates_recipe_selected_events(tmp_path):
    from sqlalchemy import create_engine as _create_engine

    db_path = tmp_path / "backfill.db"
    engine = _create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    session.add(Company(
        name="Fabricated Co", domain="fabricated-backfill.example", relationship_type="transactional",
        status="confirmed", confidence="high", evidence_count=1, evidence_types=[], example_subjects=[],
        detection_reasons=[], first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
    ))
    session.commit()
    session.close()

    migrations.migrate(engine, str(db_path))

    session = Session()
    assert session.query(DeletionEvent).filter(DeletionEvent.event_type == EventType.RECIPE_SELECTED).count() == 0
    case = session.query(PrivacyCase).one()
    assert case.selected_recipe is None
    session.close()


# --- 15: same-value re-selection is an idempotent no-op --------------------

def test_same_value_reselection_is_idempotent_noop(db):
    company = _company(db)
    case = _case(db, company)

    select_recipe(db, case, RecipeChoice.FULL_CLEAN)
    db.expire_all()
    case = db.query(PrivacyCase).filter(PrivacyCase.id == case.id).one()
    first_selected_at = case.recipe_selected_at

    select_recipe(db, case, RecipeChoice.FULL_CLEAN)  # same value again

    db.expire_all()
    fetched = db.query(PrivacyCase).filter(PrivacyCase.id == case.id).one()
    assert fetched.selected_recipe == RecipeChoice.FULL_CLEAN
    assert fetched.recipe_selected_at == first_selected_at  # unchanged - no false timestamp update

    events = db.query(DeletionEvent).filter(DeletionEvent.event_type == EventType.RECIPE_SELECTED).all()
    assert len(events) == 1  # no duplicate event


def test_same_value_reselection_returns_the_case_unchanged(db):
    company = _company(db)
    case = _case(db, company)
    select_recipe(db, case, RecipeChoice.LEAVE_IT_BE)

    returned = select_recipe(db, case, RecipeChoice.LEAVE_IT_BE)
    assert returned is case
    assert returned.selected_recipe == RecipeChoice.LEAVE_IT_BE
