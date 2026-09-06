"""Executes (or routes) a deletion request for a confirmed company - the
generic execution engine (Find -> Delete -> Track -> ...). This slice
covers Delete only: turning a VERIFIED recipe into an actual outcome, with
strict status/evidence integrity.

Execution capability model (see ExecutionCapability in deletion_constants.py):
  AUTO_EXECUTABLE    - Cookie Monster can perform the mechanism itself,
                        right now, after explicit approval.
  USER_STEP_REQUIRED - Cookie Monster can prepare/initiate it, but a human
                        step is unavoidably still required (login, MFA,
                        CAPTCHA, identity verification, missing
                        information, or an optional consent not yet
                        granted).
  MANUAL_HANDOFF      - Cookie Monster cannot safely/legitimately execute
                        this mechanism at all - opens the verified official
                        route with instructions.

classify_execution_capability() is the ONE place this decision is made,
driven only by the company's VERIFIED recipe data and what Cookie Monster
currently has on hand for this account (Gmail scopes) - never by
company-name hardcoding. Both the preview endpoint (what the approval UI
shows) and execute_deletion() (what actually happens) call it, so they can
never drift apart.

What "execute" means depends on deletion_method:

- EMAIL_REQUEST: the only AUTO_EXECUTABLE path in this slice, because
  Gmail send already exists and is the cleanest truly automatable
  mechanism. Sends for real, via the user's own Gmail, ONLY if: the recipe
  is verified, every required_request_fields entry is something Cookie
  Monster can actually supply (see _AUTO_SUPPLIABLE_IDENTITY_FIELDS -
  never guessed), and the separate gmail.send consent
  (google_oauth.has_send_scope) has been granted. Missing any of those
  degrades cleanly to USER_STEP_REQUIRED (a draft to send yourself) -
  never a guess, never a silent skip.
- WEB_FORM / PRIVACY_PORTAL / ACCOUNT_SETTING / MANUAL: MANUAL_HANDOFF in
  this slice (see README "Deletion automation - what's real" for why:
  these almost always sit behind login/CAPTCHA/email verification, and
  automating around that is exactly what this project's own safety rules
  forbid). Returns the official URL and sets USER_ACTION_REQUIRED; the
  user completes it and self-reports back via mark_user_completed().
  Opening the page is never, by itself, SUBMITTED.
- API: wired for completeness; inert until a recipe actually sets
  method=API with a real, documented endpoint (none do yet).
- UNKNOWN: never submits anything.

Nothing here ever marks DeletionStatus.SUBMITTED without concrete evidence
(a Gmail message id) - see DeletionStatus.SYSTEM_VERIFIED. Every
transition is also recorded in the deletion_events audit log (app/
deletion_events.py), not just the current status column - including an
EXECUTION_STARTED event committed BEFORE the Gmail call, so a crash mid-
send is still auditable (see recover_stuck_submitting below).
"""
import dataclasses
import datetime
import threading

from sqlalchemy.orm import Session

from app import chase_engine, google_oauth
from app.deletion_constants import (
    DeletionMethod,
    DeletionStatus,
    EventSource,
    EventType,
    ExecutionCapability,
)
from app.deletion_events import record_event
from app.models import Company, DeletionRecipe

ALREADY_DONE_STATUSES = {DeletionStatus.SUBMITTED, DeletionStatus.COMPLETED}

# The only identity information Cookie Monster ever has on hand automatically
# is the connected Gmail address - never a name, phone number, physical
# address, order number, or any credential. A recipe's required_request_fields
# entry only counts as "satisfiable" if it's one of these; anything else
# means the engine must degrade to USER_STEP_REQUIRED rather than guess or
# fabricate a value. Deliberately never includes anything credential-shaped
# (password, MFA code, government ID, SSN, etc.) - those could never be
# added here even if a recipe asked for them.
_AUTO_SUPPLIABLE_IDENTITY_FIELDS = {"account_email", "email", "gmail_address"}

