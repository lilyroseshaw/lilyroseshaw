"""Tests for Brave Search as Tier B: triggering only after a real Tier A
failure (blocked or exhausted), strict verification unchanged for
search-discovered candidates, the SOURCE_BLOCKED / unverified-portal
manual-review-lead evidence, and the daily query budget (enforcement, and
that exhausting it never counts as a failed research attempt).
"""
import datetime

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config
from app.db import Base
from app.deletion_constants import DeletionStatus, EventType, RecipeStatus, ResearchFailureReason
from app.deletion_research import (
    SourceBlockedDiscovery,
    UnverifiedPortalDiscovery,
    WebResearchProvider,
)
from app.deletion_resolver import (
    enqueue_pending,
    get_or_create_recipe_stub,
    process_pending,
    resolve_deletion_method,
)
from app.models import Company, DeletionEvent, DeletionRecipe
from app.research_crawl import SameDomainCrawler
from app.research_extract import RecipeExtractor
from app.research_fetch import PageFetcher
from app.research_search import BraveBudgetExhausted, BraveSearchBackend, DailyQueryBudget, SearchHit


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _company(db, name="Widget Co", domain="widgetco.com", **overrides) -> Company:
    defaults = dict(
        name=name, domain=domain, relationship_type="transactional", status="confirmed",
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        deletion_status=DeletionStatus.NOT_STARTED,
    )
    defaults.update(overrides)
    company = Company(**defaults)
    db.add(company)
    db.commit()
    return company


class _FakeBrave:
    """A SearchBackend stand-in with a real DailyQueryBudget, so budget
    enforcement is exercised exactly as production code would - only the
    actual HTTP call to Brave is faked."""

    def __init__(self, hits_by_query=None, daily_budget=None):
        self.hits_by_query = hits_by_query or {}
        self.budget = DailyQueryBudget(daily_budget if daily_budget is not None else config.BRAVE_SEARCH_DAILY_QUERY_BUDGET)
        self.queries_made: list[str] = []

    def search(self, query: str) -> list[SearchHit]:
        self.queries_made.append(query)
        return self.hits_by_query.get(query, [])


def _all_403_transport():
    def handler(request):
        return httpx.Response(403)
    return httpx.MockTransport(handler)


def _all_404_transport():
    def handler(request):
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def _provider_with_brave(domain: str, brave: _FakeBrave, page_handler) -> WebResearchProvider:
    client = httpx.Client(transport=httpx.MockTransport(page_handler), base_url=f"https://{domain}")
    fetcher = PageFetcher(client=client)
    return WebResearchProvider(
        fetcher=fetcher, crawler=SameDomainCrawler(), search_backend=brave, extractor=RecipeExtractor(),
    )


# --- Tier B triggers only after a genuine Tier A failure ---

def test_tier_b_triggers_after_403():
    """Eaze-shaped case: Tier A can't reach the site at all - homepage AND
    every one of its own guessed common paths return 403. The only real
    content lives at a URL Tier A would never guess, discoverable only
    via Brave."""
    domain = "eaze.com"
    hit = SearchHit(url=f"https://{domain}/legal/data-deletion-form", title="Data Deletion", snippet="")
    brave = _FakeBrave()

    def handler(request):
        if request.url.path == "/legal/data-deletion-form":
            return httpx.Response(
                200, headers={"content-type": "text/html"},
                text='<html><body><p>Right to deletion. Email <a href="mailto:privacy@eaze.com">privacy@eaze.com</a></p></body></html>',
            )
        return httpx.Response(403)  # homepage AND every Tier A guess: blocked

    provider = _provider_with_brave(domain, brave, handler)

    from app.research_search import brave_query_patterns
    for q in brave_query_patterns("Eaze", domain):
        brave.hits_by_query[q] = [hit]

    result = provider.research("Eaze", domain)
    assert result is not None
    assert result.verified is True
    assert brave.queries_made  # Tier B genuinely ran


