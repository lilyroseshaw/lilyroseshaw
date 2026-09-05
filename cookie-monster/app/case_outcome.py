"""Pure, read-time privacy outcome projection over a Company's already-
audited deletion state - the first piece of the Cleanup Recipes milestone's
PrivacyCase architecture (see models.py's PrivacyCase and
deletion_constants.py's RecipeChoice/AccountOutcome/PersonalDataOutcome/
NonessentialTrackingOutcome/OptOutOutcome/RetentionOutcome/CaseState).

PRIVACY OUTCOMES ARE DERIVED, NOT STORED. derive_case_outcome() is a pure
function: no DB session, no writes, no mutation of its inputs, no Gmail/
chase imports, deterministic and safe to call as often as needed. The
proven deletion/response/chase engine (deletion_engine.py,
deletion_response_tracker.py, chase_engine.py, response_classify.py)
remains the single source of truth for what actually happened - this module
only re-reads and re-labels that evidence along independent axes. If a
materialized/cached projection is ever needed for performance, that must be
an explicit, separately-versioned layer with reconciliation - never a
second silently-stale copy of what's derived here.

A Cleanup Recipe (PrivacyCase.selected_recipe) describes USER INTENT. It is
NOT evidence that anything happened, and it must never reinterpret
evidence: a legacy case with selected_recipe=None and a real deletion_status
derives the exact same account/personal_data/retention meaning as a modern
case with the same deletion_status. selected_recipe affects ONLY:
  - is_pantry (RecipeChoice.LEAVE_IT_BE only)
  - whether the JUST_THE_ESSENTIALS-specific axes (nonessential_tracking,
    opt_out) are applicable (non-None) at all
  - nothing else - overall/account/personal_data/retention are always
    derived purely from Company.deletion_status/deletion_evidence/
    waiting_on, exactly as CaseState's docstring requires.

LEAVE_IT_BE does not erase history: a company already carrying real
deletion evidence before the user chooses LEAVE_IT_BE keeps deriving that
same evidence-based outcome. The Pantry disposition changes nothing about
what's true; it only tells higher layers "don't pursue this further."
"""
from dataclasses import dataclass

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


@dataclass(frozen=True)
class CaseOutcome:
    """Non-persisted value object - see this module's docstring. Every
    field is a plain controlled-vocabulary string (or None where an axis
    doesn't apply) from deletion_constants.py, never a DB column."""
    account: str  # AccountOutcome.*
    personal_data: str  # PersonalDataOutcome.*
    # None unless RecipeChoice.JUST_THE_ESSENTIALS was selected - not
    # applicable for FULL_CLEAN, LEAVE_IT_BE, or no recipe selected at all.
    nonessential_tracking: str | None  # NonessentialTrackingOutcome.* or None
    opt_out: str | None  # OptOutOutcome.* or None
    retention: str  # RetentionOutcome.*
    overall: str  # CaseState.*
    is_pantry: bool


@dataclass(frozen=True)
class _StatusOutcomeRow:
    """One row of the per-DeletionStatus mapping table below - account/
    personal_data/retention/overall as they mean today, independent of any
    Cleanup Recipe. UNKNOWN_RESPONSE's `overall` here is a base value only;
    derive_case_outcome refines it using company.waiting_on (see below)."""
    account: str
    personal_data: str
    retention: str
    overall: str


