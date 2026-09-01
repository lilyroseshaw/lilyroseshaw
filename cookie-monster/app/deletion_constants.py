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
    AUTOMATABLE = "AUTOMATABLE"
    PARTIALLY_AUTOMATABLE = "PARTIALLY_AUTOMATABLE"
    USER_ACTION_REQUIRED = "USER_ACTION_REQUIRED"
    UNKNOWN = "UNKNOWN"

    ALL = {AUTOMATABLE, PARTIALLY_AUTOMATABLE, USER_ACTION_REQUIRED, UNKNOWN}


class DeletionStatus:
    NOT_STARTED = "NOT_STARTED"
    METHOD_LOOKUP = "METHOD_LOOKUP"
    READY = "READY"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    VERIFICATION_NEEDED = "VERIFICATION_NEEDED"
    USER_ACTION_REQUIRED = "USER_ACTION_REQUIRED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"

    ALL = {
        NOT_STARTED, METHOD_LOOKUP, READY, CONFIRMATION_REQUIRED, SUBMITTING,
        SUBMITTED, VERIFICATION_NEEDED, USER_ACTION_REQUIRED, COMPLETED, FAILED, UNKNOWN,
    }

    # Only these may ever be set as a *direct* result of Cookie Monster itself performing
    # (not the user performing) an action - i.e. real system-side evidence exists.
    # COMPLETED reached via user self-report is recorded separately (see deletion_engine.py)
    # so "SUBMITTED" never means "a page was opened" or "a button was clicked".
    SYSTEM_VERIFIED = {SUBMITTED}