def test_tier_b_triggers_after_candidate_exhaustion():
    """Rebag-shaped case: homepage reachable, every candidate 404s."""
    domain = "rebag.com"
    hit = SearchHit(url=f"https://{domain}/legal/data-rights", title="Data Rights", snippet="")
    brave = _FakeBrave()

    def handler(request):
        if request.url.path == "/":
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html><body>no matching links</body></html>")
        if request.url.path == "/legal/data-rights":
            return httpx.Response(
                200, headers={"content-type": "text/html"},
                text='<html><body><p>California residents have the right to deletion. Email <a href="mailto:privacy@rebag.com">privacy@rebag.com</a></p></body></html>',
            )
        return httpx.Response(404)  # every guessed common path

    provider = _provider_with_brave(domain, brave, handler)
    from app.research_search import brave_query_patterns
    for q in brave_query_patterns("Rebag", domain):
        brave.hits_by_query[q] = [hit]

    result = provider.research("Rebag", domain)
    assert result is not None
    assert result.verified is True
    assert result.source_url == f"https://{domain}/legal/data-rights"
    assert brave.queries_made


def test_no_brave_call_when_tier_a_succeeds():
    domain = "shopexample.com"
    brave = _FakeBrave()

    def handler(request):
        if request.url.path == "/":
            return httpx.Response(200, headers={"content-type": "text/html"}, text='<html><body><a href="/privacy">Privacy &amp; CCPA</a></body></html>')
        if request.url.path == "/privacy":
            return httpx.Response(
                200, headers={"content-type": "text/html"},
                text='<html><body><p>Right to deletion.</p><a href="mailto:privacy@shopexample.com">privacy@shopexample.com</a></body></html>',
            )
        return httpx.Response(404)

    provider = _provider_with_brave(domain, brave, handler)
    result = provider.research("Shop Example", domain)

    assert result is not None
    assert result.verified is True
    assert brave.queries_made == []  # Tier B never invoked


# --- successful official-domain Brave discovery ---

def test_successful_official_domain_brave_discovery():
    domain = "hardsite.com"
    brave = _FakeBrave()
    from app.research_search import brave_query_patterns
    for q in brave_query_patterns("Hard Site", domain):
        brave.hits_by_query[q] = [SearchHit(url=f"https://{domain}/ccpa-requests", title="CCPA", snippet="")]

    def handler(request):
        if request.url.path == "/ccpa-requests":
            return httpx.Response(
                200, headers={"content-type": "text/html"},
                text='<html><body><p>Submit a data rights request to delete your personal information.</p></body></html>',
            )
        return httpx.Response(404)

    provider = _provider_with_brave(domain, brave, handler)
    result = provider.research("Hard Site", domain)

    assert result is not None
    assert result.verified is True
    assert result.source_url == f"https://{domain}/ccpa-requests"


# --- rejection of unverified external portals ---

def test_unverified_external_portal_is_rejected_not_verified():
    """An external portal Brave finds, with no on-domain page linking to
    it, must never be accepted - the strict association rule is
    unchanged. It's raised as UnverifiedPortalDiscovery (a manual-review
    lead), not silently treated the same as finding nothing."""
    domain = "portalco.com"
    brave = _FakeBrave()
    from app.research_search import brave_query_patterns
    for q in brave_query_patterns("Portal Co", domain):
        brave.hits_by_query[q] = [SearchHit(url="https://privacyportal.onetrust.com/webform/portalco", title="Portal", snippet="")]

    def handler(request):
        return httpx.Response(404)  # Tier A: nothing

    def portal_handler(request):
        if "onetrust.com" in str(request.url):
            return httpx.Response(
                200, headers={"content-type": "text/html"},
                text='<html><body><p>You have the right to deletion of your personal information. Submit your request below.</p></body></html>',
            )
        return httpx.Response(404)

    # Two different hosts involved (portalco.com Tier A, onetrust.com Tier B
    # candidate) - route by host.
    def router(request):
        if request.url.host == domain:
            return handler(request)
        return portal_handler(request)

    client = httpx.Client(transport=httpx.MockTransport(router), base_url=f"https://{domain}")
    fetcher = PageFetcher(client=client)
    provider = WebResearchProvider(fetcher=fetcher, crawler=SameDomainCrawler(), search_backend=brave, extractor=RecipeExtractor())

    with pytest.raises(UnverifiedPortalDiscovery) as exc_info:
        provider.research("Portal Co", domain)
    assert exc_info.value.url == "https://privacyportal.onetrust.com/webform/portalco"


