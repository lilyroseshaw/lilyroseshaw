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

A dedicated research pipeline (app/privacy_action_research.py,
app/privacy_action_resolver.py) now exists to move a PrivacyAction beyond
NEEDS_RESEARCH - see those modules for how a mechanism is discovered and
verified. It never invents a URL, never repurposes Full Clean's verified
deletion mechanism for a different declared purpose, and never claims a
category was cleaned up/opted out of without real evidence.

just_the_essentials_review() below is the one place PrivacyAction's raw,
internal status/evidence is translated into what the browser actually
shows. Internal vocabulary (NEEDS_RESEARCH, USER_ACTION_REQUIRED,
PrivacyAction, resolver, ...) must never reach user-facing copy - only
`status_label`/`explanation`/`cta_label`/`scope_note` are meant to be
rendered directly; `status`/`action_type` are for the caller's own
branching logic (see dashboard.js), never for display text.
"""
import datetime

from sqlalchemy.orm import Session

from app.deletion_constants import DeletionMethod, EventSource, EventType, PrivacyActionStatus, PrivacyActionType
from app.deletion_events import record_event
from app.models import Company, PrivacyAction, PrivacyCase

# Compact, plain-English label/explanation per action type - used by the
# Just the Essentials review screen. Never claims a specific mechanism;
# that's the research pipeline's job, driven by real verified data.
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

# What to call the CTA once a verified mechanism is found - specific to
# what kind of mechanism it actually is, never a generic "Continue". See
# the Just the Essentials research-pipeline UX requirement: no generic
# label when a more specific action is known.
_CTA_LABEL_BY_METHOD = {
    DeletionMethod.ACCOUNT_SETTING: "Open privacy settings",
    DeletionMethod.PRIVACY_PORTAL: "Open privacy settings",
    DeletionMethod.WEB_FORM: "Continue cleanup",
    DeletionMethod.EMAIL_REQUEST: "Continue cleanup",
}
_DEFAULT_CTA_LABEL = "Continue cleanup"

# Status pill text - the ONLY user-facing rendering of a PrivacyAction's
# status. NEEDS_RESEARCH has no pill of its own (the "Find cleanup method"
# button IS its state) - see just_the_essentials_review.
_STATUS_LABEL = {
    PrivacyActionStatus.NEEDS_REVIEW: "Needs review",
    PrivacyActionStatus.USER_ACTION_REQUIRED: "Ready for you",
    PrivacyActionStatus.SUBMITTED: "Requested",
    PrivacyActionStatus.CONFIRMED: "Confirmed",
    PrivacyActionStatus.REJECTED: "Declined",
    PrivacyActionStatus.FAILED: "Couldn't verify",
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
    known today. No DB session, no writes, no mutation of its inputs.

    `status`/`action_type` are the raw internal vocabulary, included for
    the caller's own branching (see dashboard.js) - never meant to be
    rendered as text. Every OTHER field here (`status_label`,
    `explanation`, `cta_label`, `scope_note`) is exactly what the browser
    is meant to show, with no internal jargon (NEEDS_RESEARCH,
    USER_ACTION_REQUIRED, PrivacyAction, resolver, ...) leaking into it."""
    review = []
    for action in sorted(actions, key=lambda a: a.action_type):
        copy = _ACTION_COPY.get(action.action_type, {"label": action.action_type, "summary": ""})
        entry = {
            "action_type": action.action_type,
            "status": action.status,
            "label": copy["label"],
            "summary": copy["summary"],
            "status_label": _STATUS_LABEL.get(action.status),
            "explanation": None,
            "cta_label": None,
            "cta_url": None,
            "scope_note": None,
        }
        if action.status == PrivacyActionStatus.NEEDS_RESEARCH:
            entry["explanation"] = f"Baker's Dozen needs to find {company.name}'s verified method for this."
        elif action.status == PrivacyActionStatus.NEEDS_REVIEW:
            entry["explanation"] = (
                f"Baker's Dozen couldn't verify a safe method for this yet for {company.name}."
            )
        elif action.status == PrivacyActionStatus.USER_ACTION_REQUIRED:
            entry["cta_label"] = _CTA_LABEL_BY_METHOD.get(action.method, _DEFAULT_CTA_LABEL)
            entry["cta_url"] = action.url
            entry["scope_note"] = (action.evidence or {}).get("scope_note")
        review.append(entry)
    return review
