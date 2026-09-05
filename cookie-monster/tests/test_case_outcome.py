"""Cleanup Recipes milestone, commit #2: pure PrivacyCase outcome
projection (app/case_outcome.py). No DB session, no routes, no execution -
see that module's docstring for the full architecture. Fabricated
companies for all generic tests; MALK/Goop appear only as regression
fixtures reproducing their real historical/live shape, never as
production branches.
"""
import datetime

import pytest

from app.case_outcome import CaseOutcome, derive_case_outcome
from app.deletion_constants import (
    AccountOutcome,
    CaseState,
    DeletionStatus,
    NonessentialTrackingOutcome,
    OptOutOutcome,
    PersonalDataOutcome,
    RecipeChoice,
    RetentionOutcome,
    WaitingOn,
)
from app.models import Company, PrivacyCase

# --- fixtures -----------------------------------------------------------


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


# The five recipe-selection variants every meaningful status must be
# exercised under, per the milestone's testing requirements. "no_case"
# means derive_case_outcome is called with privacy_case=None entirely
# (predates PrivacyCase existing at all); "legacy_none" means a
# PrivacyCase row exists but selected_recipe is None (backfilled, no
# explicit selection ever made) - both must derive identical
# account/personal_data/retention/overall to a recipe-bearing case in the
# same deletion_status.
_RECIPE_VARIANTS = {
    "no_case": None,
    "legacy_none": _case(None),
    "full_clean": _case(RecipeChoice.FULL_CLEAN),
    "just_the_essentials": _case(RecipeChoice.JUST_THE_ESSENTIALS),
    "leave_it_be": _case(RecipeChoice.LEAVE_IT_BE),
}


# --- exhaustive per-status account/personal_data/retention/overall table ---
# (variant, expected) pairs are irrelevant here - recipe selection must
# NEVER change these evidence-derived axes, so every variant expects the
# exact same account/personal_data/retention/overall for a given status.
# waiting_on is only meaningful for UNKNOWN_RESPONSE (see below).
_EXPECTED = {
    DeletionStatus.NOT_STARTED: (AccountOutcome.UNKNOWN, PersonalDataOutcome.UNKNOWN, RetentionOutcome.NONE_DISCLOSED, CaseState.WORKING),
    DeletionStatus.METHOD_LOOKUP: (AccountOutcome.UNKNOWN, PersonalDataOutcome.UNKNOWN, RetentionOutcome.NONE_DISCLOSED, CaseState.WORKING),
    DeletionStatus.READY: (AccountOutcome.UNKNOWN, PersonalDataOutcome.UNKNOWN, RetentionOutcome.NONE_DISCLOSED, CaseState.WORKING),
    DeletionStatus.CONFIRMATION_REQUIRED: (AccountOutcome.UNKNOWN, PersonalDataOutcome.UNKNOWN, RetentionOutcome.NONE_DISCLOSED, CaseState.NEEDS_USER),
    DeletionStatus.SUBMITTING: (AccountOutcome.UNKNOWN, PersonalDataOutcome.DELETION_REQUESTED, RetentionOutcome.NONE_DISCLOSED, CaseState.WORKING),
    DeletionStatus.SUBMITTED: (AccountOutcome.UNKNOWN, PersonalDataOutcome.DELETION_REQUESTED, RetentionOutcome.NONE_DISCLOSED, CaseState.WORKING),
    DeletionStatus.IN_PROGRESS: (AccountOutcome.UNKNOWN, PersonalDataOutcome.DELETION_REQUESTED, RetentionOutcome.NONE_DISCLOSED, CaseState.WORKING),
    DeletionStatus.VERIFICATION_NEEDED: (AccountOutcome.UNKNOWN, PersonalDataOutcome.DELETION_REQUESTED, RetentionOutcome.NONE_DISCLOSED, CaseState.NEEDS_USER),
    DeletionStatus.MORE_INFO_REQUIRED: (AccountOutcome.UNKNOWN, PersonalDataOutcome.DELETION_REQUESTED, RetentionOutcome.NONE_DISCLOSED, CaseState.NEEDS_USER),
    DeletionStatus.USER_ACTION_REQUIRED: (AccountOutcome.UNKNOWN, PersonalDataOutcome.UNKNOWN, RetentionOutcome.NONE_DISCLOSED, CaseState.NEEDS_USER),
    DeletionStatus.COMPLETED: (AccountOutcome.UNKNOWN, PersonalDataOutcome.DELETION_CONFIRMED, RetentionOutcome.NONE_DISCLOSED, CaseState.RESOLVED),
    DeletionStatus.REJECTED: (AccountOutcome.UNKNOWN, PersonalDataOutcome.RETAINED, RetentionOutcome.NONE_DISCLOSED, CaseState.UNRESOLVED),
    DeletionStatus.FAILED: (AccountOutcome.UNKNOWN, PersonalDataOutcome.UNKNOWN, RetentionOutcome.NONE_DISCLOSED, CaseState.UNRESOLVED),
    DeletionStatus.UNKNOWN: (AccountOutcome.UNKNOWN, PersonalDataOutcome.UNKNOWN, RetentionOutcome.NONE_DISCLOSED, CaseState.WORKING),
    DeletionStatus.NO_METHOD_FOUND: (AccountOutcome.UNKNOWN, PersonalDataOutcome.UNKNOWN, RetentionOutcome.NONE_DISCLOSED, CaseState.WORKING),
    DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED: (AccountOutcome.CLOSED, PersonalDataOutcome.DELETION_REQUESTED, RetentionOutcome.NONE_DISCLOSED, CaseState.WORKING),
    DeletionStatus.ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED: (AccountOutcome.UNKNOWN, PersonalDataOutcome.DELETION_REQUESTED, RetentionOutcome.NONE_DISCLOSED, CaseState.WORKING),
    # UNKNOWN_RESPONSE's overall depends on waiting_on - tested separately below.
    DeletionStatus.UNKNOWN_RESPONSE: (AccountOutcome.UNKNOWN, PersonalDataOutcome.DELETION_REQUESTED, RetentionOutcome.NONE_DISCLOSED, CaseState.WORKING),
}