# --- SOURCE_BLOCKED handling ---

def test_source_blocked_when_brave_finds_own_domain_url_that_403s():
    domain = "blockedco.com"
    brave = _FakeBrave()
    from app.research_search import brave_query_patterns
    for q in brave_query_patterns("Blocked Co", domain):
        brave.hits_by_query[q] = [SearchHit(url=f"https://{domain}/privacy-requests", title="Privacy", snippet="")]

    def handler(request):
        return httpx.Response(403)  # everything, including the Brave-found URL, is blocked

    provider = _provider_with_brave(domain, brave, handler)

    with pytest.raises(SourceBlockedDiscovery) as exc_info:
        provider.research("Blocked Co", domain)
    assert exc_info.value.url == f"https://{domain}/privacy-requests"


def test_source_blocked_is_recorded_distinctly_end_to_end(db, monkeypatch):
    """Full pipeline: SOURCE_BLOCKED must never verify the recipe, never
    mark anything submitted, and must be recorded with the blocked URL as
    evidence for manual review."""
    domain = "blockedco.com"
    company = _company(db, domain=domain)
    brave = _FakeBrave()
    from app.research_search import brave_query_patterns
    for q in brave_query_patterns(company.name, domain):
        brave.hits_by_query[q] = [SearchHit(url=f"https://{domain}/privacy-requests", title="Privacy", snippet="")]

    def handler(request):
        return httpx.Response(403)

    provider = _provider_with_brave(domain, brave, handler)
    resolve_deletion_method(db, company, provider, force=True)

    db.refresh(company)
    assert company.deletion_status == DeletionStatus.UNKNOWN  # never verified/READY
    assert company.deletion_verified is False

    event = (
        db.query(DeletionEvent)
        .filter(DeletionEvent.company_id == company.id, DeletionEvent.event_type == EventType.RESEARCH_FAILED)
        .one()
    )
    assert event.evidence["reason"] == ResearchFailureReason.SOURCE_BLOCKED
    assert event.evidence["blocked_url"] == f"https://{domain}/privacy-requests"


# --- daily budget enforcement ---

def test_daily_budget_enforced_across_multiple_attempts():
    budget = DailyQueryBudget(daily_limit=5)
    assert budget.try_consume(3) is True
    assert budget.used_today == 3
    assert budget.try_consume(3) is False  # would exceed 5
    assert budget.used_today == 3  # unchanged - all-or-nothing, no partial consumption
    assert budget.try_consume(2) is True
    assert budget.used_today == 5
    assert budget.try_consume(1) is False


def test_daily_budget_resets_on_new_day():
    budget = DailyQueryBudget(daily_limit=2)
    assert budget.try_consume(2) is True
    assert budget.try_consume(1) is False
    # Simulate a day rollover.
    budget._date = datetime.date.today() - datetime.timedelta(days=1)
    assert budget.try_consume(2) is True
    assert budget.used_today == 2


def test_worker_stops_making_brave_requests_once_budget_exhausted():
    domain = "eaze.com"
    brave = _FakeBrave(daily_budget=1)
    brave.budget.try_consume(1)  # already exhausted before this attempt

    def handler(request):
        return httpx.Response(403)

    provider = _provider_with_brave(domain, brave, handler)
    with pytest.raises(BraveBudgetExhausted):
        provider.research("Eaze", domain)
    assert brave.queries_made == []  # no Brave request was made at all


# --- budget exhaustion never counts as a failed attempt ---

