"""Just the Essentials mechanism tracking - PrivacyAction (models.py) is
the one place a JUST_THE_ESSENTIALS PrivacyCase's individual, mechanism-
level sub-requests (nonessential-tracking cleanup, sale/sharing opt-out)
are materialized and assessed.

Keep the distinctions this milestone has established throughout explicit:
  - RecipeChoice.JUST_THE_ESSENTIALS (PrivacyCase) = USER INTENT.
  - PrivacyAction = mechanism-level EXECUTION TRACKING for that intent -
    what Baker's Dozen has found/attempted for ONE specific ask, never
    evidence about the whole recipe by itself.
  - CaseOutcome (app/case_outcome.py) = the derived interpretation of
    actual evidence across everything, computed separately.

HONESTY OVER COMPLETENESS: no research pipeline exists yet that verifies
a nonessential-tracking-cleanup or sale/sharing opt-out mechanism for any
company (DeletionRecipe only ever researches a full-deletion mechanism -
see deletion_resolver.py). So classify_privacy_action() always returns
NEEDS_RESEARCH today. This is not a stub standing in for a missing
feature; it is the accurate, non-fabricated answer given what Baker's
Dozen actually knows right now. It must never invent a URL, repurpose
Full Clean's verified deletion mechanism for a different declared
purpose, or claim a category was cleaned up/opted out of without real
evidence.
"""
import datetime

from sqlalchemy.orm import Session

from app.deletion_constants import EventSource, EventType, PrivacyActionStatus, PrivacyActionType
from app.deletion_events import record_event
from app.models import Company, PrivacyAction, PrivacyCase

# Compact, plain-English label/explanation per action type - used by the
# Just the Essentials review screen. Never claims a specific mechanism;
# that's classify_privacy_action()'s job, driven by real (currently
# nonexistent) verified data.
_ACTION_COPY = {
    PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP: {
        "label": "Nonessential tracking & profiling cleanup",
        "summary": (
            "Analytics, advertising identifiers, cross-site tracking, marketing profiles, "
            "and similar nonessential tracking/profiling data - never your account, "
            "purchases, or service-essential information."
        ),
    },
    PrivacyActionType.SALE_SHARING_OPT_OUT: {
        "label": "Sale/sharing & targeted-advertising opt-out",
        "summary": "An applicable opt-out from the sale/sharing of your data or cross-context behavioral advertising.",
    },
}


def classify_privacy_action(company: Company, action_type: str) -> dict:
    """The single source of truth for what Baker's Dozen currently knows
    about pursuing `action_type` for `company` - mirrors the shape/spirit
    of deletion_engine.classify_execution_capability, but for a
    nonessential-tracking/opt-out mechanism instead of full deletion.

    Always returns NEEDS_RESEARCH today (see module docstring) - never
    fabricates a mechanism. Returns a plain dict (not persisted directly)
    so callers decide what, if anything, changed on the PrivacyAction row.
    """
    return {
        "status": PrivacyActionStatus.NEEDS_RESEARCH,
        "method": None,
        "url": None,
        "reason": (
            f"Baker's Dozen doesn't yet have a verified way to do this for {company.name} - "
            "this will be tracked and revisited as research support is added."
        ),
    }


def ensure_just_the_essentials_actions(db: Session, privacy_case: PrivacyCase) -> list[PrivacyAction]:
    """Idempotent get-or-create for the two PrivacyActionType rows a
    JUST_THE_ESSENTIALS case needs. Never called automatically by
    select_recipe() itself (recipe selection stays intent-only, with zero
    side effects beyond the PrivacyCase/DeletionEvent it already touches)
    - callers (main.py's select_company_recipe) invoke this as an
    explicit, separate, auditable step, only when the just-selected
    recipe is JUST_THE_ESSENTIALS.

    Commits internally, same top-level pattern as
    app.privacy_case.select_recipe. Safe to call repeatedly: existing rows
    are returned unchanged, never duplicated, never overwritten."""
    existing = {
        a.action_type: a
        for a in db.query(PrivacyAction).filter(PrivacyAction.privacy_case_id == privacy_case.id).all()
    }
    created = []
    for action_type in sorted(PrivacyActionType.ALL):
        if action_type in existing:
            continue
        action = PrivacyAction(
            privacy_case_id=privacy_case.id,
            action_type=action_type,
            method="UNKNOWN",
            status=PrivacyActionStatus.NEEDS_RESEARCH,
            evidence={},
        )
        db.add(action)
        db.flush()  # populate action.id for the event FK below
        record_event(
            db,
            privacy_case.company_id,
            EventType.PRIVACY_ACTION_NEEDS_RESEARCH,
            source=EventSource.SYSTEM,
            evidence={"action_type": action_type},
            privacy_case_id=privacy_case.id,
            privacy_action_id=action.id,
        )
        created.append(action)
    if created:
        db.commit()
    return sorted(existing.values(), key=lambda a: a.action_type) + created


def just_the_essentials_review(company: Company, actions: list[PrivacyAction]) -> list[dict]:
    """Pure, read-only review payload for the Just the Essentials preview
    screen - one entry per PrivacyAction, truthful about exactly what's
    known today. No DB session, no writes, no mutation of its inputs."""
    review = []
    for action in sorted(actions, key=lambda a: a.action_type):
        copy = _ACTION_COPY.get(action.action_type, {"label": action.action_type, "summary": ""})
        plan = classify_privacy_action(company, action.action_type)
        review.append({
            "action_type": action.action_type,
            "label": copy["label"],
            "summary": copy["summary"],
            "status": action.status,
            "reason": plan["reason"] if action.status == PrivacyActionStatus.NEEDS_RESEARCH else None,
        })
    return review