def test_expected_table_covers_every_current_deletion_status():
    """Guards the test file itself against drift, independent of the
    production table's own self-check in app/case_outcome.py."""
    assert set(_EXPECTED.keys()) == DeletionStatus.ALL


@pytest.mark.parametrize("status", sorted(DeletionStatus.ALL))
@pytest.mark.parametrize("variant_name", sorted(_RECIPE_VARIANTS.keys()))
def test_status_outcome_is_independent_of_recipe_selection(status, variant_name):
    """The core evidence-first guarantee: for every status, every one of
    the five recipe-selection variants derives the identical
    account/personal_data/retention, and (outside UNKNOWN_RESPONSE, whose
    overall depends on waiting_on, not recipe) the identical overall too."""
    company = _company(status, waiting_on=None)
    outcome = derive_case_outcome(company, _RECIPE_VARIANTS[variant_name])

    expected_account, expected_personal_data, expected_retention, expected_overall = _EXPECTED[status]
    assert outcome.account == expected_account
    assert outcome.personal_data == expected_personal_data
    assert outcome.retention == expected_retention
    assert outcome.overall == expected_overall


def test_derive_case_outcome_table_is_exhaustive_over_deletion_status():
    """Production self-check mirrors the test-file self-check - both must
    agree the table is exhaustive."""
    from app.case_outcome import _STATUS_OUTCOME_TABLE
    assert set(_STATUS_OUTCOME_TABLE.keys()) == DeletionStatus.ALL


# --- UNKNOWN_RESPONSE: the one status whose overall depends on waiting_on ---

def test_unknown_response_needs_user_when_waiting_on_user():
    company = _company(DeletionStatus.UNKNOWN_RESPONSE, waiting_on=WaitingOn.USER)
    outcome = derive_case_outcome(company)
    assert outcome.overall == CaseState.NEEDS_USER
    assert outcome.personal_data == PersonalDataOutcome.DELETION_REQUESTED


def test_unknown_response_stays_working_when_waiting_on_company():
    company = _company(DeletionStatus.UNKNOWN_RESPONSE, waiting_on=WaitingOn.COMPANY)
    outcome = derive_case_outcome(company)
    assert outcome.overall == CaseState.WORKING


def test_unknown_response_defaults_to_working_when_waiting_on_unset():
    company = _company(DeletionStatus.UNKNOWN_RESPONSE, waiting_on=None)
    outcome = derive_case_outcome(company)
    assert outcome.overall == CaseState.WORKING