# In-process per-company execution lock - a double-click on "Continue with
# deletion", or two browser tabs approving the same company close together,
# must never result in two Gmail sends. Same pattern/tradeoffs as
# deletion_resolver.py's per-domain research guard: in-memory only (single-
# process prototype), scoped per company so unrelated companies still
# execute independently.
_in_flight_executions: set[int] = set()
_in_flight_lock = threading.Lock()


def _try_start_execution(company_id: int) -> bool:
    with _in_flight_lock:
        if company_id in _in_flight_executions:
            return False
        _in_flight_executions.add(company_id)
        return True


def _finish_execution(company_id: int) -> None:
    with _in_flight_lock:
        _in_flight_executions.discard(company_id)


class DuplicateRequestWarning(Exception):
    """Raised instead of executing again when a request already has a
    terminal, evidenced outcome - the caller should surface this as a
    confirmation prompt, not silently resend."""

    def __init__(self, company: Company):
        self.company = company
        super().__init__(
            f"A deletion request for {company.name} was already marked "
            f"{company.deletion_status} on "
            f"{(company.deletion_completed_at or company.deletion_requested_at)}."
        )


class UnverifiedRecipeError(ValueError):
    """Raised when execution is attempted without a verified recipe backing
    it. Discovered/researched is not enough - only Company.deletion_verified
    (set exclusively from a RecipeStatus.VERIFIED recipe application - see
    deletion_resolver.apply_recipe_to_company) authorizes execution. This is
    never weakened, even for force_resend."""


class ExecutionInFlightError(Exception):
    """Raised when another execution attempt for this exact company is
    already running in this process (double-click / concurrent approval).
    The caller should treat this as a no-op, not an error to surface -
    the in-flight attempt will resolve shortly regardless of who started it."""


@dataclasses.dataclass
class ExecutionPlan:
    """The full answer to 'what would happen if the user approved this
    right now' - computed fresh every time (never cached/stored), so it can
    never go stale relative to the user's current OAuth grants. Used by
    BOTH the preview/approval UI and execute_deletion() itself, so they can
    never disagree about what's about to happen."""

    capability: str  # ExecutionCapability.*
    method: str  # DeletionMethod.*
    reason: str | None  # why not AUTO_EXECUTABLE - shown in the approval UI; None when AUTO_EXECUTABLE
    consequences: str | None  # known_consequences from the recipe, if any
    draft: dict | None  # {"to","subject","body"} for EMAIL_REQUEST; None otherwise
    missing_identity_fields: list[str]
    url: str | None  # verified official page, for MANUAL_HANDOFF


def _missing_identity_fields(recipe: DeletionRecipe | None) -> list[str]:
    if recipe is None:
        return []
    return [f for f in (recipe.required_request_fields or []) if f not in _AUTO_SUPPLIABLE_IDENTITY_FIELDS]


def build_structured_email(company: Company, recipe: DeletionRecipe | None, user_email: str) -> dict:
    """Builds the exact outgoing email from structured fields only - never
    arbitrary LLM prose (the LLM tier elsewhere in this app, see
    research_extract.py, is only ever used to LOCATE facts on a fetched
    page, never to compose outbound text). recipe may be None (legacy
    companies whose deletion_email/deletion_method were set before the
    recipe table existed) - falls back to Company's own cached fields."""
    to_email = (recipe.email if recipe else None) or company.deletion_email or f"privacy@{company.domain}"
    subject = (recipe.required_subject if recipe else None) or f"CCPA/CPRA Deletion Request - {company.name}"
    lines = [
        f"To the Privacy Team at {company.name},",
        "",
        "I am requesting deletion of my personal information pursuant to "
        "applicable California privacy law (CCPA/CPRA).",
        "",
        "Please confirm receipt of this request and provide instructions "
        "for completing any required identity verification.",
        "",
        f"This request relates to the account/contact associated with: {user_email}",
    ]
    jurisdiction_notes = recipe.jurisdiction_notes if recipe else None
    if jurisdiction_notes:
        lines += ["", jurisdiction_notes]
    lines += ["", "Thank you."]
    return {"to": to_email, "subject": subject, "body": "\n".join(lines)}


