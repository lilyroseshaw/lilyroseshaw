"""Cleanup Recipes milestone, commit #1: PrivacyCase model + RecipeChoice.

Schema/intent only - no derive_case_outcome, no CaseOutcome, no recipe-
selection routes, no UI, no PrivacyAction. See migrations.py's backfill
tests for the migration-side behavior (one PrivacyCase per existing
Company, selected_recipe always None, idempotent, no side effects on
Company/DeletionEvent). Fabricated companies only - no company-specific
production logic.
"""
import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.deletion_constants import EventType, RecipeChoice
from app.models import Company, PrivacyCase


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _company(db, domain="fabricated.example") -> Company:
    company = Company(
        name="Fabricated Co", domain=domain, relationship_type="transactional", status="confirmed",
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
    )
    db.add(company)
    db.commit()
    return company


def test_recipe_choice_has_exactly_three_members():
    assert RecipeChoice.ALL == {
        RecipeChoice.FULL_CLEAN, RecipeChoice.JUST_THE_ESSENTIALS, RecipeChoice.LEAVE_IT_BE,
    }
    assert len(RecipeChoice.ALL) == 3


def test_none_is_not_a_member_of_recipe_choice_all():
    """None means 'no Cleanup Recipe explicitly selected' - a legitimate
    state represented by PrivacyCase.selected_recipe being NULL, never a
    fourth member of RecipeChoice.ALL."""
    assert None not in RecipeChoice.ALL


def test_privacy_case_can_be_created_with_no_recipe_selected(db):
    company = _company(db)
    case = PrivacyCase(company_id=company.id, selected_recipe=None, recipe_selected_at=None)
    db.add(case)
    db.commit()

    fetched = db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
    assert fetched.selected_recipe is None
    assert fetched.recipe_selected_at is None
    assert fetched.id is not None
    assert fetched.created_at is not None


def test_privacy_case_can_hold_each_recipe_choice(db):
    for i, recipe in enumerate(sorted(RecipeChoice.ALL)):
        company = _company(db, domain=f"fabricated-{i}.example")
        case = PrivacyCase(
            company_id=company.id, selected_recipe=recipe,
            recipe_selected_at=datetime.datetime(2026, 1, 1),
        )
        db.add(case)
        db.commit()
        fetched = db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
        assert fetched.selected_recipe == recipe


def test_privacy_case_company_id_is_unique_one_to_one(db):
    company = _company(db)
    db.add(PrivacyCase(company_id=company.id, selected_recipe=None, recipe_selected_at=None))
    db.commit()

    db.add(PrivacyCase(company_id=company.id, selected_recipe=RecipeChoice.FULL_CLEAN, recipe_selected_at=None))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    # Exactly one case for this company survives the failed second insert.
    assert db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).count() == 1


def test_privacy_case_has_no_outcome_or_pantry_columns():
    """Explicit guard for the "outcomes are derived, not stored" and
    "no is_pantry column" architecture decisions - a future change that
    accidentally reintroduces a stored outcome/pantry column should fail
    this test rather than silently regress the design."""
    columns = {c.name for c in PrivacyCase.__table__.columns}
    assert columns == {"id", "company_id", "selected_recipe", "recipe_selected_at", "created_at", "updated_at"}


def test_event_type_recipe_selected_exists():
    assert EventType.RECIPE_SELECTED == "RECIPE_SELECTED"
    assert EventType.RECIPE_SELECTED in EventType.ALL


def test_privacy_action_table_does_not_exist_yet():
    """PrivacyAction is deliberately deferred to the Just the Essentials
    commit (YAGNI - no commit #1 behavior reads/writes it). Guards against
    it being added speculatively before real requirements exist."""
    assert "privacy_actions" not in Base.metadata.tables
