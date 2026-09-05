"""Controlled vocabularies for the deletion-request feature. Kept as plain
string constants (not a DB-level enum type) so SQLite migrations stay
additive/simple - see migrations.py.
"""


class DeletionMethod:
    WEB_FORM = "WEB_FORM"
    ACCOUNT_SETTING = "ACCOUNT_SETTING"
    EMAIL_REQUEST = "EMAIL_REQUEST"
    PRIVACY_PORTAL = "PRIVACY_PORTAL"
    API = "API"
    MANUAL = "MANUAL"
    UNKNOWN = "UNKNOWN"

    ALL = {WEB_FORM, ACCOUNT_SETTING, EMAIL_REQUEST, PRIVACY_PORTAL, API, MANUAL, UNKNOWN}


class ActionCapability:
    FULLY_AUTOMATABLE = "FULLY_AUTOMATABLE"
    PARTIALLY_AUTOMATABLE = "PARTIALLY_AUTOMATABLE"
    USER_ACTION_REQUIRED = "USER_ACTION_REQUIRED"
    UNKNOWN = "UNKNOWN"

    ALL = {FULLY_AUTOMATABLE, PARTIALLY_AUTOMATABLE, USER_ACTION_REQUIRED, UNKNOWN}


class ExecutionCapability:
    """What the execution engine will actually DO on approval, for THIS
    company right now - see deletion_engine.classify_execution_capability.
    Distinct from ActionCapability above: ActionCapability is a property of
    the RECIPE (how the method intrinsically works, as researched, stored
    on DeletionRecipe/Company); ExecutionCapability is computed fresh at
    approval/execute time from the recipe PLUS what Cookie Monster
    currently has on hand for this account (e.g. whether gmail.send is
    enabled) - never stored, so it can never go stale relative to the
    user's current OAuth grants."""
    # Cookie Monster can perform the verified deletion mechanism itself,
    # right now, after explicit user approval.
    AUTO_EXECUTABLE = "AUTO_EXECUTABLE"
    # Cookie Monster can prepare/initiate the request, but a human step
    # (login, MFA, CAPTCHA, identity verification, missing information,
    # or a not-yet-enabled optional consent) is unavoidably still required.
    USER_STEP_REQUIRED = "USER_STEP_REQUIRED"
    # Cookie Monster cannot safely/legitimately execute this mechanism at
    # all (this slice never does browser automation) - opens the verified
    # official route with instructions instead.
    MANUAL_HANDOFF = "MANUAL_HANDOFF"

    ALL = {AUTO_EXECUTABLE, USER_STEP_REQUIRED, MANUAL_HANDOFF}


