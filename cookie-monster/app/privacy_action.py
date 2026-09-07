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
# button IS its state) - see just_the_essentials_review. USER_COMPLETED
# deliberately never says "Confirmed"/"Verified"/"Completed" alone -
# "by you" keeps the provenance visible in the one place a user actually
# reads it, so user-attested progress can never be mistaken for
# CONFIRMED's company/system-evidence meaning.
_STATUS_LABEL = {
    PrivacyActionStatus.NEEDS_REVIEW: "Needs review",
    PrivacyActionStatus.USER_ACTION_REQUIRED: "Ready for you",
    PrivacyActionStatus.USER_COMPLETED: "Completed by you",
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


def attest_user_completed(db: Session, action: PrivacyAction, company: Company) -> bool:
    """Records that the USER attests they personally completed the
    verified privacy control Baker's Dozen handed them to for this ONE
    PrivacyAction. USER-ATTESTED completion ONLY - never presented as, or
    conflated with, company-confirmed/system-verified evidence (that
    stays PrivacyActionStatus.CONFIRMED, still unreachable this
    milestone). Does not send Gmail, submit anything externally, execute
    a deletion, run research, or start a follow-up - it is a pure intent/
    provenance record, same spirit as app.privacy_case.select_recipe.

    Only valid FROM USER_ACTION_REQUIRED - there must be a real, verified
    mechanism the user could actually have used (fails closed: returns
    False, no mutation, for any other status). Idempotent if the action
    is ALREADY USER_COMPLETED: returns True without rewriting evidence or
    appending a duplicate audit event, so a double-submit (double-click,
    retried request) can never fabricate a second "the user did this
    again" entry.

    Provenance is stored in the action's own `evidence` JSON (merged, not
    replaced, so the mechanism's scope_note/source_url/confidence already
    recorded by research survive) rather than a new column - `attested_by`
    is always EventSource.USER, never inferred, never fabricated for a
    company/system source."""
    if action.status == PrivacyActionStatus.USER_COMPLETED:
        return True
    if action.status != PrivacyActionStatus.USER_ACTION_REQUIRED:
        return False

    now = datetime.datetime.utcnow()
    action.status = PrivacyActionStatus.USER_COMPLETED
    action.evidence = {
        **(action.evidence or {}),
        "attested_by": EventSource.USER,
        "attested_at": now.isoformat(),
    }
    record_event(
        db, company.id, EventType.PRIVACY_ACTION_USER_COMPLETED, source=EventSource.USER,
        evidence={"action_type": action.action_type},
        privacy_action_id=action.id,
    )
    db.commit()
    return True


# Dashboard card summary vocabulary - deliberately separate from
# _STATUS_LABEL (the JTE modal's per-action pill text). Ordered by
# priority so a mixed-state summary lists the more-resolved action(s)
# first (see just_the_essentials_dashboard_summary). CONFIRMED and
# USER_COMPLETED never share a clause: keeping them distinct statuses
# here is exactly what lets the company/system-evidence-vs-user-attested
# distinction survive onto the compact card summary, not just the modal.
_DASHBOARD_CLAUSE_ORDER = [
    PrivacyActionStatus.CONFIRMED,
    PrivacyActionStatus.USER_COMPLETED,
    PrivacyActionStatus.USER_ACTION_REQUIRED,
    PrivacyActionStatus.NEEDS_REVIEW,
    PrivacyActionStatus.NEEDS_RESEARCH,
    PrivacyActionStatus.REJECTED,
    PrivacyActionStatus.FAILED,
]


def _dashboard_clause(status: str, n: int) -> str:
    if status == PrivacyActionStatus.CONFIRMED:
        return f"{n} confirmed"
    if status == PrivacyActionStatus.USER_COMPLETED:
        return f"{n} completed by you"
    if status == PrivacyActionStatus.USER_ACTION_REQUIRED:
        return f"{n} ready for you"
    if status == PrivacyActionStatus.NEEDS_REVIEW:
        return f"{n} needs review" if n == 1 else f"{n} need review"
    if status == PrivacyActionStatus.NEEDS_RESEARCH:
        return f"{n} needs research" if n == 1 else f"{n} need research"
    if status == PrivacyActionStatus.REJECTED:
        return f"{n} declined"
    return f"{n} couldn't be verified"  # FAILED


def just_the_essentials_dashboard_summary(actions: list[PrivacyAction]) -> str:
    """Compact, truthful one-line progress summary for the main dashboard
    company card - READ-ONLY presentation over the SAME PrivacyAction rows
    the JTE modal already shows in detail (see just_the_essentials_review).
    No DB session, no writes, no CaseOutcome involvement: this never
    invents a new privacy-work state, it only summarizes the two existing
    PrivacyAction statuses into one line.

    Ground rules this must never violate:
      - USER_COMPLETED always keeps its "by you" provenance - never
        collapsed into a bare "completed"/"confirmed"/"verified" that
        could pass for company/system evidence.
      - CONFIRMED and USER_COMPLETED are never merged into a single
        clause - a mix of the two says exactly how many are "by you"
        rather than implying the company confirmed both.
      - Never says "deleted", "verified", or "company confirmed" for a
        user-attested action - this vocabulary is entirely separate from
        (and much narrower than) _STATUS_LABEL's per-action pill text.
      - Never implies Full Clean success or that historical
        tracking/profile data was deleted or previously-shared data was
        recalled - it only counts PrivacyAction statuses, nothing else.

    If every action has reached a positive outcome (CONFIRMED and/or
    USER_COMPLETED), returns a single "X of Y privacy controls ..." line.
    Otherwise returns one short clause per distinct status present
    (ordered by _DASHBOARD_CLAUSE_ORDER), joined with " · ", so a mixed
    state like one USER_ACTION_REQUIRED + one NEEDS_REVIEW reads as
    "1 ready for you · 1 needs review" rather than a false single state."""
    total = len(actions)
    if total == 0:
        return ""

    statuses = [a.status for a in actions]
    n_confirmed = statuses.count(PrivacyActionStatus.CONFIRMED)
    n_user = statuses.count(PrivacyActionStatus.USER_COMPLETED)
    n_completed = n_confirmed + n_user

    if n_completed == total:
        if n_user == 0:
            return f"{total} of {total} privacy controls confirmed"
        if n_confirmed == 0:
            return f"{total} of {total} privacy controls completed by you"
        return f"{total} of {total} privacy controls completed · {n_user} by you"

    clauses = []
    for status in _DASHBOARD_CLAUSE_ORDER:
        n = statuses.count(status)
        if n:
            clauses.append(_dashboard_clause(status, n))
    return " · ".join(clauses)


def just_the_essentials_review(company: Company, actions: list[PrivacyAction]) -> list[dict]:
    """Pure, read-only review payload for the Just the Essentials preview
    screen - one entry per PrivacyAction, truthful about exactly what's
    known today. No DB session, no writes, no mutation of its inputs.

    `status`/`action_type` are the raw internal vocabulary, included for
    the caller's own branching (see dashboard.js) - never meant to be
    rendered as text. Every OTHER field here (`status_label`,
    `explanation`, `cta_label`, `scope_note`, `can_attest`) is exactly
    what the browser is meant to show/branch on, with no internal jargon
    (NEEDS_RESEARCH, USER_ACTION_REQUIRED, PrivacyAction, resolver, ...)
    leaking into display text."""
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
            # Only true from USER_ACTION_REQUIRED - the one status with a
            # real, verified mechanism the user could actually have used.
            "can_attest": action.status == PrivacyActionStatus.USER_ACTION_REQUIRED,
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
        elif action.status == PrivacyActionStatus.USER_COMPLETED:
            # No remaining action - no cta_label/cta_url, just the
            # "Completed by you" status pill (see _STATUS_LABEL) plus the
            # SAME truthful scope_note recorded when the mechanism was
            # originally found, carried over unchanged by
            # attest_user_completed's evidence merge.
            entry["scope_note"] = (action.evidence or {}).get("scope_note")
        review.append(entry)
    return review
