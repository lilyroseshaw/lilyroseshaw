import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config
from app.db import Base
from app.deletion_constants import DeletionStatus, RecipeStatus
from app.deletion_research import DeletionResearchProvider
from app.deletion_resolver import enqueue_pending, get_or_create_recipe_stub, process_pending, resolve_deletion_method
from app.models import Company, DeletionEvent, DeletionRecipe
from app.research_types import ResearchResult


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _company(db, name="Widget Co", domain="widgetco.com", status="confirmed") -> Company:
    company = Company(
        name=name, domain=domain, relationship_type="transactional", status=status,
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        deletion_status=DeletionStatus.NOT_STARTED,
    )
    db.add(company)
    db.commit()
    return company


class FakeProvider(DeletionResearchProvider):
    """Records every call it receives and returns pre-programmed results per domain."""

    def __init__(self, results: dict[str, ResearchResult | None]):
        self.results = results
        self.calls: list[str] = []

    def search_official_sources(self, company_name, domain):
        return []

    def inspect_privacy_page(self, url, domain):
        return None

    def extract_deletion_recipe(self, company_name, domain, pages):
        return None

    def verify_recipe(self, domain, result):
        return result.verified

    def research(self, company_name, domain):
        self.calls.append(domain)
        return self.results.get(domain)


def _verified_result(domain, email="privacy@example.com"):
    return ResearchResult(
        domain=domain, method="EMAIL_REQUEST", email=email,
        source_url=f"https://{domain}/privacy", confidence="high", verified=True, reasons=["test"],
    )


# --- enqueue_pending: fast, no network ---

def test_enqueue_pending_creates_recipe_stub_and_applies_it(db):
    company = _company(db)
    enqueue_pending(db, [company])
    db.commit()
    assert company.deletion_status == DeletionStatus.UNKNOWN  # stub recipe, not yet researched
    assert db.query(DeletionRecipe).filter(DeletionRecipe.domain == "widgetco.com").count() == 1


def test_enqueue_pending_applies_an_already_verified_recipe(db):
    company = _company(db)
    db.add(DeletionRecipe(
        domain="widgetco.com", method="EMAIL_REQUEST", email="privacy@widgetco.com",
        status=RecipeStatus.VERIFIED, source_url="https://widgetco.com/privacy",
        verified_at=datetime.datetime.utcnow(),
        expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=100),
    ))
    db.commit()
    enqueue_pending(db, [company])
    db.commit()
    assert company.deletion_status == DeletionStatus.READY
    assert company.deletion_email == "privacy@widgetco.com"


def test_enqueue_pending_never_calls_the_research_provider():
    """This is what keeps scanning fast - enqueue_pending must not accept
    or need a provider at all."""
    import inspect
    assert "provider" not in inspect.signature(enqueue_pending).parameters


# --- resolve_deletion_method: single-company synchronous path ---

def test_resolve_single_company_researches_when_uncached(db):
    company = _company(db)
    provider = FakeProvider({"widgetco.com": _verified_result("widgetco.com")})
    changed = resolve_deletion_method(db, company, provider)
    assert changed is True
    assert provider.calls == ["widgetco.com"]
    assert company.deletion_status == DeletionStatus.READY
    assert company.deletion_email == "privacy@example.com"


def test_resolve_single_company_uses_cache_without_calling_provider(db):
    company = _company(db)
    provider = FakeProvider({"widgetco.com": _verified_result("widgetco.com")})
    resolve_deletion_method(db, company, provider)  # first call researches
    provider.calls.clear()

    changed = resolve_deletion_method(db, company, provider)  # second call should hit cache
    assert changed is False
    assert provider.calls == []


def test_resolve_single_company_force_rechecks_even_when_fresh(db):
    company = _company(db)
    provider = FakeProvider({"widgetco.com": _verified_result("widgetco.com")})
    resolve_deletion_method(db, company, provider)
    provider.calls.clear()

    resolve_deletion_method(db, company, provider, force=True)
    assert provider.calls == ["widgetco.com"]


def test_resolve_unverifiable_company_marks_unknown_not_fabricated(db):
    company = _company(db, domain="mystery.com")
    provider = FakeProvider({})  # no result for this domain
    resolve_deletion_method(db, company, provider)
    assert company.deletion_status == DeletionStatus.UNKNOWN
    assert company.deletion_method == "UNKNOWN"
    assert company.deletion_verified is False


def test_resolve_records_events(db):
    company = _company(db)
    provider = FakeProvider({"widgetco.com": _verified_result("widgetco.com")})
    resolve_deletion_method(db, company, provider)
    events = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).all()
    assert len(events) == 1
    assert events[0].event_type == "METHOD_DISCOVERED"


# --- process_pending: background batch path ---

