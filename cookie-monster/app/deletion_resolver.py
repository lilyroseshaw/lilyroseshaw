"""Applies (and, when needed, refreshes) a DeletionRecipe for a Company.

Three entry points, all DB-transactional:

- enqueue_pending(): fast, no network - ensures a recipe row exists for each
  company's domain and copies whatever's currently known onto the company.
  This is what runs inline during a Gmail scan (see aggregator.py), so
  scanning is never slowed down by research.
- process_pending(): the batch path a background worker calls - finds
  recipes that need (re)research, actually calls the DeletionResearchProvider
  (concurrently, bounded by config.DELETION_RESEARCH_MAX_CONCURRENCY, so one
  slow company can't stall the rest of the same batch), and syncs the result
  back to every Company row on that domain.
- backfill_all_companies(): startup safety net - reuses enqueue_pending over
  EVERY company, not just ones a scan just touched, so a company that
  existed before this pipeline (or hasn't been rescanned since) still gets a
  recipe stub and becomes visible to process_pending().

resolve_deletion_method() is a single-company synchronous variant of the
above, used by the dashboard's manual "Research deletion method" button -
fine to block briefly since it's one explicit click on one row.

DeletionRecipe is the shared cache (see models.py) - this module is what
makes the registry "grow automatically" instead of staying a static list.

Every path that starts a real research attempt sets the affected
company/companies to DeletionStatus.METHOD_LOOKUP first (and commits, so
it's visible immediately) and is guaranteed - via _run_research_only()
never raising, plus recover_stuck_method_lookup() as a startup-time safety
net for the one case no in-process handler can cover (the process itself
being killed mid-attempt) - to move back out of it, never leaving a company
stuck showing "Researching..." forever.
"""
import concurrent.futures
import datetime

from sqlalchemy.orm import Session

from app import config
from app.deletion_constants import (
    ActionCapability,
    DeletionMethod,
    DeletionStatus,
    EventType,
    RecipeOrigin,
    RecipeStatus,
    ResearchFailureReason,
)
from app.deletion_events import record_event
from app.deletion_research import DeletionResearchProvider
from app.models import Company, DeletionRecipe

# NO_METHOD_FOUND is included here (not just NOT_STARTED/METHOD_LOOKUP/
# UNKNOWN/READY) so a later successful research attempt - automatic retry
# or a manual "Research deletion method" click - can still move a company
# out of it into READY, or back to UNKNOWN if it's still short of the
# failure threshold. It's a resolvable state, never a terminal one.
_RESOLVABLE_COMPANY_STATUSES = {
    DeletionStatus.NOT_STARTED, DeletionStatus.METHOD_LOOKUP, DeletionStatus.UNKNOWN,
    DeletionStatus.READY, DeletionStatus.NO_METHOD_FOUND,
}

# The ResearchResult fields that get copied onto a DeletionRecipe when a
# result verifies - shared between the single-item and concurrent-batch
# paths so they can never drift apart.
_RECIPE_RESULT_FIELDS = [
    "method", "url", "email", "login_required", "email_verification_expected",
    "identity_verification_expected", "deletes_account", "known_consequences",
    "instructions", "source_url", "referring_official_url", "source_type", "confidence",
]


def _is_fresh(recipe: DeletionRecipe) -> bool:
    if recipe.status != RecipeStatus.VERIFIED:
        return False
    if recipe.expires_at is None:
        return False  # conservative: a verified recipe should always have an expiry
    return recipe.expires_at > datetime.datetime.utcnow()


def _retry_allowed(recipe: DeletionRecipe) -> bool:
    if recipe.last_attempted_at is None:
        return True
    cooldown = datetime.timedelta(days=config.DELETION_RECIPE_RETRY_COOLDOWN_DAYS)
    return datetime.datetime.utcnow() - recipe.last_attempted_at > cooldown


def _capability_from_method(method: str) -> str:
    if method == DeletionMethod.EMAIL_REQUEST:
        return ActionCapability.PARTIALLY_AUTOMATABLE  # draft always possible; auto-send needs gmail.send
    if method in (DeletionMethod.WEB_FORM, DeletionMethod.PRIVACY_PORTAL, DeletionMethod.ACCOUNT_SETTING):
        return ActionCapability.USER_ACTION_REQUIRED
    return ActionCapability.UNKNOWN


