"""Cleanup Recipe selection - the one place explicit USER INTENT (see
RecipeChoice in deletion_constants.py) is persisted onto a PrivacyCase and
audited - plus the small read-only helpers (get_or_create_privacy_case,
full_clean_selected, full_clean_review_copy) main.py's Full Clean
preview/execute gating and pre-commit review copy are built on.

Keep these distinctions explicit - none of them may collapse into each
other:
  - RecipeChoice = what the user wants.
  - DeletionRecipe = knowledge about how a company handles a privacy
    request (a completely different concept - see deletion_resolver.py).
  - DeletionEvent (EventType.RECIPE_SELECTED) = audit evidence that the
    USER made a choice - never evidence that anything happened as a
    result of it.
  - CaseOutcome (app/case_outcome.py) = derived interpretation of actual
    privacy evidence, computed separately and never written here.

Selecting FULL_CLEAN does not mean anything was deleted. Selecting
JUST_THE_ESSENTIALS does not mean anything was opted out. Selecting
LEAVE_IT_BE does not mean privacy work was successfully resolved. This
module persists intent only - see select_recipe()'s docstring for the
full list of things it deliberately never does.
"""
import datetime

from sqlalchemy.orm import Session

from app.deletion_constants import EventSource, EventType, RecipeChoice
from app.deletion_events import record_event
from app.models import Company, DeletionRecipe, PrivacyCase


class InvalidRecipeChoiceError(ValueError):
    """Raised when select_recipe() is asked to persist anything other than
    one of RecipeChoice.ALL's exact three values - never None (that's the
    distinct "no recipe explicitly selected" state a PrivacyCase starts
    in, not a fourth choice that can be actively selected), never an
    unknown string, never a case/typo variant. Fails closed: no
    PrivacyCase mutation and no DeletionEvent are ever created when this
    is raised."""


def select_recipe(
    db: Session,
    privacy_case: PrivacyCase,
    recipe: str,
    source: str = EventSource.USER,
) -> PrivacyCase:
    """Persists an explicit Cleanup Recipe choice for `privacy_case`.

    USER INTENT ONLY. This function - and everything it calls - never:
      - reads or writes any Company field (deletion_status, status,
        waiting_on, next_followup_at, deletion_thread_id, or any other
        column): a Cleanup Recipe choice is not a mechanism and not
        evidence (see RecipeChoice's own docstring in
        deletion_constants.py), so it has nothing to say about the
        Company row's deletion/chase state.
      - sends Gmail, executes a deletion request, or schedules/alters a
        chase (no deletion_engine/chase_engine/google_oauth import here
        or transitively through this module).
      - classifies a response or infers any CaseOutcome axis - see
        app/case_outcome.py, computed independently and never written to.
      - creates a PrivacyAction (not part of this milestone).
      - moves anything into the Pantry beyond the selected_recipe fact
        itself - Pantry membership (CaseOutcome.is_pantry) is DERIVED
        elsewhere from selected_recipe == RecipeChoice.LEAVE_IT_BE, never
        stored or acted on here.

    Fails closed on an invalid recipe (raises InvalidRecipeChoiceError,
    with no PrivacyCase mutation and no DeletionEvent created): None is
    never accepted as a value to select (it's the separate "no recipe
    selected" state, not something a user can actively choose), and
    neither is any string outside RecipeChoice.ALL's exact three values -
    no case-insensitive or fuzzy matching, no partial credit for a typo.

    Same-value re-selection (recipe == privacy_case.selected_recipe
    already) is an idempotent no-op: no DeletionEvent, no
    recipe_selected_at change, no commit. This mirrors the repository's
    existing convention of treating a repeated identical action as a
    no-op rather than fabricating new history for it (see
    deletion_engine.py's ExecutionInFlightError/DuplicateRequestWarning,
    and migrations.py's backfill functions, which all skip re-doing
    something already true) - it exists so an accidental duplicate submit
    (double-click, retried request) can never create a fake "the user
    changed their mind" entry in the audit trail. An actual change of
    mind (a genuinely different value) always appends a new
    RECIPE_SELECTED event and updates recipe_selected_at; re-selection is
    fully allowed, and every prior selection stays in the DeletionEvent
    history, never mutated or deleted.

    Commits internally - the same top-level, single-user-action pattern
    as deletion_engine.mark_user_completed (record_event() itself only
    stages the event; the entry-point function commits once, so the
    status/timestamp change and its audit event land in one transaction).
    """
    if recipe not in RecipeChoice.ALL:
        raise InvalidRecipeChoiceError(
            f"{recipe!r} is not a valid Cleanup Recipe - must be one of {sorted(RecipeChoice.ALL)}"
        )

    previous_recipe = privacy_case.selected_recipe
    if recipe == previous_recipe:
        return privacy_case  # idempotent no-op - see docstring above

    privacy_case.selected_recipe = recipe
    privacy_case.recipe_selected_at = datetime.datetime.utcnow()
    record_event(
        db,
        privacy_case.company_id,
        EventType.RECIPE_SELECTED,
        source=source,
        evidence={"selected_recipe": recipe, "previous_recipe": previous_recipe},
        privacy_case_id=privacy_case.id,
    )
    db.commit()
    return privacy_case