# Exhaustive mapping over the CURRENT DeletionStatus vocabulary
# (deletion_constants.py). Every existing value is covered explicitly - see
# test_case_outcome.py's test asserting this table's keys equal
# DeletionStatus.ALL, so a future new status can never silently fall
# through unmapped.
#
# Reasoning, status by status (see deletion_constants.py/response_classify.py/
# deletion_engine.py/main.py for the underlying evidence each relies on):
#
# NOT_STARTED / METHOD_LOOKUP / READY / SUBMITTING / UNKNOWN / NO_METHOD_FOUND:
#   No company reply exists yet (SUBMITTING is Cookie Monster's own send
#   attempt in flight, not yet confirmed sent or answered). account and
#   personal_data are UNKNOWN - nothing has been claimed by anyone yet, so
#   "nothing has happened yet" is the honest framing (never RETAINED, which
#   would assert a claim no one has made). overall=WORKING: main.py's own
#   _METHOD_NOT_READY_STATUSES/_METHOD_READY_STATUSES groupings treat these
#   as "in the pipeline, not yet actionable-needed", never terminal.
# CONFIRMATION_REQUIRED:
#   Defined in DeletionStatus but not currently set anywhere in the engine
#   (grep confirms no production code path emits it - the equivalent "needs
#   explicit confirmation before executing" behavior today lives in the
#   ephemeral deletion/preview -> deletion/execute flow, never a persisted
#   status). Mapped conservatively by name alone: a status literally called
#   "confirmation required" means the USER needs to confirm something
#   before progress continues, so overall=NEEDS_USER. Covered by a test
#   documenting it as currently unreachable in production code.
# SUBMITTED / IN_PROGRESS:
#   A request has actually gone out (SUBMITTED: gmail_send-verified per
#   deletion_engine._execute_email_request; IN_PROGRESS: the company
#   acknowledged it's processing). personal_data=DELETION_REQUESTED (a
#   request is outstanding, not yet confirmed one way or another).
#   overall=WORKING - chase_engine.derive_waiting_on maps both to
#   WaitingOn.COMPANY: the ball is in the company's court, not the user's.
# VERIFICATION_NEEDED / MORE_INFO_REQUIRED:
#   The company replied asking the user to verify identity or provide more
#   information (response_classify.py's VERIFICATION_NEEDED_PATTERNS/
#   MORE_INFO_REQUIRED_PATTERNS). A request is outstanding
#   (personal_data=DELETION_REQUESTED). overall=NEEDS_USER unconditionally:
#   chase_engine.derive_waiting_on maps BOTH of these to WaitingOn.USER with
#   no branching at all, so this is exactly the "waiting_on == USER ...
#   proven state" the architecture calls for - no need to even consult
#   company.waiting_on defensively, since the engine never sets anything
#   else for these two statuses.
# USER_ACTION_REQUIRED:
#   Three distinct engine paths land here (deletion_engine.py's
#   _draft_only_email_request, _route_to_user_action, and
#   recover_stuck_submitting), and NONE of them means a request has
#   actually reached the company from Cookie Monster's own tracked
#   evidence yet: a draft prepared but not sent, an official page opened
#   but not yet completed by the user, or a send whose outcome is
#   genuinely unknown after a process crash. personal_data=UNKNOWN (not
#   DELETION_REQUESTED - none of these confirm a request reached the
#   company) is the conservative, honest reading of all three.
#   overall=NEEDS_USER by the status's own name - is_chase_eligible only
#   covers EMAIL_REQUEST cases, so company.waiting_on is frequently None
#   here (the manual WEB_FORM/PORTAL/ACCOUNT_SETTING/MANUAL paths are
#   never chase-eligible) and can't be relied on; the status itself is
#   the authority.
# COMPLETED:
#   personal_data=DELETION_CONFIRMED ONLY because response_classify.py's
#   COMPLETED_PATTERNS already require an explicit personal-data object
#   (see that module's docstring on the "must never be wrong" guardrails,
#   plus mark_user_completed's user-self-report path) - never inferred
#   here from anything weaker. account stays UNKNOWN: COMPLETED can be
#   reached from a personal-data-only claim with no mention of the account
#   at all, so assuming CLOSED would overclaim. overall=RESOLVED.
# REJECTED:
#   The company explicitly declined/cannot fulfill the request
#   (response_classify.py's REJECTED_PATTERNS) - the safest evidence-based
#   reading is personal_data=RETAINED (an explicit refusal to delete means
#   the data continues to be held). overall=UNRESOLVED per the approved
#   architecture note - this is a terminal, unresolved-in-the-user's-favor
#   outcome, not a resolved one.
# FAILED:
#   Reserved for a genuinely permanent TECHNICAL failure - the Gmail send
#   itself failing, or the tracked thread no longer existing at all (see
#   deletion_response_tracker.py's module docstring: "DeletionStatus.FAILED
#   is reserved for a genuinely permanent failure ... never set merely
#   because one poll attempt errored"). This is Cookie-Monster-side, not a
#   company privacy response - account/personal_data both stay UNKNOWN
#   (never RETAINED/anything asserting a company outcome that never
#   happened). overall=UNRESOLVED (terminal, needs manual attention) while
#   staying evidence-honest that this is a tracking failure, not a
#   privacy outcome.
# UNKNOWN_RESPONSE:
#   A reply arrived that the classifier couldn't confidently place - a
#   request is outstanding (personal_data=DELETION_REQUESTED), but who it's
#   waiting on is genuinely ambiguous by design (chase_engine.derive_waiting_on
#   branches on body-text a pure company/privacy_case-only function has no
#   access to, and should not re-derive). The base row here is WORKING;
#   derive_case_outcome refines it to NEEDS_USER using company.waiting_on,
#   which chase_engine already computed and stored - reusing that recorded
#   answer rather than re-running classification logic here.
# ACCOUNT_CLOSED_DATA_UNVERIFIED:
#   The company confirmed closing/deactivating the ACCOUNT specifically,
#   with nothing said about the underlying data (see this status's own
#   docstring in deletion_constants.py) - account=CLOSED (the one status
#   this milestone's architecture calls out explicitly), personal_data
#   stays DELETION_REQUESTED (still outstanding, never DELETION_CONFIRMED -
#   the hard safety rule). overall=WORKING: chase_engine keeps this in
#   WaitingOn.COMPANY, still actively chasing for the data answer.
# ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED:
#   A stronger, more specific claim than mere closure - the account
#   RECORD itself was said to be deleted - but still not a claim about the
#   user's broader personal information (see this status's own docstring).
#   account stays UNKNOWN per the architecture's explicit instruction (a
#   record-deletion claim is not the same claim as account closure, so it
#   must not be promoted to CLOSED without evidence that specifically
#   establishes closure). personal_data=PARTIALLY_DELETED: this is the one
#   status where something concrete and real (the account record) was
#   actually confirmed deleted, while the rest of the user's personal
#   information remains unconfirmed - an honest middle value, still never
#   DELETION_CONFIRMED. overall=WORKING (chase_engine: WaitingOn.COMPANY).
_STATUS_OUTCOME_TABLE: dict[str, _StatusOutcomeRow] = {
    DeletionStatus.NOT_STARTED: _StatusOutcomeRow(
        AccountOutcome.UNKNOWN, PersonalDataOutcome.UNKNOWN, RetentionOutcome.NONE_DISCLOSED, CaseState.WORKING,
    ),
    DeletionStatus.METHOD_LOOKUP: _StatusOutcomeRow(
        AccountOutcome.UNKNOWN, PersonalDataOutcome.UNKNOWN, RetentionOutcome.NONE_DISCLOSED, CaseState.WORKING,
    ),
    DeletionStatus.READY: _StatusOutcomeRow(
        AccountOutcome.UNKNOWN, PersonalDataOutcome.UNKNOWN, RetentionOutcome.NONE_DISCLOSED, CaseState.WORKING,
    ),
    DeletionStatus.CONFIRMATION_REQUIRED: _StatusOutcomeRow(
        AccountOutcome.UNKNOWN, PersonalDataOutcome.UNKNOWN, RetentionOutcome.NONE_DISCLOSED, CaseState.NEEDS_USER,
    ),
    DeletionStatus.SUBMITTING: _StatusOutcomeRow(
        AccountOutcome.UNKNOWN, PersonalDataOutcome.DELETION_REQUESTED, RetentionOutcome.NONE_DISCLOSED,
        CaseState.WORKING,
    ),
    DeletionStatus.SUBMITTED: _StatusOutcomeRow(
        AccountOutcome.UNKNOWN, PersonalDataOutcome.DELETION_REQUESTED, RetentionOutcome.NONE_DISCLOSED,
        CaseState.WORKING,
    ),
    DeletionStatus.IN_PROGRESS: _StatusOutcomeRow(
        AccountOutcome.UNKNOWN, PersonalDataOutcome.DELETION_REQUESTED, RetentionOutcome.NONE_DISCLOSED,
        CaseState.WORKING,
    ),
    DeletionStatus.VERIFICATION_NEEDED: _StatusOutcomeRow(
        AccountOutcome.UNKNOWN, PersonalDataOutcome.DELETION_REQUESTED, RetentionOutcome.NONE_DISCLOSED,
        CaseState.NEEDS_USER,
    ),
    DeletionStatus.MORE_INFO_REQUIRED: _StatusOutcomeRow(
        AccountOutcome.UNKNOWN, PersonalDataOutcome.DELETION_REQUESTED, RetentionOutcome.NONE_DISCLOSED,
        CaseState.NEEDS_USER,
    ),
    DeletionStatus.USER_ACTION_REQUIRED: _StatusOutcomeRow(
        AccountOutcome.UNKNOWN, PersonalDataOutcome.UNKNOWN, RetentionOutcome.NONE_DISCLOSED, CaseState.NEEDS_USER,
    ),
    DeletionStatus.COMPLETED: _StatusOutcomeRow(
        AccountOutcome.UNKNOWN, PersonalDataOutcome.DELETION_CONFIRMED, RetentionOutcome.NONE_DISCLOSED,
        CaseState.RESOLVED,
    ),
    DeletionStatus.REJECTED: _StatusOutcomeRow(
        AccountOutcome.UNKNOWN, PersonalDataOutcome.RETAINED, RetentionOutcome.NONE_DISCLOSED, CaseState.UNRESOLVED,
    ),
    DeletionStatus.FAILED: _StatusOutcomeRow(
        AccountOutcome.UNKNOWN, PersonalDataOutcome.UNKNOWN, RetentionOutcome.NONE_DISCLOSED, CaseState.UNRESOLVED,
    ),
    DeletionStatus.UNKNOWN_RESPONSE: _StatusOutcomeRow(
        AccountOutcome.UNKNOWN, PersonalDataOutcome.DELETION_REQUESTED, RetentionOutcome.NONE_DISCLOSED,
        CaseState.WORKING,
    ),
    DeletionStatus.UNKNOWN: _StatusOutcomeRow(
        AccountOutcome.UNKNOWN, PersonalDataOutcome.UNKNOWN, RetentionOutcome.NONE_DISCLOSED, CaseState.WORKING,
    ),
    DeletionStatus.NO_METHOD_FOUND: _StatusOutcomeRow(
        AccountOutcome.UNKNOWN, PersonalDataOutcome.UNKNOWN, RetentionOutcome.NONE_DISCLOSED, CaseState.WORKING,
    ),
    DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED: _StatusOutcomeRow(
        AccountOutcome.CLOSED, PersonalDataOutcome.DELETION_REQUESTED, RetentionOutcome.NONE_DISCLOSED,
        CaseState.WORKING,
    ),
    DeletionStatus.ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED: _StatusOutcomeRow(
        AccountOutcome.UNKNOWN, PersonalDataOutcome.PARTIALLY_DELETED, RetentionOutcome.NONE_DISCLOSED,
        CaseState.WORKING,
    ),
}

