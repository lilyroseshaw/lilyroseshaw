"""Cleanup Recipe selection - the one place explicit USER INTENT (see
RecipeChoice in deletion_constants.py) is persisted onto a PrivacyCase and
audited.

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
from app.models import PrivacyCase


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