def get_or_create_recipe_stub(db: Session, domain: str) -> DeletionRecipe:
    recipe = db.query(DeletionRecipe).filter(DeletionRecipe.domain == domain).one_or_none()
    if recipe is None:
        recipe = DeletionRecipe(
            domain=domain, method=DeletionMethod.UNKNOWN, action_capability=ActionCapability.UNKNOWN,
            status=RecipeStatus.UNKNOWN,
        )
        db.add(recipe)
        db.flush()  # assign an id, cheap - no network involved
    return recipe


def _resolvable_status_for_recipe(recipe: DeletionRecipe) -> str:
    """What a company's deletion_status should become once a recipe's
    current state is applied - the single place this decision is made, so
    apply_recipe_to_company (every caller) and nothing else decides it."""
    if recipe.status == RecipeStatus.VERIFIED:
        return DeletionStatus.READY
    if recipe.status == RecipeStatus.NEEDS_RESEARCH and recipe.research_attempts >= config.DELETION_RECIPE_FAILURE_THRESHOLD:
        return DeletionStatus.NO_METHOD_FOUND
    return DeletionStatus.UNKNOWN


def apply_recipe_to_company(company: Company, recipe: DeletionRecipe) -> None:
    company.deletion_method = recipe.method
    company.deletion_action_capability = recipe.action_capability
    company.deletion_url = recipe.url
    company.deletion_email = recipe.email
    company.deletion_instructions = recipe.instructions
    company.deletion_source_url = recipe.source_url
    company.deletion_verified = recipe.status == RecipeStatus.VERIFIED
    company.deletion_last_checked = datetime.datetime.utcnow()
    if company.deletion_status in _RESOLVABLE_COMPANY_STATUSES:
        company.deletion_status = _resolvable_status_for_recipe(recipe)


def _run_research_only(provider: DeletionResearchProvider, company_name: str, domain: str) -> tuple[bool, str | None, dict]:
    """The stateless part of research - the actual network calls and
    extraction, no DB/ORM access at all. Safe to call from a worker thread
    (see process_pending's ThreadPoolExecutor) since it never touches a
    SQLAlchemy object. Never raises - any exception from the provider
    becomes a TECHNICAL_ERROR outcome instead, so one company's fetch/parse
    bug can never take down the batch or the tick.

    Returns (verified, failure_reason, extra):
    - verified=True: extra is the dict of ResearchResult fields to copy
      onto the recipe (see _RECIPE_RESULT_FIELDS).
    - verified=False: failure_reason is a ResearchFailureReason.* category
      (safe to show in the UI); extra optionally has a "detail" key with a
      capped, human-readable technical message (audit-log only, never
      shown in the UI as-is).
    """
    try:
        result = provider.research(company_name, domain)
    except Exception as exc:  # noqa: BLE001 - any provider failure is a technical error, not a crash
        return False, ResearchFailureReason.TECHNICAL_ERROR, {"detail": str(exc)[:200]}

    if result is None or not result.verified:
        return False, ResearchFailureReason.NO_OFFICIAL_SOURCE_FOUND, {}

    return True, None, {field: getattr(result, field) for field in _RECIPE_RESULT_FIELDS}


def _apply_research_outcome(recipe: DeletionRecipe, verified: bool, result_fields: dict) -> None:
    """Applies a completed _run_research_only() outcome to the recipe ORM
    object. Must only ever be called from the thread/session that owns
    `recipe` - never from inside the concurrent research step itself.

    A failed attempt on a recipe that was NOT already verified (a genuinely
    new/unknown company) correctly becomes NEEDS_RESEARCH. But a failed
    *re-verification* of an already-VERIFIED recipe (e.g. a stale seed
    re-checked with research disabled, or a transient fetch failure) must
    NOT destroy the last-known-good data - that would make clicking
    "Research deletion method" or a routine freshness re-check actively
    worse than doing nothing. It stays VERIFIED and gets retried at the
    next freshness/cooldown cycle instead.
    """
    now = datetime.datetime.utcnow()
    was_verified = recipe.status == RecipeStatus.VERIFIED
    recipe.research_attempts += 1
    recipe.last_attempted_at = now

    if not verified:
        if not was_verified:
            recipe.status = RecipeStatus.NEEDS_RESEARCH
        return

    changed = (
        recipe.method != result_fields["method"]
        or recipe.url != result_fields["url"]
        or recipe.email != result_fields["email"]
    )
    for field, value in result_fields.items():
        setattr(recipe, field, value)
    recipe.action_capability = _capability_from_method(result_fields["method"])
    recipe.status = RecipeStatus.VERIFIED
    recipe.origin = RecipeOrigin.RESEARCHED
    recipe.verified_at = now
    recipe.expires_at = now + datetime.timedelta(days=config.DELETION_RECIPE_FRESHNESS_DAYS)
    if changed:
        recipe.recipe_version += 1