class DeletionStatus:
    NOT_STARTED = "NOT_STARTED"
    METHOD_LOOKUP = "METHOD_LOOKUP"
    READY = "READY"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    IN_PROGRESS = "IN_PROGRESS"
    VERIFICATION_NEEDED = "VERIFICATION_NEEDED"
    MORE_INFO_REQUIRED = "MORE_INFO_REQUIRED"
    USER_ACTION_REQUIRED = "USER_ACTION_REQUIRED"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    UNKNOWN_RESPONSE = "UNKNOWN_RESPONSE"
    UNKNOWN = "UNKNOWN"
    # Research has been attempted config.DELETION_RECIPE_FAILURE_THRESHOLD+
    # times on a domain that's never verified - distinct from UNKNOWN
    # (which still means "will keep retrying, just hasn't succeeded yet").
    # Never terminal: automatic retries keep happening on the normal
    # cooldown, and the manual "Research deletion method" button still
    # works from here - see deletion_resolver.py.
    NO_METHOD_FOUND = "NO_METHOD_FOUND"
    # The company confirmed closing/deactivating the ACCOUNT the request
    # was about, but said nothing that confirms the underlying PERSONAL
    # DATA was deleted (see response_classify.py's
    # ACCOUNT_CLOSED_DATA_UNVERIFIED_PATTERNS and its module docstring for
    # why this must never be conflated with COMPLETED). Not terminal: the
    # 24h chase keeps going, explicitly asking whether personal data was
    # deleted and, if retained, what and why - see chase_engine.py.
    ACCOUNT_CLOSED_DATA_UNVERIFIED = "ACCOUNT_CLOSED_DATA_UNVERIFIED"

    ALL = {
        NOT_STARTED, METHOD_LOOKUP, READY, CONFIRMATION_REQUIRED, SUBMITTING,
        SUBMITTED, IN_PROGRESS, VERIFICATION_NEEDED, MORE_INFO_REQUIRED,
        USER_ACTION_REQUIRED, COMPLETED, REJECTED, FAILED, UNKNOWN_RESPONSE, UNKNOWN,
        NO_METHOD_FOUND, ACCOUNT_CLOSED_DATA_UNVERIFIED,
    }

    # Statuses that engine code only ever sets when it has real evidence -
    # never for "a page was opened" or "a button was clicked". COMPLETED is
    # deliberately NOT here: today it is exclusively a user self-report (see
    # deletion_engine.mark_user_completed) - that's the whole point of using
    # a different word than SUBMITTED for it.
    #
    # SUBMITTED is one exception worth knowing about: the one-time migration
    # of pre-recipe-table legacy data (app/migrations.py) also maps an old
    # free-text "submitted" self-report onto DeletionStatus.SUBMITTED, since
    # that's the closest existing meaning ("user says they sent/submitted
    # something") without inventing a new status. It tags the evidence
    # {"type": "user_reported", "legacy": true} specifically so it's never
    # confused with a real Gmail-send-verified SUBMITTED
    # ({"type": "gmail_send", ...}). Use is_system_verified() below instead
    # of checking status membership alone if that distinction matters to you.
    SYSTEM_VERIFIED = {
        SUBMITTED, IN_PROGRESS, VERIFICATION_NEEDED, MORE_INFO_REQUIRED, REJECTED,
        ACCOUNT_CLOSED_DATA_UNVERIFIED,
    }

    # Statuses for which a company reply might still arrive - the background
    # response-checker (deletion_response_tracker.py) only polls threads
    # whose company is in one of these. COMPLETED/REJECTED/FAILED are
    # terminal: polling stops once a request reaches one of them.
    ACTIVELY_MONITORED = {
        SUBMITTED, IN_PROGRESS, VERIFICATION_NEEDED, MORE_INFO_REQUIRED, UNKNOWN_RESPONSE,
        ACCOUNT_CLOSED_DATA_UNVERIFIED,
    }

    # A request in one of these is done, one way or another - nothing more
    # will ever change it. Used to gate actions that only make sense for a
    # still-open request, e.g. manually attaching a confirmation-email
    # thread (see main.py's /deletion/attach-thread routes) - there's
    # nothing to track a response for once a request is already resolved.
    TERMINAL = {COMPLETED, REJECTED, FAILED}


def is_system_verified(status: str, evidence: dict | None) -> bool:
    """The status-alone check (`status in DeletionStatus.SYSTEM_VERIFIED`) is
    correct for anything the engine itself produced, but not for a
    legacy-migrated row - a pre-recipe-table self-report can carry
    DeletionStatus.SUBMITTED without real evidence. This checks both."""
    if status not in DeletionStatus.SYSTEM_VERIFIED:
        return False
    return (evidence or {}).get("type") != "user_reported"


class RecipeStatus:
    """Status of a DeletionRecipe itself - distinct from a Company's
    deletion_status, which tracks THIS user's request progress."""
    VERIFIED = "VERIFIED"
    NEEDS_RESEARCH = "NEEDS_RESEARCH"  # research attempted, no official source found/verified
    UNKNOWN = "UNKNOWN"  # not yet attempted

    ALL = {VERIFIED, NEEDS_RESEARCH, UNKNOWN}


class RecipeOrigin:
    """How a DeletionRecipe came to exist - for audit/debugging, not trust
    (trust is RecipeStatus.VERIFIED + confidence, not origin)."""
    SEED = "seed"
    RESEARCHED = "researched"
    MANUAL = "manual"
    MIGRATED = "migrated"

    ALL = {SEED, RESEARCHED, MANUAL, MIGRATED}


class SourceType:
    """What kind of official page a recipe's source_url is - research priority
    order per the product spec."""
    OFFICIAL_PRIVACY_RIGHTS_PAGE = "official_privacy_rights_page"
    OFFICIAL_DELETION_PAGE = "official_deletion_page"
    OFFICIAL_PRIVACY_PORTAL = "official_privacy_portal"
    OFFICIAL_ACCOUNT_HELP = "official_account_deletion_help"
    OFFICIAL_PRIVACY_POLICY = "official_privacy_policy"
    OFFICIAL_SUPPORT_DOCS = "official_support_docs"
    THIRD_PARTY_VIA_OFFICIAL_LINK = "third_party_via_official_link"
    SEED = "seed"
    MANUAL = "manual"

    # Priority order used when multiple candidate sources are found - a
    # dedicated data-rights/deletion page beats a general privacy policy.
    PRIORITY = [
        OFFICIAL_PRIVACY_RIGHTS_PAGE, OFFICIAL_DELETION_PAGE, OFFICIAL_PRIVACY_PORTAL,
        OFFICIAL_ACCOUNT_HELP, OFFICIAL_PRIVACY_POLICY, OFFICIAL_SUPPORT_DOCS,
    ]