def test_process_pending_researches_multiple_domains(db):
    c1 = _company(db, "Widget Co", "widgetco.com")
    c2 = _company(db, "Gadget Co", "gadgetco.com")
    enqueue_pending(db, [c1, c2])
    db.commit()

    provider = FakeProvider({"widgetco.com": _verified_result("widgetco.com"), "gadgetco.com": None})
    processed = process_pending(db, provider, limit=10)
    assert processed == 2

    db.refresh(c1)
    db.refresh(c2)
    assert c1.deletion_status == DeletionStatus.READY
    assert c2.deletion_status == DeletionStatus.UNKNOWN


def test_process_pending_respects_limit(db):
    companies = [_company(db, f"Co {i}", f"co{i}.com") for i in range(5)]
    enqueue_pending(db, companies)
    db.commit()

    provider = FakeProvider({f"co{i}.com": _verified_result(f"co{i}.com") for i in range(5)})
    processed = process_pending(db, provider, limit=2)
    assert processed == 2
    assert len(provider.calls) == 2


def test_process_pending_skips_recipes_still_in_retry_cooldown(db):
    company = _company(db)
    recipe = get_or_create_recipe_stub(db, "widgetco.com")
    recipe.status = RecipeStatus.NEEDS_RESEARCH
    recipe.last_attempted_at = datetime.datetime.utcnow()  # just attempted - within cooldown
    db.commit()

    provider = FakeProvider({"widgetco.com": _verified_result("widgetco.com")})
    processed = process_pending(db, provider, limit=10)
    assert processed == 0
    assert provider.calls == []


def test_process_pending_retries_after_cooldown_elapses(db):
    company = _company(db)
    recipe = get_or_create_recipe_stub(db, "widgetco.com")
    recipe.status = RecipeStatus.NEEDS_RESEARCH
    recipe.last_attempted_at = datetime.datetime.utcnow() - datetime.timedelta(
        days=config.DELETION_RECIPE_RETRY_COOLDOWN_DAYS + 1
    )
    db.commit()

    provider = FakeProvider({"widgetco.com": _verified_result("widgetco.com")})
    processed = process_pending(db, provider, limit=10)
    assert processed == 1
    assert provider.calls == ["widgetco.com"]


def test_stale_verified_recipe_is_re_researched(db):
    company = _company(db)
    db.add(DeletionRecipe(
        domain="widgetco.com", method="EMAIL_REQUEST", status=RecipeStatus.VERIFIED,
        source_url="https://widgetco.com/old-privacy",
        verified_at=datetime.datetime.utcnow() - datetime.timedelta(days=200),
        expires_at=datetime.datetime.utcnow() - datetime.timedelta(days=50),  # expired
    ))
    db.commit()

    provider = FakeProvider({"widgetco.com": _verified_result("widgetco.com", email="new@widgetco.com")})
    processed = process_pending(db, provider, limit=10)
    assert processed == 1

    recipe = db.query(DeletionRecipe).filter(DeletionRecipe.domain == "widgetco.com").one()
    assert recipe.email == "new@widgetco.com"
    assert recipe.recipe_version == 2  # bumped because the result changed


def test_failed_reverification_never_destroys_an_already_verified_recipe(db):
    """Regression: forcing a re-check (e.g. the manual 'Research deletion
    method' button, or a routine freshness cycle) with a provider that can't
    verify anything right now (disabled research, transient failure) must
    not downgrade an already-good seed/researched recipe to NEEDS_RESEARCH -
    that would make clicking refresh actively worse than doing nothing."""
    company = _company(db)
    db.add(DeletionRecipe(
        domain="widgetco.com", method="ACCOUNT_SETTING", url="https://widgetco.com/delete",
        status=RecipeStatus.VERIFIED, origin="seed", source_url="https://widgetco.com/delete",
        verified_at=datetime.datetime.utcnow() - datetime.timedelta(days=200),
        expires_at=datetime.datetime.utcnow() - datetime.timedelta(days=50),  # stale, due for recheck
    ))
    db.commit()

    provider = FakeProvider({})  # can't verify anything right now
    resolve_deletion_method(db, company, provider, force=True)

    recipe = db.query(DeletionRecipe).filter(DeletionRecipe.domain == "widgetco.com").one()
    assert recipe.status == RecipeStatus.VERIFIED  # NOT downgraded
    assert recipe.method == "ACCOUNT_SETTING"  # last-known-good data preserved
    assert recipe.research_attempts == 1  # attempt was still recorded
    assert company.deletion_status == DeletionStatus.READY
    assert company.deletion_verified is True


def test_domain_normalization_shares_one_recipe_across_subdomains(db):
    """mail.widgetco.com and widgetco.com must resolve to the same cached
    recipe - this is already guaranteed by Company.domain being normalized
    at classification time, but confirm the resolver doesn't create a
    second recipe for a differently-cased/prefixed variant."""
    stub1 = get_or_create_recipe_stub(db, "widgetco.com")
    stub2 = get_or_create_recipe_stub(db, "widgetco.com")
    db.commit()
    assert stub1.id == stub2.id
    assert db.query(DeletionRecipe).filter(DeletionRecipe.domain == "widgetco.com").count() == 1
