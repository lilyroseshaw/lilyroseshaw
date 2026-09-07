"""Top-level, consumer-facing destination for the upcoming Baker's Dozen
UI - collapses the existing, already-derived CaseOutcome (app/case_outcome.py)
into exactly one of four presentation buckets a real user actually needs to
navigate by (WORKING / NEEDS_YOU / DONE / PANTRY).

PURE READ-MODEL, same guarantees as app/case_outcome.py: no DB session, no
writes, no mutation of Company/PrivacyCase/PrivacyAction/MailMessage/
DeletionEvent/game state, no Gmail/chase imports, deterministic and safe to
call as often as needed. This module never re-derives privacy evidence or
JTE per-action status itself - app.case_outcome.derive_case_outcome remains
the ONE place that happens; everything here does is collapse that richer,
already-correct projection down to four values a top-level UI can branch
on without re-implementing any of that logic.
"""
from app.case_outcome import CaseOutcome, derive_case_outcome
from app.deletion_constants import CaseState
from app.models import Company, PrivacyAction, PrivacyCase


class TopLevelState:
    """Exactly one of these four values - the ONLY vocabulary the upcoming
    UI's top-level navigation is meant to branch on. Never persisted (see
    module docstring) - always re-derived at read time from
    CaseOutcome.is_pantry/overall, which are themselves derived, not
    stored columns."""
    WORKING = "WORKING"
    NEEDS_YOU = "NEEDS_YOU"
    DONE = "DONE"
    PANTRY = "PANTRY"

    ALL = {WORKING, NEEDS_YOU, DONE, PANTRY}


# CaseState.* (app/case_outcome.py's `overall` axis) -> TopLevelState.* .
# Exhaustive over CaseState.ALL - see the assert below. This is the ONLY
# place that mapping happens; nothing here re-derives privacy evidence,
# JTE per-action status, or chase/waiting_on logic - all of that is
# already computed by derive_case_outcome.
#
#   WORKING       -> WORKING    Still active/automatic, no user action due
#                    right now: SUBMITTED/IN_PROGRESS/
#                    ACCOUNT_CLOSED_DATA_UNVERIFIED/
#                    ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED (Baker's
#                    Dozen, not the user, is chasing the company for
#                    confirmation), a JTE case whose actions are all still
#                    NEEDS_RESEARCH (Baker's Dozen's own lookup, not a
#                    user step), or an UNKNOWN_RESPONSE reply
#                    chase_engine has resolved to WaitingOn.COMPANY.
#   NEEDS_USER    -> NEEDS_YOU  The next required actor IS the user:
#                    VERIFICATION_NEEDED/MORE_INFO_REQUIRED/
#                    USER_ACTION_REQUIRED, a JTE action ready for the user
#                    (or a mix where at least one still needs the user),
#                    or an UNKNOWN_RESPONSE reply chase_engine has
#                    resolved to WaitingOn.USER.
#   RESOLVED      -> DONE       Real company/system evidence resolved the
#                    case: Full Clean COMPLETED, or every JTE action
#                    CONFIRMED.
#   USER_RESOLVED -> DONE       Every JTE action reached a positive
#                    outcome and at least one is the user's own
#                    attestation. Still genuinely "nothing left for the
#                    user to do" from the consumer's point of view, even
#                    though CaseOutcome itself deliberately keeps
#                    USER_RESOLVED distinct from RESOLVED internally so
#                    company/system-confirmed evidence is never confused
#                    with user-attested evidence - that provenance
#                    distinction is preserved in CaseOutcome, not erased,
#                    it just isn't part of what THIS four-value top-level
#                    destination distinguishes.
#   UNRESOLVED    -> NEEDS_YOU  A terminal outcome the automatic process
#                    has already stopped on and only a HUMAN DECISION can
#                    move it forward: REJECTED (chase_engine routes this
#                    to WaitingOn.ESCALATION_NEEDED - the dashboard's own
#                    existing "NEEDS YOUR ATTENTION" framing) and FAILED
#                    (the dashboard's existing "Send again" affordance is
#                    a manual retry Baker's Dozen never performs on its
#                    own). Despite its name, CaseState.UNRESOLVED is NOT
#                    "Baker's Dozen is still working on it" - that's
#                    CaseState.WORKING; UNRESOLVED means the automatic
#                    process already gave up and control has passed to a
#                    person, which is exactly what NEEDS_YOU means here.
_CASE_STATE_TO_TOP_LEVEL = {
    CaseState.WORKING: TopLevelState.WORKING,
    CaseState.NEEDS_USER: TopLevelState.NEEDS_YOU,
    CaseState.RESOLVED: TopLevelState.DONE,
    CaseState.USER_RESOLVED: TopLevelState.DONE,
    CaseState.UNRESOLVED: TopLevelState.NEEDS_YOU,
}

assert set(_CASE_STATE_TO_TOP_LEVEL.keys()) == CaseState.ALL, (
    "every CaseState value must have an explicit TopLevelState mapping - see this module's docstring"
)


def derive_top_level_state(
    company: Company,
    privacy_case: PrivacyCase | None = None,
    actions: list[PrivacyAction] | None = None,
    *,
    case_outcome: CaseOutcome | None = None,
) -> str:
    """Pure, deterministic projection onto TopLevelState.* - no DB session,
    no writes, no mutation of any argument, safe to call as often as
    needed. Reuses app.case_outcome.derive_case_outcome as the ONLY source
    of privacy-evidence/JTE-action interpretation (never a second, parallel
    outcome engine) - this function's entire job is collapsing that
    already-derived, richer CaseOutcome down to the four buckets a
    top-level UI needs to navigate by.

    Precedence (mirrors the PANTRY / OVERALL INVARIANT documented in
    app/case_outcome.py exactly):
      1. PANTRY   - CaseOutcome.is_pantry, checked FIRST and
                    unconditionally. A Pantry company presents as PANTRY
                    even when it also carries real historical deletion
                    evidence (e.g. was COMPLETED before the user chose
                    Leave It Be) - this NEVER erases or overwrites that
                    evidence: CaseOutcome itself still derives the true
                    `overall`/`personal_data`/etc. from Company.
                    deletion_status exactly as it always did; this
                    function just doesn't surface that as the company's
                    top-level destination once the user has disposed of
                    it into the Pantry.
      2. Otherwise, CaseOutcome.overall (a CaseState.* value) maps via
         _CASE_STATE_TO_TOP_LEVEL above - DONE only for a real resolved
         outcome (RESOLVED/USER_RESOLVED), NEEDS_YOU whenever the next
         required actor is the user OR the automatic process has already
         stopped and is waiting on a human decision (NEEDS_USER/
         UNRESOLVED), and WORKING for every other still-active/automatic
         state.

    Pass an already-computed `case_outcome` to skip recomputing it (e.g. a
    caller that already needs the full CaseOutcome for other display
    purposes); otherwise it's derived internally from `company`/
    `privacy_case`/`actions` exactly as
    app.case_outcome.derive_case_outcome already does."""
    outcome = case_outcome if case_outcome is not None else derive_case_outcome(company, privacy_case, actions)
    if outcome.is_pantry:
        return TopLevelState.PANTRY
    return _CASE_STATE_TO_TOP_LEVEL[outcome.overall]