# --- Pantry / recipe-choice rules ---------------------------------------

def test_is_pantry_true_only_for_leave_it_be():
    company = _company(DeletionStatus.NOT_STARTED)
    for name, case in _RECIPE_VARIANTS.items():
        outcome = derive_case_outcome(company, case)
        expected = name == "leave_it_be"
        assert outcome.is_pantry is expected, f"variant {name} expected is_pantry={expected}"


def test_recipe_selection_alone_never_yields_deletion_confirmation():
    """A company with zero evidence (NOT_STARTED) must never derive
    DELETION_CONFIRMED regardless of which recipe was chosen - recipe
    choice alone is explicitly forbidden as a source of DELETION_CONFIRMED."""
    company = _company(DeletionStatus.NOT_STARTED)
    for case in _RECIPE_VARIANTS.values():
        outcome = derive_case_outcome(company, case)
        assert outcome.personal_data != PersonalDataOutcome.DELETION_CONFIRMED


def test_leave_it_be_does_not_erase_prior_evidence():
    """LEAVE_IT_BE must not force RESOLVED/UNKNOWN over real evidence - a
    company already COMPLETED before the user chose LEAVE_IT_BE keeps
    deriving COMPLETED's real outcome, with is_pantry layered on top."""
    company = _company(DeletionStatus.COMPLETED)
    outcome = derive_case_outcome(company, _case(RecipeChoice.LEAVE_IT_BE))
    assert outcome.personal_data == PersonalDataOutcome.DELETION_CONFIRMED
    assert outcome.overall == CaseState.RESOLVED
    assert outcome.is_pantry is True


def test_leave_it_be_on_fresh_company_does_not_falsely_resolve():
    """A brand-new company with no evidence that the user immediately
    marks LEAVE_IT_BE must not be reported as RESOLVED (that would falsely
    claim a privacy result was achieved) - overall stays evidence-derived
    (WORKING), and the "don't pursue further" fact lives only in is_pantry."""
    company = _company(DeletionStatus.NOT_STARTED)
    outcome = derive_case_outcome(company, _case(RecipeChoice.LEAVE_IT_BE))
    assert outcome.overall != CaseState.RESOLVED
    assert outcome.overall == CaseState.WORKING
    assert outcome.is_pantry is True


def test_pantry_working_must_not_be_read_as_active_work():
    """PANTRY / OVERALL INVARIANT (see case_outcome.py's module docstring):
    is_pantry is a USER DISPOSITION axis; overall is an EVIDENCE/PRIVACY-
    WORK projection. They are independent and must be read together.

    This fixture is the exact shape a future "active work" dashboard
    filter/query must not get wrong: a Pantry case with no privacy
    evidence legitimately derives overall=WORKING (nothing has resolved)
    at the same time as is_pantry=True (the user chose not to pursue this
    further). WORKING here means only "no privacy result exists yet" -
    never "Baker's Dozen is actively working this case". Any future
    active-work surface MUST check is_pantry and exclude Pantry cases
    BEFORE branching on overall - branching on overall alone would
    silently surface every un-pursued Pantry case as active work.
    No dashboard filtering exists yet; this test only pins the
    CaseOutcome shape such a filter must handle correctly."""
    pantry_case = _company(DeletionStatus.NOT_STARTED)
    outcome = derive_case_outcome(pantry_case, _case(RecipeChoice.LEAVE_IT_BE))

    assert outcome.overall == CaseState.WORKING
    assert outcome.is_pantry is True

    # The one correct way to answer "is this case active work": check
    # is_pantry first. A consumer that skips this and treats
    # overall == WORKING as sufficient proof of active work is wrong -
    # this is exactly what that mistake would look like.
    naive_active_work = outcome.overall == CaseState.WORKING
    correct_active_work = (not outcome.is_pantry) and outcome.overall == CaseState.WORKING
    assert naive_active_work is True  # the trap: overall alone says "active"
    assert correct_active_work is False  # is_pantry correctly excludes it


# --- JUST_THE_ESSENTIALS / FULL_CLEAN axis applicability ------------------

def test_full_clean_leaves_recipe_specific_axes_inapplicable():
    company = _company(DeletionStatus.NOT_STARTED)
    outcome = derive_case_outcome(company, _case(RecipeChoice.FULL_CLEAN))
    assert outcome.nonessential_tracking is None
    assert outcome.opt_out is None