def test_budget_exhaustion_does_not_increment_attempts_single_company(db):
    domain = "eaze.com"
    company = _company(db, domain=domain, deletion_status=DeletionStatus.UNKNOWN)
    recipe = get_or_create_recipe_stub(db, domain)
    recipe.status = RecipeStatus.NEEDS_RESEARCH
    recipe.research_attempts = 1
    db.commit()

    brave = _FakeBrave(daily_budget=1)
    brave.budget.try_consume(1)  # pre-exhausted

    def handler(request):
        return httpx.Response(403)

    provider = _provider_with_brave(domain, brave, handler)
    resolve_deletion_method(db, company, provider, force=True)

    db.refresh(recipe)
    db.refresh(company)
    assert recipe.research_attempts == 1  # untouched - not incremented
    assert company.deletion_status == DeletionStatus.UNKNOWN  # restored to exactly what it was

    event = (
        db.query(DeletionEvent)
        .filter(DeletionEvent.company_id == company.id, DeletionEvent.event_type == EventType.RESEARCH_DEFERRED)
        .one()
    )
    assert event.evidence["reason"] == ResearchFailureReason.BUDGET_EXHAUSTED
    # No RESEARCH_FAILED event at all for this attempt.
    assert db.query(DeletionEvent).filter(
        DeletionEvent.company_id == company.id, DeletionEvent.event_type == EventType.RESEARCH_FAILED
    ).count() == 0


def test_budget_exhaustion_does_not_increment_attempts_in_process_pending(db):
    domain = "eaze.com"
    company = _company(db, domain=domain)
    enqueue_pending(db, [company])
    db.commit()

    brave = _FakeBrave(daily_budget=1)
    brave.budget.try_consume(1)  # pre-exhausted

    def handler(request):
        return httpx.Response(403)

    provider = _provider_with_brave(domain, brave, handler)
    processed = process_pending(db, provider, limit=10)

    assert processed == 0  # a pure deferral is never counted as "processed"
    recipe = db.query(DeletionRecipe).filter(DeletionRecipe.domain == domain).one()
    assert recipe.research_attempts == 0
    assert recipe.last_attempted_at is None
    db.refresh(company)
    assert company.deletion_status == DeletionStatus.UNKNOWN  # original enqueue_pending state, restored

    event = (
        db.query(DeletionEvent)
        .filter(DeletionEvent.company_id == company.id, DeletionEvent.event_type == EventType.RESEARCH_DEFERRED)
        .one()
    )
    assert event.evidence["reason"] == ResearchFailureReason.BUDGET_EXHAUSTED


def test_budget_exhaustion_never_sets_method_lookup_permanently(db):
    """Sanity check on top of the reliability fix's existing METHOD_LOOKUP
    guarantees: a budget-deferred attempt must not leave the company
    showing "Researching..." either."""
    domain = "eaze.com"
    company = _company(db, domain=domain, deletion_status=DeletionStatus.NOT_STARTED)
    brave = _FakeBrave(daily_budget=1)
    brave.budget.try_consume(1)

    def handler(request):
        return httpx.Response(403)

    provider = _provider_with_brave(domain, brave, handler)
    resolve_deletion_method(db, company, provider, force=True)

    db.refresh(company)
    assert company.deletion_status != DeletionStatus.METHOD_LOOKUP
    assert company.deletion_status == DeletionStatus.NOT_STARTED  # exactly restored


# --- query pattern count respects config ---

def test_brave_query_patterns_respects_configured_count(monkeypatch):
    from app.research_search import brave_query_patterns

    monkeypatch.setattr(config, "BRAVE_SEARCH_QUERIES_PER_ATTEMPT", 2)
    patterns = brave_query_patterns("Acme", "acme.com")
    assert len(patterns) == 2
    assert all("site:acme.com" in p for p in patterns)


# --- real BraveSearchBackend wires a budget from config automatically ---

def test_brave_search_backend_defaults_budget_from_config(monkeypatch):
    monkeypatch.setattr(config, "BRAVE_SEARCH_DAILY_QUERY_BUDGET", 42)
    backend = BraveSearchBackend("key", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    assert backend.budget.daily_limit == 42