assert set(_STATUS_OUTCOME_TABLE.keys()) == DeletionStatus.ALL, (
    "every DeletionStatus value must have an explicit outcome mapping - see this module's docstring"
)


def derive_case_outcome(company: Company, privacy_case: PrivacyCase | None = None) -> CaseOutcome:
    """Pure projection of a company's already-audited deletion evidence
    (plus, optionally, its PrivacyCase's selected Cleanup Recipe) onto the
    independent CaseOutcome axes. No DB session, no writes, no mutation of
    `company` or `privacy_case`, no Gmail/chase imports, deterministic -
    see this module's docstring for the full set of hard rules.
    """
    selected_recipe = privacy_case.selected_recipe if privacy_case is not None else None
    is_pantry = privacy_case is not None and selected_recipe == RecipeChoice.LEAVE_IT_BE

    row = _STATUS_OUTCOME_TABLE.get(company.deletion_status, _STATUS_OUTCOME_TABLE[DeletionStatus.UNKNOWN])
    overall = row.overall

    # The one status whose "who's it waiting on" question chase_engine
    # itself resolves ambiguously (body-text dependent) rather than
    # unconditionally - see _STATUS_OUTCOME_TABLE's docstring comment for
    # UNKNOWN_RESPONSE. Reuse chase_engine's own recorded answer
    # (company.waiting_on) instead of re-deriving it from text this
    # function never receives.
    if company.deletion_status == DeletionStatus.UNKNOWN_RESPONSE:
        overall = CaseState.NEEDS_USER if company.waiting_on == WaitingOn.USER else CaseState.WORKING

    if selected_recipe == RecipeChoice.JUST_THE_ESSENTIALS:
        # No PrivacyAction/execution engine exists yet for this recipe in
        # this milestone, so this is always the initial "not yet resolved"
        # value today - never CONFIRMED from the recipe choice alone.
        nonessential_tracking = NonessentialTrackingOutcome.UNRESOLVED
        opt_out = OptOutOutcome.UNKNOWN
    else:
        # Not applicable for FULL_CLEAN, LEAVE_IT_BE, or no recipe selected
        # at all - None, not UNRESOLVED/UNKNOWN, since these recipes never
        # make this specific ask in the first place.
        nonessential_tracking = None
        opt_out = None

    return CaseOutcome(
        account=row.account,
        personal_data=row.personal_data,
        nonessential_tracking=nonessential_tracking,
        opt_out=opt_out,
        retention=row.retention,
        overall=overall,
        is_pantry=is_pantry,
    )