def test_no_recipe_selected_leaves_recipe_specific_axes_inapplicable():
    company = _company(DeletionStatus.NOT_STARTED)
    outcome = derive_case_outcome(company, _case(None))
    assert outcome.nonessential_tracking is None
    assert outcome.opt_out is None
    outcome_no_case = derive_case_outcome(company, None)
    assert outcome_no_case.nonessential_tracking is None
    assert outcome_no_case.opt_out is None


def test_leave_it_be_leaves_recipe_specific_axes_inapplicable():
    company = _company(DeletionStatus.NOT_STARTED)
    outcome = derive_case_outcome(company, _case(RecipeChoice.LEAVE_IT_BE))
    assert outcome.nonessential_tracking is None
    assert outcome.opt_out is None


def test_just_the_essentials_starts_conservative_and_unresolved():
    """No execution engine exists for this recipe yet - must never claim
    CONFIRMED from the recipe choice alone."""
    company = _company(DeletionStatus.NOT_STARTED)
    outcome = derive_case_outcome(company, _case(RecipeChoice.JUST_THE_ESSENTIALS))
    assert outcome.nonessential_tracking == NonessentialTrackingOutcome.UNRESOLVED
    assert outcome.opt_out == OptOutOutcome.UNKNOWN
    assert outcome.nonessential_tracking != NonessentialTrackingOutcome.CONFIRMED
    assert outcome.opt_out != OptOutOutcome.CONFIRMED


# --- evidence-first safety rule: forbidden paths to DELETION_CONFIRMED ---

@pytest.mark.parametrize("status", sorted(DeletionStatus.ALL - {DeletionStatus.COMPLETED}))
def test_only_completed_can_ever_yield_deletion_confirmed(status):
    for case in _RECIPE_VARIANTS.values():
        outcome = derive_case_outcome(_company(status), case)
        assert outcome.personal_data != PersonalDataOutcome.DELETION_CONFIRMED


def test_account_closed_never_becomes_personal_data_confirmed():
    company = _company(DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED, waiting_on=WaitingOn.COMPANY)
    for case in _RECIPE_VARIANTS.values():
        outcome = derive_case_outcome(company, case)
        assert outcome.personal_data != PersonalDataOutcome.DELETION_CONFIRMED
        assert outcome.account == AccountOutcome.CLOSED


def test_account_record_deleted_never_becomes_personal_data_confirmed():
    """Account/account-record deletion != confirmed personal-data
    deletion: the evidence establishes the account record was deleted, but
    not that any specific subset of the user's broader personal
    information was - PARTIALLY_DELETED would overclaim that, so this
    reads exactly like an outstanding, unconfirmed request
    (DELETION_REQUESTED), the same as ACCOUNT_CLOSED_DATA_UNVERIFIED."""
    company = _company(DeletionStatus.ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED, waiting_on=WaitingOn.COMPANY)
    for case in _RECIPE_VARIANTS.values():
        outcome = derive_case_outcome(company, case)
        assert outcome.personal_data == PersonalDataOutcome.DELETION_REQUESTED
        assert outcome.personal_data != PersonalDataOutcome.DELETION_CONFIRMED
        assert outcome.personal_data != PersonalDataOutcome.PARTIALLY_DELETED
        assert outcome.personal_data != PersonalDataOutcome.RETAINED


def test_generic_acknowledgment_and_in_progress_remain_non_terminal():
    for status in (DeletionStatus.IN_PROGRESS, DeletionStatus.SUBMITTED, DeletionStatus.UNKNOWN):
        outcome = derive_case_outcome(_company(status))
        assert outcome.overall not in (CaseState.RESOLVED, CaseState.UNRESOLVED)


def test_completed_stays_completed():
    outcome = derive_case_outcome(_company(DeletionStatus.COMPLETED))
    assert outcome.personal_data == PersonalDataOutcome.DELETION_CONFIRMED
    assert outcome.overall == CaseState.RESOLVED


def test_user_required_states_map_to_needs_user():
    for status in (
        DeletionStatus.VERIFICATION_NEEDED, DeletionStatus.MORE_INFO_REQUIRED, DeletionStatus.USER_ACTION_REQUIRED,
    ):
        outcome = derive_case_outcome(_company(status))
        assert outcome.overall == CaseState.NEEDS_USER


