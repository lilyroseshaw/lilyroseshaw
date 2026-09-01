"""Applies (and, when needed, refreshes) a DeletionRecipe for a Company.

Two entry points, both DB-transactional:

- enqueue_pending(): fast, no network - ensures a recipe row exists for each
  company's domain and copies whatever's currently known onto the company.
  This is what runs inline during a Gmail scan (see aggregator.py), so
  scanning is never slowed down by research.
- process_pending(): the batch path a background worker calls - finds
  recipes that need (re)research, actually calls the DeletionResearchProvider,
  and syncs the result back to every Company row on that domain.

resolve_deletion_method() is a single-company synchronous variant of the
above two, used by the dashboard's manual "Research deletion method" button -
fine to block briefly since it's one explicit click on one row.

DeletionRecipe is the shared cache (see models.py) - this module is what
makes the registry "grow automatically" instead of staying a static list.
"""
import datetime

from sqlalchemy.orm import Session

from app import config
from app.deletion_constants import ActionCapability, DeletionMethod, DeletionStatus, EventType, RecipeOrigin, RecipeStatus
from app.deletion_events import record_event
from app.deletion_research import DeletionResearchProvider
from app.models import Company, DeletionRecipe
from app.research_types import ResearchResult

_RESOLVABLE_COMPANY_STATUSES = {
    DeletionStatus.NOT_STARTED, DeletionStatus.METHOD_LOOKUP, DeletionStatus.UNKNOWN, DeletionStatus.READY,
}


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


def _capability_from_result(result: ResearchResult) -> str:
    if result.method == DeletionMethod.EMAIL_REQUEST:
        return ActionCapability.PARTIALLY_AUTOMATABLE  # draft always possible; auto-send needs gmail.send
    if result.method in (DeletionMethod.WEB_FORM, DeletionMethod.PRIVACY_PORTAL, DeletionMethod.ACCOUNT_SETTING):
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
        company.deletion_status = DeletionStatus.READY if recipe.status == RecipeStatus.VERIFIED else DeletionStatus.UNKNOWN


def _research_and_update_recipe(recipe: DeletionRecipe, company_name: str, provider: DeletionResearchProvider) -> bool:
    """Runs research and updates the recipe row in place (caller commits).
    Returns True if this call found a freshly-verified result.

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

    result = provider.research(company_name, recipe.domain)
    if result is None or not result.verified:
        if not was_verified:
            recipe.status = RecipeStatus.NEEDS_RESEARCH
        return False

    changed = recipe.method != result.method or recipe.url != result.url or recipe.email != result.email

    recipe.method = result.method
    recipe.action_capability = _capability_from_result(result)
    recipe.url = result.url
    recipe.email = result.email
    recipe.login_required = result.login_required
    recipe.email_verification_expected = result.email_verification_expected
    recipe.identity_verification_expected = result.identity_verification_expected
    recipe.deletes_account = result.deletes_account
    recipe.known_consequences = result.known_consequences
    recipe.instructions = result.instructions
    recipe.source_url = result.source_url
    recipe.referring_official_url = result.referring_official_url
    recipe.source_type = result.source_type
    recipe.confidence = result.confidence
    recipe.status = RecipeStatus.VERIFIED
    recipe.origin = RecipeOrigin.RESEARCHED
    recipe.verified_at = now
    recipe.expires_at = now + datetime.timedelta(days=config.DELETION_RECIPE_FRESHNESS_DAYS)
    if changed:
        recipe.recipe_version += 1
    return True


def enqueue_pending(db: Session, companies: list[Company]) -> None:
    """Fast, no network. Called inline from aggregator.store() right after a
    scan - every company gets whatever recipe currently exists (possibly a
    bare UNKNOWN stub) applied immediately; anything not yet verified is
    picked up later by process_pending()."""
    for company in companies:
        recipe = get_or_create_recipe_stub(db, company.domain)
        apply_recipe_to_company(company, recipe)


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

    verified = _research_and_update_recipe(recipe, company.name, provider)
    apply_recipe_to_company(company, recipe)
    record_event(
        db, company.id,
        EventType.METHOD_DISCOVERED if verified else EventType.RESEARCH_FAILED,
        evidence={"domain": recipe.domain, "confidence": recipe.confidence, "source_url": recipe.source_url}
        if verified else {"domain": recipe.domain, "research_attempts": recipe.research_attempts},
        recipe_id=recipe.id, recipe_version=recipe.recipe_version,
    )
    db.commit()
    return True


def process_pending(db: Session, provider: DeletionResearchProvider, limit: int | None = None) -> int:
    """Batch path for the background queue worker (app/deletion_queue.py).
    Finds recipes needing (re)research, respecting the retry cooldown, and
    syncs results to every Company row on that domain. Returns how many
    recipes were actually processed (attempted, not necessarily verified)."""
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

    processed = 0
    for recipe in candidates:
        if processed >= limit:
            break
        if recipe.status == RecipeStatus.NEEDS_RESEARCH and not _retry_allowed(recipe):
            continue

        companies = db.query(Company).filter(Company.domain == recipe.domain).all()
        company_name = companies[0].name if companies else recipe.domain

        verified = _research_and_update_recipe(recipe, company_name, provider)
        for company in companies:
            apply_recipe_to_company(company, recipe)
            record_event(
                db, company.id,
                EventType.METHOD_DISCOVERED if verified else EventType.RESEARCH_FAILED,
                evidence={"domain": recipe.domain, "confidence": recipe.confidence}
                if verified else {"domain": recipe.domain, "research_attempts": recipe.research_attempts},
                recipe_id=recipe.id, recipe_version=recipe.recipe_version,
            )
        db.commit()
        processed += 1

    return processed