class ResearchFailureReason:
    """Short, safe category for why a research attempt didn't verify a
    recipe - stored in a DeletionEvent's evidence and shown (via a small
    friendly-label mapping in main.py) on the dashboard for UNKNOWN/
    NO_METHOD_FOUND companies. Deliberately just a category, never the raw
    exception text - see deletion_resolver.py's _run_research_only, which
    keeps any actual exception message in a separate, audit-only "detail"
    evidence field never surfaced to the UI."""
    NO_OFFICIAL_SOURCE_FOUND = "no_official_source_found"
    TECHNICAL_ERROR = "technical_error"
    # A Tier B (Brave) discovery resolved to the company's OWN domain, but
    # our fetcher couldn't reach it (401/403/429) - we never bypass that
    # block, but the URL is kept as manual-review evidence (see
    # deletion_research.SourceBlockedDiscovery) rather than silently
    # treated the same as "found nothing at all".
    SOURCE_BLOCKED = "source_blocked"
    # Brave's daily query budget (config.BRAVE_SEARCH_DAILY_QUERY_BUDGET)
    # was exhausted, so Tier B could not run this attempt. NOT a research
    # failure - deletion_resolver.py never counts this as an attempt
    # (research_attempts/last_attempted_at untouched), it just tries again
    # once budget is available. Paired with EventType.RESEARCH_DEFERRED,
    # never RESEARCH_FAILED.
    BUDGET_EXHAUSTED = "brave_budget_exhausted"

    ALL = {NO_OFFICIAL_SOURCE_FOUND, TECHNICAL_ERROR, SOURCE_BLOCKED, BUDGET_EXHAUSTED}