def test_technical_failure_does_not_fabricate_a_privacy_outcome():
    """DeletionStatus.FAILED is reserved for a technical/tracking failure
    (see deletion_response_tracker.py) - it must never be read as a
    company privacy response of any kind."""
    outcome = derive_case_outcome(_company(DeletionStatus.FAILED))
    assert outcome.personal_data == PersonalDataOutcome.UNKNOWN
    assert outcome.account == AccountOutcome.UNKNOWN
    assert outcome.overall == CaseState.UNRESOLVED


# --- purity / determinism -------------------------------------------------

def test_derive_case_outcome_does_not_mutate_company():
    company = _company(DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED, waiting_on=WaitingOn.COMPANY)
    snapshot = (company.deletion_status, company.deletion_evidence, company.waiting_on, company.name, company.domain)
    derive_case_outcome(company, _case(RecipeChoice.FULL_CLEAN))
    assert (company.deletion_status, company.deletion_evidence, company.waiting_on, company.name, company.domain) == snapshot


def test_derive_case_outcome_does_not_mutate_privacy_case():
    case = _case(RecipeChoice.JUST_THE_ESSENTIALS)
    snapshot = (case.company_id, case.selected_recipe, case.recipe_selected_at)
    derive_case_outcome(_company(DeletionStatus.NOT_STARTED), case)
    assert (case.company_id, case.selected_recipe, case.recipe_selected_at) == snapshot


def test_repeated_invocation_returns_identical_output():
    company = _company(DeletionStatus.COMPLETED)
    case = _case(RecipeChoice.FULL_CLEAN)
    first = derive_case_outcome(company, case)
    second = derive_case_outcome(company, case)
    assert first == second
    assert isinstance(first, CaseOutcome)


def test_derive_case_outcome_requires_no_db_or_session():
    """Plain, unattached ORM instances - never added to a Session, never
    committed - are sufficient. If this test needed a DB fixture, that
    would itself be evidence the function isn't pure."""
    company = _company(DeletionStatus.SUBMITTED, waiting_on=WaitingOn.COMPANY)
    outcome = derive_case_outcome(company)
    assert isinstance(outcome, CaseOutcome)


# --- regression fixtures: real historical shapes -------------------------

def test_goop_shape_no_false_personal_data_completion():
    """Goop Kitchen's real live reply: 'the account associated with
    <email> ... has been deactivated' resolved to
    ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED with waiting_on=COMPANY (see
    the reliability fix verified live in commit 953b69b) - selected_recipe
    is None (this case predates the recipe picker). The account/account-
    record deletion claim does NOT establish that any specific subset of
    the user's broader personal data was deleted, so this must read as an
    outstanding, unconfirmed request (DELETION_REQUESTED) - not
    PARTIALLY_DELETED (too generous a claim) and never DELETION_CONFIRMED."""
    goop = _company(
        DeletionStatus.ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED, waiting_on=WaitingOn.COMPANY,
        name="Goop Kitchen (fixture)", domain="goop-fixture.example",
        deletion_evidence={"type": "gmail_reply", "note": "regression fixture, not live production data"},
    )
    outcome = derive_case_outcome(goop, _case(None))
    assert outcome.personal_data == PersonalDataOutcome.DELETION_REQUESTED
    assert outcome.personal_data != PersonalDataOutcome.PARTIALLY_DELETED
    assert outcome.personal_data != PersonalDataOutcome.DELETION_CONFIRMED
    assert outcome.overall == CaseState.WORKING
    assert outcome.is_pantry is False


def test_malk_shape_no_false_completion():
    """MALK Organics's real live reply was a generic acknowledgment
    ('We have received your email...someone from our team will get back
    to you') - IN_PROGRESS, never COMPLETED. selected_recipe is None
    (predates the recipe picker)."""
    malk = _company(
        DeletionStatus.IN_PROGRESS, waiting_on=WaitingOn.COMPANY,
        name="MALK Organics (fixture)", domain="malk-fixture.example",
        deletion_evidence={"type": "gmail_reply", "note": "regression fixture, not live production data"},
    )
    outcome = derive_case_outcome(malk, _case(None))
    assert outcome.personal_data != PersonalDataOutcome.DELETION_CONFIRMED
    assert outcome.overall == CaseState.WORKING
    assert outcome.is_pantry is False