def _research_and_update_recipe(
    recipe: DeletionRecipe, company_name: str, provider: DeletionResearchProvider
) -> tuple[bool, str | None, dict]:
    """Synchronous single-recipe path - used by resolve_deletion_method()
    (the dashboard's manual button, one row, fine to block briefly).
    Returns (verified, failure_reason, extra) - failure_reason/extra are
    None/{} when verified. See _run_research_only/_apply_research_outcome
    for the split version process_pending() uses to run several of these
    concurrently."""
    verified, reason, result_fields = _run_research_only(provider, company_name, recipe.domain)
    _apply_research_outcome(recipe, verified, result_fields)
    return verified, reason, ({} if verified else result_fields)


def _failure_evidence(recipe: DeletionRecipe, reason: str | None, extra: dict) -> dict:
    evidence = {"domain": recipe.domain, "research_attempts": recipe.research_attempts}
    if reason:
        evidence["reason"] = reason
    if extra.get("detail"):
        evidence["detail"] = extra["detail"]
    return evidence


def enqueue_pending(db: Session, companies: list[Company]) -> None:
    """Fast, no network. Called inline from aggregator.store() right after a
    scan - every company gets whatever recipe currently exists (possibly a
    bare UNKNOWN stub) applied immediately; anything not yet verified is
    picked up later by process_pending()."""
    for company in companies:
        recipe = get_or_create_recipe_stub(db, company.domain)
        apply_recipe_to_company(company, recipe)


def backfill_all_companies(db: Session) -> int:
    """Startup safety net (see main.py's on_startup): ensures EVERY company
    - not just ones a scan happens to have touched - has at least a recipe
    stub and reflects whatever's currently known for its domain. Covers
    companies that existed before this pipeline shipped, or that simply
    haven't been rescanned since. Idempotent and non-destructive: reuses
    enqueue_pending's existing get-or-create + apply logic, which never
    creates a duplicate recipe for a domain that already has one and never
    overwrites a VERIFIED recipe's own fields or downgrades it."""
    companies = db.query(Company).all()
    if not companies:
        return 0
    enqueue_pending(db, companies)
    db.commit()
    return len(companies)


def recover_stuck_method_lookup(db: Session) -> int:
    """METHOD_LOOKUP is meant to be a brief, transient, in-flight marker -
    every normal code path (resolve_deletion_method, process_pending) is
    guaranteed to move a company back out of it, success or failure, since
    _run_research_only() never raises. The one gap no in-process handler
    can cover is the process itself being killed mid-attempt (power loss,
    a forced restart) - called once at startup, alongside the backfill, so
    a company never shows "Researching..." forever because of a restart
    that happened at exactly the wrong moment."""
    stuck = db.query(Company).filter(Company.deletion_status == DeletionStatus.METHOD_LOOKUP).all()
    for company in stuck:
        company.deletion_status = DeletionStatus.UNKNOWN
    if stuck:
        db.commit()
    return len(stuck)


