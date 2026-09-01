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
import logging

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
from app.deletion_research import DeletionResearchProvider, SourceBlockedDiscovery, UnverifiedPortalDiscovery
from app.models import Company, DeletionRecipe
from app.research_search import BraveBudgetExhausted

# Explicit handler/level, matching main.py/deletion_queue.py's pattern, so
# budget-exhaustion deferrals are actually visible - "log/query-count
# usage without logging the API key" was an explicit product requirement.
_research_log = logging.getLogger("cookie_monster.deletion_resolver")
_research_log.setLevel(logging.INFO)
if not _research_log.handlers:
    _research_handler = logging.StreamHandler()
    _research_handler.setFormatter(logging.Formatter("%(asctime)s [cookie-monster-research] %(message)s"))
    _research_log.addHandler(_research_handler)
    _research_log.propagate = False

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
      (safe to show in the UI); extra may carry additional audit-only or
      manual-review context depending on the reason - a capped "detail"
      message for TECHNICAL_ERROR, or a "blocked_url"/"unverified_lead_url"
      for SOURCE_BLOCKED/an unverified Tier B portal (see
      deletion_research.py's SourceBlockedDiscovery/UnverifiedPortalDiscovery).
      BUDGET_EXHAUSTED carries no extra - it's a pure deferral, not a
      finding.
    """
    try:
        result = provider.research(company_name, domain)
    except BraveBudgetExhausted:
        return False, ResearchFailureReason.BUDGET_EXHAUSTED, {}
    except SourceBlockedDiscovery as exc:
        return False, ResearchFailureReason.SOURCE_BLOCKED, {"blocked_url": exc.url}
    except UnverifiedPortalDiscovery as exc:
        return False, ResearchFailureReason.NO_OFFICIAL_SOURCE_FOUND, {"unverified_lead_url": exc.url}
    except Exception as exc:  # noqa: BLE001 - any other provider failure is a technical error, not a crash
        return False, ResearchFailureReason.TECHNICAL_ERROR, {"detail": str(exc)[:200]}

    if result is None or not result.verified:
        return False, ResearchFailureReason.NO_OFFICIAL_SOURCE_FOUND, {}

    return True, None, {field: getattr(result, field) for field in _RECIPE_RESULT_FIELDS}


def _apply_research_outcome(
    recipe: DeletionRecipe, verified: bool, result_fields: dict, reason: str | None = None
) -> bool:
    """Applies a completed _run_research_only() outcome to the recipe ORM
    object. Must only ever be called from the thread/session that owns
    `recipe` - never from inside the concurrent research step itself.

    Returns True if this was counted as a real attempt (research_attempts
    incremented, last_attempted_at/status updated), False if it was a pure
    no-op deferral - today, only ResearchFailureReason.BUDGET_EXHAUSTED:
    the Brave daily budget was exhausted, so Tier B never even ran. Per
    product decision, that must NEVER count as a failed research attempt -
    the recipe is left completely untouched and simply becomes eligible
    again the next tick, once budget allows it.

    A failed (but counted) attempt on a recipe that was NOT already
    verified (a genuinely new/unknown company) correctly becomes
    NEEDS_RESEARCH. But a failed *re-verification* of an already-VERIFIED
    recipe (e.g. a stale seed re-checked with research disabled, or a
    transient fetch failure) must NOT destroy the last-known-good data -
    that would make clicking "Research deletion method" or a routine
    freshness re-check actively worse than doing nothing. It stays
    VERIFIED and gets retried at the next freshness/cooldown cycle
    instead.
    """
    if reason == ResearchFailureReason.BUDGET_EXHAUSTED:
        return False

    now = datetime.datetime.utcnow()
    was_verified = recipe.status == RecipeStatus.VERIFIED
    recipe.research_attempts += 1
    recipe.last_attempted_at = now

    if not verified:
        if not was_verified:
            recipe.status = RecipeStatus.NEEDS_RESEARCH
        return True

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
    return True


def _research_and_update_recipe(
    recipe: DeletionRecipe, company_name: str, provider: DeletionResearchProvider
) -> tuple[bool, str | None, dict, bool]:
    """Synchronous single-recipe path - used by resolve_deletion_method()
    (the dashboard's manual button, one row, fine to block briefly).
    Returns (verified, failure_reason, extra, counted) - failure_reason/
    extra are None/{} when verified; counted is False only for a
    budget-exhausted deferral (see _apply_research_outcome). See
    _run_research_only/_apply_research_outcome for the split version
    process_pending() uses to run several of these concurrently."""
    verified, reason, result_fields = _run_research_only(provider, company_name, recipe.domain)
    counted = _apply_research_outcome(recipe, verified, result_fields, reason)
    return verified, reason, ({} if verified else result_fields), counted


def _failure_evidence(recipe: DeletionRecipe, reason: str | None, extra: dict) -> dict:
    evidence = {"domain": recipe.domain, "research_attempts": recipe.research_attempts}
    if reason:
        evidence["reason"] = reason
    if extra.get("detail"):
        evidence["detail"] = extra["detail"]
    if extra.get("blocked_url"):
        evidence["blocked_url"] = extra["blocked_url"]
    if extra.get("unverified_lead_url"):
        evidence["unverified_lead_url"] = extra["unverified_lead_url"]
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
    original_status = company.deletion_status
    company.deletion_status = DeletionStatus.METHOD_LOOKUP
    db.commit()

    verified, reason, extra, counted = _research_and_update_recipe(recipe, company.name, provider)

    if not counted:
        # Budget-exhausted deferral: must NEVER count as a failed attempt -
        # restore exactly the status this company had before, touch nothing
        # else on the recipe, and record a distinct, non-failure audit event.
        company.deletion_status = original_status
        record_event(
            db, company.id, EventType.RESEARCH_DEFERRED,
            evidence={"domain": recipe.domain, "reason": reason},
        )
        db.commit()
        if reason == ResearchFailureReason.BUDGET_EXHAUSTED:
            _research_log.info(
                "Brave Search daily budget exhausted (limit=%d/day) - deferring research for %s, no attempt counted",
                config.BRAVE_SEARCH_DAILY_QUERY_BUDGET, recipe.domain,
            )
        return True

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
    original_statuses: dict[int, str] = {}
    for recipe in eligible:
        companies = db.query(Company).filter(Company.domain == recipe.domain).all()
        companies_by_recipe_id[recipe.id] = companies
        for company in companies:
            original_statuses[company.id] = company.deletion_status
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
    deferred_count = 0
    for recipe in eligible:
        verified, reason, extra = outcomes[recipe.id]
        counted = _apply_research_outcome(recipe, verified, extra, reason)
        companies = companies_by_recipe_id[recipe.id]

        if not counted:
            # Budget-exhausted deferral: restore each company's exact
            # pre-attempt status, touch nothing else on the recipe, and
            # record a distinct, non-failure audit event - never counted
            # toward `processed` (this attempt never really happened).
            for company in companies:
                company.deletion_status = original_statuses[company.id]
                record_event(
                    db, company.id, EventType.RESEARCH_DEFERRED,
                    evidence={"domain": recipe.domain, "reason": reason},
                )
            db.commit()
            deferred_count += 1
            continue

        for company in companies:
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

    if deferred_count:
        _research_log.info(
            "Brave Search daily budget exhausted (limit=%d/day) - deferred %d compan(y/ies) this tick, "
            "no attempts counted",
            config.BRAVE_SEARCH_DAILY_QUERY_BUDGET, deferred_count,
        )

    return processed