# Kept as a thin alias - main.py's existing /deletion/preview route and
# older tests call this name; it's now a special case of the recipe-aware
# builder above (recipe=None).
def build_email_draft(company: Company, user_email: str) -> dict:
    return build_structured_email(company, None, user_email)


def _get_recipe(db: Session, company: Company) -> DeletionRecipe | None:
    return db.query(DeletionRecipe).filter(DeletionRecipe.domain == company.domain).one_or_none()


def classify_execution_capability(db: Session, company: Company) -> ExecutionPlan:
    """The single source of truth for what happens on approval - driven
    only by the VERIFIED recipe (company.deletion_verified must already be
    True; callers that haven't checked that should call
    require_verified_recipe first) plus what Cookie Monster currently has
    on hand for this account. Never company-name hardcoding."""
    recipe = _get_recipe(db, company)
    method = company.deletion_method

    if method == DeletionMethod.EMAIL_REQUEST:
        gmail_address = google_oauth.get_connected_address(db)
        missing = _missing_identity_fields(recipe)
        draft = build_structured_email(company, recipe, gmail_address or "your Gmail address")

        if missing:
            return ExecutionPlan(
                capability=ExecutionCapability.USER_STEP_REQUIRED,
                method=method,
                reason=f"Missing: {', '.join(missing)}. Add it to the draft below before sending it yourself.",
                consequences=recipe.known_consequences if recipe else None,
                draft=draft,
                missing_identity_fields=missing,
                url=None,
            )
        if not google_oauth.has_send_scope(db):
            return ExecutionPlan(
                capability=ExecutionCapability.USER_STEP_REQUIRED,
                method=method,
                reason="Automatic sending isn't enabled yet - turn it on to have Baker's Dozen send this for you.",
                consequences=recipe.known_consequences if recipe else None,
                draft=draft,
                missing_identity_fields=[],
                url=None,
            )
        return ExecutionPlan(
            capability=ExecutionCapability.AUTO_EXECUTABLE,
            method=method,
            reason=None,
            consequences=recipe.known_consequences if recipe else None,
            draft=draft,
            missing_identity_fields=[],
            url=None,
        )

    if method in (DeletionMethod.WEB_FORM, DeletionMethod.PRIVACY_PORTAL, DeletionMethod.ACCOUNT_SETTING):
        return ExecutionPlan(
            capability=ExecutionCapability.MANUAL_HANDOFF,
            method=method,
            reason="Requires a manual step on their own site.",
            consequences=recipe.known_consequences if recipe else None,
            draft=None,
            missing_identity_fields=[],
            url=company.deletion_url,
        )

    # MANUAL, API (no real endpoint exists yet), UNKNOWN, or anything else -
    # never fabricate an execution path for a method the engine doesn't
    # have a real mechanism for.
    return ExecutionPlan(
        capability=ExecutionCapability.MANUAL_HANDOFF,
        method=method,
        reason="No automatic execution path exists for this method yet.",
        consequences=recipe.known_consequences if recipe else None,
        draft=None,
        missing_identity_fields=[],
        url=company.deletion_url,
    )


def require_verified_recipe(company: Company) -> None:
    """Execution must always be refused - not guessed at - when the current
    deletion_method/email/url weren't copied from a VERIFIED recipe.
    Company.deletion_verified is set exclusively by
    deletion_resolver.apply_recipe_to_company from RecipeStatus.VERIFIED,
    so it's already the correct, existing signal for this - see
    migrations.py's own use of the same condition for its legacy backfill."""
    if not company.deletion_verified:
        raise UnverifiedRecipeError(
            f"No verified deletion recipe for {company.name} ({company.domain}) - execution refused."
        )


