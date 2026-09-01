"""Bootstrap data only - NOT the primary source of truth for deletion
recipes. This is the two entries this project already had (Lyft, Edikted),
carried over from the previous hardcoded-in-the-template version. They're
loaded into the DeletionRecipe table once at startup (idempotent - never
overwrites a recipe that's already been researched/verified for that domain)
so they go through the exact same cache/resolver/engine path as every other
company Cookie Monster researches on its own.

Everything past these two entries is expected to come from
DeletionResearchProvider (app/deletion_research.py), not from editing this
file. If you want to add a manually-verified entry (e.g. you personally
confirmed a company's process), that's still legitimate - just make sure
`source_url` points at the exact official page you read, same as these two.
"""
import datetime
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app import config
from app.classifier import normalize_domain
from app.deletion_constants import ActionCapability, DeletionMethod, RecipeOrigin, RecipeStatus, SourceType
from app.models import DeletionRecipe


@dataclass(frozen=True)
class SeedRecipe:
    domain: str
    method: str
    automation: str
    source_url: str
    url: str | None = None
    email: str | None = None
    instructions: str | None = None
    consequences: str | None = None


SEED_RECIPES: list[SeedRecipe] = [
    SeedRecipe(
        domain="lyft.com",
        method=DeletionMethod.ACCOUNT_SETTING,
        automation=ActionCapability.USER_ACTION_REQUIRED,
        source_url="https://account.lyft.com/privacy/data/delete",
        url="https://account.lyft.com/privacy/data/delete",
        instructions="Sign in to your Lyft account and submit the deletion request from Lyft's own data-deletion page.",
        consequences="Lyft states this process deletes your Lyft account. Once completed, it cannot be undone.",
    ),
    SeedRecipe(
        domain="edikted.com",
        method=DeletionMethod.PRIVACY_PORTAL,
        automation=ActionCapability.USER_ACTION_REQUIRED,
        source_url="https://edikted.com/pages/ccpa-compliance",
        url="https://edikted.com/pages/ccpa-compliance",
        instructions="Submit your deletion request through Edikted's CCPA compliance page.",
        consequences="Edikted states that requesting deletion will also delete your Edikted account.",
    ),
]


def seed_known_recipes(db: Session) -> int:
    """Idempotent: only inserts a seed if that domain has no recipe yet.
    Never overwrites a recipe that's since been researched or manually
    corrected. Returns how many were newly inserted."""
    inserted = 0
    for seed in SEED_RECIPES:
        domain = normalize_domain(seed.domain) or seed.domain
        existing = db.query(DeletionRecipe).filter(DeletionRecipe.domain == domain).one_or_none()
        if existing is not None:
            continue
        now = datetime.datetime.utcnow()
        db.add(
            DeletionRecipe(
                domain=domain,
                method=seed.method,
                action_capability=seed.automation,
                url=seed.url,
                email=seed.email,
                instructions=" ".join(part for part in (seed.instructions, seed.consequences) if part) or None,
                known_consequences=seed.consequences,
                source_url=seed.source_url,
                source_type=SourceType.SEED,
                confidence="high",
                status=RecipeStatus.VERIFIED,
                origin=RecipeOrigin.SEED,
                verified_at=now,
                expires_at=now + datetime.timedelta(days=config.DELETION_RECIPE_FRESHNESS_DAYS),
            )
        )
        inserted += 1
    if inserted:
        db.commit()
    return inserted
