import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.deletion_constants import RecipeOrigin, RecipeStatus
from app.deletion_seeds import SEED_RECIPES, seed_known_recipes
from app.models import DeletionRecipe


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_seeds_are_inserted_as_verified(db):
    inserted = seed_known_recipes(db)
    assert inserted == len(SEED_RECIPES)
    recipes = db.query(DeletionRecipe).all()
    assert len(recipes) == len(SEED_RECIPES)
    for recipe in recipes:
        assert recipe.status == RecipeStatus.VERIFIED
        assert recipe.origin == RecipeOrigin.SEED
        assert recipe.source_url  # every seed must carry a real source
        assert recipe.expires_at is not None


def test_seeding_is_idempotent(db):
    seed_known_recipes(db)
    second_pass = seed_known_recipes(db)
    assert second_pass == 0
    assert db.query(DeletionRecipe).count() == len(SEED_RECIPES)


def test_seeding_never_overwrites_a_researched_recipe(db):
    """If a domain has already been independently researched (or manually
    corrected) before the seed loader runs, the seed must not clobber it."""
    lyft_domain = next(s.domain for s in SEED_RECIPES if s.domain == "lyft.com")
    db.add(DeletionRecipe(domain=lyft_domain, method="EMAIL_REQUEST", status=RecipeStatus.VERIFIED,
                           origin=RecipeOrigin.RESEARCHED, source_url="https://lyft.com/some-other-page"))
    db.commit()

    seed_known_recipes(db)

    recipe = db.query(DeletionRecipe).filter(DeletionRecipe.domain == lyft_domain).one()
    assert recipe.origin == RecipeOrigin.RESEARCHED
    assert recipe.source_url == "https://lyft.com/some-other-page"