def execute_deletion(db: Session, company: Company, force_resend: bool = False) -> Company:
    """Runs (or routes) one deletion request. Raises DuplicateRequestWarning
    if the company already has a terminal, evidenced outcome and
    force_resend is False; UnverifiedRecipeError if there's no verified
    recipe backing this method; ExecutionInFlightError if another attempt
    for this exact company is already running. The caller should re-confirm
    with the user before passing force_resend=True, and treat
    ExecutionInFlightError as a no-op, not a user-facing failure."""
    if company.status != "confirmed":
        raise ValueError("Only confirmed companies can have a deletion request executed")

    if company.deletion_status in ALREADY_DONE_STATUSES and not force_resend:
        raise DuplicateRequestWarning(company)

    require_verified_recipe(company)

    if not _try_start_execution(company.id):
        raise ExecutionInFlightError(f"An execution attempt for {company.name} is already in progress.")

    try:
        now = datetime.datetime.utcnow()
        plan = classify_execution_capability(db, company)
        record_event(
            db, company.id, EventType.USER_CONFIRMED, source=EventSource.USER,
            evidence={
                "method": company.deletion_method,
                "forced_resend": force_resend,
                "capability": plan.capability,
                "reason": plan.reason,
            },
        )

        if company.deletion_method == DeletionMethod.EMAIL_REQUEST:
            if plan.capability == ExecutionCapability.AUTO_EXECUTABLE:
                _execute_email_request(db, company, plan, now)
            else:
                _draft_only_email_request(db, company, plan, now)
        elif company.deletion_method in (
            DeletionMethod.WEB_FORM,
            DeletionMethod.PRIVACY_PORTAL,
            DeletionMethod.ACCOUNT_SETTING,
            DeletionMethod.MANUAL,
        ):
            _route_to_user_action(db, company, now)
        elif company.deletion_method == DeletionMethod.API:
            # No recipe sets method=API with a real endpoint yet - this
            # branch exists so the state machine is complete, but is
            # unreachable in practice today. Never fabricate an API call.
            company.deletion_status = DeletionStatus.FAILED
            company.deletion_error = "No supported API integration exists for this company yet."
            record_event(db, company.id, EventType.FAILED, evidence={"reason": company.deletion_error})
        else:
            company.deletion_status = DeletionStatus.UNKNOWN
            company.deletion_error = "Deletion method not verified yet."

        db.commit()
        return company
    finally:
        _finish_execution(company.id)


def _draft_only_email_request(db: Session, company: Company, plan: ExecutionPlan, now: datetime.datetime) -> None:
    """USER_STEP_REQUIRED path: prepares the exact email but never sends
    it - either a required field is missing, or gmail.send isn't enabled.
    Never claims execution happened."""
    company.deletion_status = DeletionStatus.USER_ACTION_REQUIRED
    company.deletion_url = None
    company.deletion_error = None
    company.deletion_instructions = plan.reason
    company.deletion_evidence = {
        "type": "draft_only",
        "to": plan.draft["to"],
        "subject": plan.draft["subject"],
        "missing_identity_fields": plan.missing_identity_fields,
        "prepared_at": now.isoformat(),
    }


def _execute_email_request(db: Session, company: Company, plan: ExecutionPlan, now: datetime.datetime) -> None:
    """AUTO_EXECUTABLE path: recipe verified, no missing identity fields,
    gmail.send granted - actually sends. SUBMITTING is set and committed
    BEFORE the Gmail call (with an EXECUTION_STARTED audit event) so a
    process crash mid-send leaves real evidence behind instead of silence -
    see recover_stuck_submitting()."""
    company.deletion_status = DeletionStatus.SUBMITTING
    record_event(
        db, company.id, EventType.EXECUTION_STARTED,
        evidence={"to": plan.draft["to"], "subject": plan.draft["subject"]},
    )
    db.commit()

    try:
        creds = google_oauth.load_credentials(db)
        if creds is None:
            raise RuntimeError("Gmail is not connected")
        response = google_oauth.send_email(creds, plan.draft["to"], plan.draft["subject"], plan.draft["body"])
    except Exception as exc:  # noqa: BLE001 - any send failure lands here, never silently
        company.deletion_status = DeletionStatus.FAILED
        company.deletion_error = f"Failed to send deletion request email: {exc}"
        record_event(db, company.id, EventType.FAILED, evidence={"reason": str(exc)[:300]})
        return

    message_id = response.get("id")
    thread_id = response.get("threadId")

    company.deletion_status = DeletionStatus.SUBMITTED
    company.deletion_requested_at = now
    company.deletion_error = None
    # The response tracker (deletion_response_tracker.py) picks this company
    # up automatically the moment deletion_thread_id is set and
    # deletion_status is in DeletionStatus.ACTIVELY_MONITORED (SUBMITTED is)
    # - no separate wiring needed, and it only ever reads this ONE thread.
    company.deletion_thread_id = thread_id
    company.deletion_evidence = {
        "type": "gmail_send",
        "gmail_message_id": message_id,
        "gmail_thread_id": thread_id,
        "sent_to": plan.draft["to"],
        "subject": plan.draft["subject"],
        "sent_at": now.isoformat(),
    }
    record_event(
        db, company.id, EventType.EMAIL_SENT,
        evidence={"gmail_message_id": message_id, "gmail_thread_id": thread_id, "sent_to": plan.draft["to"]},
    )
    # Approving this send is what authorizes the 24-hour automatic chase
    # (see chase_engine.py) - disclosed once at approval time, never a
    # per-follow-up consent prompt.
    chase_engine.on_request_sent(company, now)