def resolve_deletion_method(
    db: Session, company: Company, provider: DeletionResearchProvider, force: bool = False
) -> bool:
    """Single-company synchronous resolve - used by the dashboard's manual
    'Research deletion method' button. Returns True if research actually ran
    (as opposed to a cache hit)."""
    recipe = get_or_create_recipe_stub(db, company.domain)

    if not force:
        if _is_fresh(recipe):
            apply_recipe_to_company(company, recipe)
            db.commit()
            return False
        if recipe.status == RecipeStatus.NEEDS_RESEARCH and not _retry_allowed(recipe):
            apply_recipe_to_company(company, recipe)
            db.commit()
            return False

    # Visible immediately - a concurrent dashboard load sees "Researching..."
    # for the duration of this call, not the previous stale state.
    company.deletion_status = DeletionStatus.METHOD_LOOKUP
    db.commit()

    verified, reason, extra = _research_and_update_recipe(recipe, company.name, provider)
    apply_recipe_to_company(company, recipe)
    record_event(
        db, company.id,
        EventType.METHOD_DISCOVERED if verified else EventType.RESEARCH_FAILED,
        evidence=(
            {"domain": recipe.domain, "confidence": recipe.confidence, "source_url": recipe.source_url}
            if verified else _failure_evidence(recipe, reason, extra)
        ),
        recipe_id=recipe.id, recipe_version=recipe.recipe_version,
    )
    db.commit()
    return True


def process_pending(db: Session, provider: DeletionResearchProvider, limit: int | None = None) -> int:
    """Batch path for the background queue worker (app/deletion_queue.py).
    Finds recipes needing (re)research, respecting the retry cooldown, and
    syncs results to every Company row on that domain. Returns how many
    recipes were actually processed (attempted, not necessarily verified).

    The actual research calls for this batch run CONCURRENTLY, bounded by
    config.DELETION_RESEARCH_MAX_CONCURRENCY, so one slow/unresponsive
    company's site can't stall the rest of the same batch - each call is
    still capped at config.RESEARCH_HTTP_TIMEOUT_SECONDS regardless. All
    DB reads/writes stay strictly sequential in this one session/thread;
    only the stateless network+extraction step (_run_research_only) is
    ever handed to a worker thread.
    """
    limit = limit or config.DELETION_QUEUE_BATCH_SIZE
    now = datetime.datetime.utcnow()

    candidates = (
        db.query(DeletionRecipe)
        .filter(
            (DeletionRecipe.status != RecipeStatus.VERIFIED)
            | (DeletionRecipe.expires_at.is_(None))
            | (DeletionRecipe.expires_at <= now)
        )
        .order_by(DeletionRecipe.last_attempted_at.asc())  # NULLs (never attempted) sort first in SQLite
        .limit(limit * 3)  # over-fetch since some will be cooldown-blocked
        .all()
    )

    eligible: list[DeletionRecipe] = []
    for recipe in candidates:
        if len(eligible) >= limit:
            break
        if recipe.status == RecipeStatus.NEEDS_RESEARCH and not _retry_allowed(recipe):
            continue
        eligible.append(recipe)

    if not eligible:
        return 0

    companies_by_recipe_id: dict[int, list[Company]] = {}
    for recipe in eligible:
        companies = db.query(Company).filter(Company.domain == recipe.domain).all()
        companies_by_recipe_id[recipe.id] = companies
        for company in companies:
            company.deletion_status = DeletionStatus.METHOD_LOOKUP
    db.commit()  # visible immediately, and never left stuck if the step below dies

    company_names = {
        recipe.id: (companies_by_recipe_id[recipe.id][0].name if companies_by_recipe_id[recipe.id] else recipe.domain)
        for recipe in eligible
    }
    max_workers = max(1, min(config.DELETION_RESEARCH_MAX_CONCURRENCY, len(eligible)))
    outcomes: dict[int, tuple[bool, str | None, dict]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_run_research_only, provider, company_names[recipe.id], recipe.domain): recipe.id
            for recipe in eligible
        }
        for future in concurrent.futures.as_completed(futures):
            outcomes[futures[future]] = future.result()

    processed = 0
    for recipe in eligible:
        verified, reason, extra = outcomes[recipe.id]
        _apply_research_outcome(recipe, verified, extra)
        for company in companies_by_recipe_id[recipe.id]:
            apply_recipe_to_company(company, recipe)
            record_event(
                db, company.id,
                EventType.METHOD_DISCOVERED if verified else EventType.RESEARCH_FAILED,
                evidence=(
                    {"domain": recipe.domain, "confidence": recipe.confidence}
                    if verified else _failure_evidence(recipe, reason, extra)
                ),
                recipe_id=recipe.id, recipe_version=recipe.recipe_version,
            )
        db.commit()
        processed += 1

    return processed
