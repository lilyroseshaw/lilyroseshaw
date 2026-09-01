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

    ALL = {
        NOT_STARTED, METHOD_LOOKUP, READY, CONFIRMATION_REQUIRED, SUBMITTING,
        SUBMITTED, IN_PROGRESS, VERIFICATION_NEEDED, MORE_INFO_REQUIRED,
        USER_ACTION_REQUIRED, COMPLETED, REJECTED, FAILED, UNKNOWN_RESPONSE, UNKNOWN,
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
    SYSTEM_VERIFIED = {SUBMITTED, IN_PROGRESS, VERIFICATION_NEEDED, MORE_INFO_REQUIRED, REJECTED}


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


class EventType:
    """Deletion-attempt audit log event types (deletion_events.py)."""
    METHOD_DISCOVERED = "METHOD_DISCOVERED"
    RESEARCH_FAILED = "RESEARCH_FAILED"
    USER_CONFIRMED = "USER_CONFIRMED"
    EMAIL_SENT = "EMAIL_SENT"
    PORTAL_OPENED = "PORTAL_OPENED"
    COMPANY_ACKNOWLEDGED = "COMPANY_ACKNOWLEDGED"
    VERIFICATION_REQUESTED = "VERIFICATION_REQUESTED"
    ADDITIONAL_INFO_REQUESTED = "ADDITIONAL_INFO_REQUESTED"
    COMPLETION_CONFIRMED = "COMPLETION_CONFIRMED"
    USER_MARKED_COMPLETE = "USER_MARKED_COMPLETE"
    REQUEST_REJECTED = "REQUEST_REJECTED"
    FAILED = "FAILED"
    RETRY = "RETRY"

    ALL = {
        METHOD_DISCOVERED, RESEARCH_FAILED, USER_CONFIRMED, EMAIL_SENT, PORTAL_OPENED,
        COMPANY_ACKNOWLEDGED, VERIFICATION_REQUESTED, ADDITIONAL_INFO_REQUESTED,
        COMPLETION_CONFIRMED, USER_MARKED_COMPLETE, REQUEST_REJECTED, FAILED, RETRY,
    }


class EventSource:
    SYSTEM = "SYSTEM"
    USER = "USER"

    ALL = {SYSTEM, USER}