def _route_to_user_action(db: Session, company: Company, now: datetime.datetime) -> None:
    """MANUAL_HANDOFF path (WEB_FORM/PRIVACY_PORTAL/ACCOUNT_SETTING/MANUAL).
    Opening the verified official page is never, by itself, SUBMITTED -
    only the user completing it and self-reporting (mark_user_completed)
    produces a status change from here."""
    if not company.deletion_url:
        company.deletion_status = DeletionStatus.UNKNOWN
        company.deletion_error = "No verified deletion page is on file for this company."
        return
    company.deletion_status = DeletionStatus.USER_ACTION_REQUIRED
    if company.deletion_requested_at is None:
        company.deletion_requested_at = now
    record_event(db, company.id, EventType.PORTAL_OPENED, evidence={"url": company.deletion_url})


def mark_user_completed(db: Session, company: Company, evidence_note: str | None = None) -> Company:
    """Self-report path: the user did the WEB_FORM/ACCOUNT_SETTING/manual-email
    step themselves outside Cookie Monster and is telling us it's done. This
    is recorded as COMPLETED (self-reported), never as SUBMITTED - Cookie
    Monster has no independent proof, and this project's own rules require
    we never claim otherwise."""
    company.deletion_status = DeletionStatus.COMPLETED
    company.deletion_completed_at = datetime.datetime.utcnow()
    note = (evidence_note or "").strip()[:300] or None
    company.deletion_evidence = {
        "type": "user_reported",
        "note": note,
        "reported_at": company.deletion_completed_at.isoformat(),
    }
    record_event(
        db, company.id, EventType.USER_MARKED_COMPLETE, source=EventSource.USER,
        evidence={"note": note} if note else {},
    )
    db.commit()
    return company


def recover_stuck_submitting(db: Session) -> int:
    """Startup recovery: a company left in DeletionStatus.SUBMITTING means
    the process that started sending its deletion email died before
    recording EMAIL_SENT or FAILED - whether Gmail actually sent it is
    genuinely unknown (the HTTP response could have been lost after Gmail
    already processed the request). Unlike recover_stuck_method_lookup
    (deletion_resolver.py), this deliberately does NOT reset to a freely-
    retryable state - auto-resending here risks a real duplicate email to
    the company. Instead it moves to USER_ACTION_REQUIRED with an explicit
    warning, records EXECUTION_INTERRUPTED, and leaves the decision to the
    user (who can check their Sent folder before retrying). Idempotent -
    safe to run on every startup."""
    stuck = db.query(Company).filter(Company.deletion_status == DeletionStatus.SUBMITTING).all()
    for company in stuck:
        company.deletion_status = DeletionStatus.USER_ACTION_REQUIRED
        company.deletion_error = (
            "An earlier attempt to send this deletion request was interrupted before Cookie "
            "Monster could confirm whether it actually sent. Check your Gmail Sent folder for "
            f"a message to {company.deletion_email or 'this company'} before retrying, to avoid "
            "sending it twice."
        )
        record_event(db, company.id, EventType.EXECUTION_INTERRUPTED, evidence={"previous_status": "SUBMITTING"})
    if stuck:
        db.commit()
    return len(stuck)