class EventType:
    """Deletion-attempt audit log event types (deletion_events.py)."""
    METHOD_DISCOVERED = "METHOD_DISCOVERED"
    RESEARCH_FAILED = "RESEARCH_FAILED"
    USER_CONFIRMED = "USER_CONFIRMED"
    EMAIL_SENT = "EMAIL_SENT"
    PORTAL_OPENED = "PORTAL_OPENED"
    COMPANY_ACKNOWLEDGED = "COMPANY_ACKNOWLEDGED"
    # A reply came in but the classifier could not confidently place it
    # into any known category (response_classify.py's UNKNOWN_RESPONSE) -
    # deliberately distinct from COMPANY_ACKNOWLEDGED, which asserts the
    # company acknowledged the request. This asserts nothing about what
    # the reply means; it only records that one arrived and needs review
    # (see deletion_response_tracker.py's _EVENT_TYPE_FOR_STATUS and its
    # reclassify_stale_unknown_response, which re-checks these later
    # against an improved classifier).
    UNCLASSIFIED_REPLY_RECEIVED = "UNCLASSIFIED_REPLY_RECEIVED"
    VERIFICATION_REQUESTED = "VERIFICATION_REQUESTED"
    ADDITIONAL_INFO_REQUESTED = "ADDITIONAL_INFO_REQUESTED"
    COMPLETION_CONFIRMED = "COMPLETION_CONFIRMED"
    USER_MARKED_COMPLETE = "USER_MARKED_COMPLETE"
    REQUEST_REJECTED = "REQUEST_REJECTED"
    FAILED = "FAILED"
    RETRY = "RETRY"
    # A background response-check attempt hit a technical error (Gmail API
    # error, network failure, ...). This is NOT a deletion-status change -
    # the underlying request status (SUBMITTED/IN_PROGRESS/etc.) is left
    # alone; only this audit event is recorded, and the thread is retried
    # later per the backoff policy. See deletion_response_tracker.py.
    RESPONSE_CHECK_FAILED = "RESPONSE_CHECK_FAILED"
    # The user manually associated an externally-submitted deletion request
    # (a web form, account setting, or privacy portal Cookie Monster never
    # sent itself) with a Gmail thread containing the company's confirmation
    # - see main.py's /deletion/attach-thread routes and
    # deletion_response_tracker.py's module docstring. Always source=USER:
    # the user is the one who reviewed and approved the specific message
    # this points to, not something Cookie Monster inferred on its own.
    # This event, by itself, never changes deletion_status - it only makes
    # Company.deletion_thread_id non-null so the EXISTING response tracker
    # can take over from here.
    THREAD_ASSOCIATED = "THREAD_ASSOCIATED"
    # A research attempt was skipped/postponed - not attempted at all -
    # because Brave's daily query budget was exhausted (see
    # ResearchFailureReason.BUDGET_EXHAUSTED). Distinct from
    # RESEARCH_FAILED on purpose: this must never count toward a
    # recipe's research_attempts or the NO_METHOD_FOUND threshold.
    RESEARCH_DEFERRED = "RESEARCH_DEFERRED"
    # Recorded BEFORE the risky network call (Gmail send), and committed
    # immediately - so an attempt is auditable even if the process crashes
    # before EMAIL_SENT/FAILED is ever recorded. Paired with
    # DeletionStatus.SUBMITTING - see deletion_engine.py and
    # recover_stuck_submitting().
    EXECUTION_STARTED = "EXECUTION_STARTED"
    # A previously-started execution attempt (EXECUTION_STARTED/SUBMITTING)
    # was found still unresolved at process startup - the process that
    # started it died before recording EMAIL_SENT or FAILED, so whether the
    # email actually went out is genuinely unknown. Recorded by
    # recover_stuck_submitting(); never auto-resolved either way.
    EXECUTION_INTERRUPTED = "EXECUTION_INTERRUPTED"
    # A follow-up reply was sent through the mailbox's explicit-approval
    # Respond flow (see app/mail.py) - distinct from EMAIL_SENT, which is
    # reserved for the ORIGINAL deletion request send in deletion_engine.py.
    # Always source=USER: nothing here is ever sent without a per-message
    # human approval click.
    MAIL_REPLY_SENT = "MAIL_REPLY_SENT"
    # An automated 24-hour chase follow-up was sent, in the same tracked
    # Gmail thread, via a deterministic template - never an LLM. evidence
    # carries {"attempt": N, "gmail_message_id":..., "gmail_thread_id":...}
    # and, when the send was discovered via reconciliation rather than
    # observed directly, {"recovered": true} - see chase_engine.py.
    FOLLOWUP_SENT = "FOLLOWUP_SENT"
    # The company confirmed the ACCOUNT was closed/deactivated, but said
    # nothing that confirms the underlying personal DATA was deleted - see
    # DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED and
    # response_classify.py's ACCOUNT_CLOSED_DATA_UNVERIFIED_PATTERNS.
    ACCOUNT_CLOSED_DATA_UNVERIFIED = "ACCOUNT_CLOSED_DATA_UNVERIFIED"

    ALL = {
        METHOD_DISCOVERED, RESEARCH_FAILED, USER_CONFIRMED, EMAIL_SENT, PORTAL_OPENED,
        COMPANY_ACKNOWLEDGED, UNCLASSIFIED_REPLY_RECEIVED, VERIFICATION_REQUESTED, ADDITIONAL_INFO_REQUESTED,
        COMPLETION_CONFIRMED, USER_MARKED_COMPLETE, REQUEST_REJECTED, FAILED, RETRY,
        RESPONSE_CHECK_FAILED, THREAD_ASSOCIATED, RESEARCH_DEFERRED,
        EXECUTION_STARTED, EXECUTION_INTERRUPTED, MAIL_REPLY_SENT,
        FOLLOWUP_SENT, ACCOUNT_CLOSED_DATA_UNVERIFIED,
    }


class WaitingOn:
    """Whose move it is on an active chase case - see chase_engine.py.
    Deliberately separate from DeletionStatus: DeletionStatus is the
    PRIVACY-facing outcome (what the company actually said), WaitingOn is
    the GAME/scheduling-facing question of who the chase clock is
    currently blocked on. Never conflate the two - see
    chase_engine.derive_waiting_on for the one place DeletionStatus (plus
    a narrow human-action signal) maps onto this."""
    COMPANY = "COMPANY"
    USER = "USER"
    ESCALATION_NEEDED = "ESCALATION_NEEDED"

    ALL = {COMPANY, USER, ESCALATION_NEEDED}


class EventSource:
    SYSTEM = "SYSTEM"
    USER = "USER"

    ALL = {SYSTEM, USER}