def get_or_create_privacy_case(db: Session, company: Company) -> PrivacyCase:
    """Returns `company`'s existing PrivacyCase, or creates one with
    selected_recipe=None (mirrors migrations.py's backfill exactly - never
    infers a recipe) if none exists yet. Covers a company discovered after
    the last migrate() backfill ran, during a still-running server
    process - see migrations.py's _backfill_privacy_cases for the
    equivalent one-time-startup version of this same guarantee (every
    Company has exactly one PrivacyCase, selected_recipe=None until an
    explicit select_recipe() call)."""
    case = db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one_or_none()
    if case is not None:
        return case
    case = PrivacyCase(company_id=company.id, selected_recipe=None, recipe_selected_at=None)
    db.add(case)
    db.commit()
    db.refresh(case)
    return case


def full_clean_selected(db: Session, company_id: int) -> bool:
    """Whether this company's PrivacyCase currently has FULL_CLEAN
    selected - the one gate the Full Clean preview/execution routes must
    pass before proceeding (see main.py's preview_deletion_email/
    execute_company_deletion). A missing PrivacyCase is treated exactly
    like selected_recipe=None (fails closed) - it is never created just to
    check this."""
    case = db.query(PrivacyCase).filter(PrivacyCase.company_id == company_id).one_or_none()
    return case is not None and case.selected_recipe == RecipeChoice.FULL_CLEAN


def full_clean_review_copy(company: Company, recipe: DeletionRecipe | None) -> dict:
    """Compact, truthful pre-commit review copy for the Full Clean recipe -
    see select_recipe()'s module docstring for the intent-vs-evidence
    distinction this must never blur. Uses ONLY existing trusted recipe
    metadata (deletes_account) - never invents a company-specific
    consequence, and never claims a stronger/more certain outcome than the
    verified recipe actually supports. `recipe.known_consequences` (if
    any) is surfaced separately, unchanged, by the existing approval
    modal - this function does not duplicate it."""
    if recipe is not None and recipe.deletes_account is True:
        account_line = f"This will close your account with {company.name}."
    elif recipe is not None and recipe.deletes_account is False:
        account_line = (
            f"Your account with {company.name} isn't expected to close, but some data may still be removed."
        )
    else:
        account_line = f"This may close your account with {company.name}, depending on how they handle requests."

    return {
        "summary": f"Delete my eligible personal data from {company.name}.",
        "explanation": (
            f"Full Clean asks {company.name} to delete the personal information it can legally delete. "
            f"{account_line}"
        ),
        "tracking_note": (
            "Baker's Dozen will send and track the request for you when it can. "
            "It only marks your data deleted once the evidence supports that."
        ),
    }
