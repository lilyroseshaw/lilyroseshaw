"""The research interface deletion_resolver.py calls when a domain has no
cached recipe (or a stale one). Two implementations ship here:

- NullResearchProvider: makes zero outbound requests, always reports
  "nothing found". Used when DELETION_RESEARCH_ENABLED=false.
- WebResearchProvider: the real, production-capable implementation -
  Tier A (same-domain crawl, always on, no key needed) + optional Tier B
  (Brave Search, needs BRAVE_SEARCH_API_KEY) for source discovery, and
  Pass 1 (regex) + optional Pass 2 (Claude, needs ANTHROPIC_API_KEY) for
  extraction. See research_crawl.py / research_search.py / research_extract.py
  for each piece, and research_fetch.py for the HTTP layer.

verify_recipe() is the anti-hallucination gate: a result is only accepted if
its source is the company's own domain, or a third-party portal that a
domain-verified official page explicitly linked to (with that referring page
recorded as evidence). Nothing here ever invents a URL or email - if
verify_recipe rejects a result, research() returns None and the caller marks
the recipe NEEDS_RESEARCH.
"""
from abc import ABC, abstractmethod
from urllib.parse import urlparse

from app import config
from app.classifier import normalize_domain
from app.research_crawl import SameDomainCrawler
from app.research_extract import RecipeExtractor
from app.research_fetch import PageContent, PageFetcher
from app.research_search import (
    BraveBudgetExhausted,
    BraveSearchBackend,
    SearchBackend,
    brave_query_patterns,
    search_hits_to_candidates,
)
from app.research_types import CandidateSource, ResearchResult

# HTTP statuses treated as "the site is refusing automated access" - never
# bypassed, just classified distinctly from a genuine 404 "not found" so
# Tier B triggers appropriately and a same-domain block can be surfaced as
# SourceBlockedDiscovery rather than silently folded into "nothing found".
_BLOCKING_STATUSES = (401, 403, 429)


class SourceBlockedDiscovery(Exception):
    """A candidate (Tier A or Tier B) resolved to the company's OWN
    domain, but our fetcher couldn't reach it - a blocking response, never
    bypassed. The URL is still useful as a manual-review lead, so it's
    carried on this exception rather than silently discarded as "nothing
    found" - see deletion_resolver.py's _run_research_only, which turns
    this into ResearchFailureReason.SOURCE_BLOCKED evidence."""

    def __init__(self, url: str):
        self.url = url
        super().__init__(f"source blocked: {url}")


class UnverifiedPortalDiscovery(Exception):
    """A Tier B (or Tier A) candidate produced a result, but verify_recipe
    rejected it - most often an external portal with no on-domain page
    linking to it, so its "legitimate association" with the company
    couldn't be established. Never accepted as verified (the strict
    same-domain/linked-portal rule is unchanged), but the URL is kept as a
    manual-review lead rather than silently dropped - see product
    decision: "retain it only as a manual-review lead"."""

    def __init__(self, url: str | None):
        self.url = url
        super().__init__(f"unverified candidate: {url}")


class DeletionResearchProvider(ABC):
    @abstractmethod
    def search_official_sources(self, company_name: str, domain: str) -> list[CandidateSource]:
        ...

    @abstractmethod
    def inspect_privacy_page(self, url: str, domain: str) -> PageContent | None:
        ...

    @abstractmethod
    def extract_deletion_recipe(
        self, company_name: str, domain: str, pages: list[PageContent]
    ) -> ResearchResult | None:
        ...

    @abstractmethod
    def verify_recipe(self, domain: str, result: ResearchResult) -> bool:
        ...

    def refresh_recipe(self, company_name: str, domain: str, existing_source_url: str | None = None):
        """Re-verifies/re-researches a domain that already has a recipe (stale,
        or execution failed against it). Default implementation just re-runs
        full research; a provider could override to check existing_source_url
        first before falling back to a full search."""
        return self.research(company_name, domain)

    def research(self, company_name: str, domain: str) -> ResearchResult | None:
        """Orchestrates the four steps above. Never called with an already-fresh
        cached recipe - see deletion_resolver.py."""
        candidates = self.search_official_sources(company_name, domain)
        pages: list[PageContent] = []
        for candidate in candidates[: config.RESEARCH_MAX_PAGES_PER_COMPANY]:
            page = self.inspect_privacy_page(candidate.url, domain)
            if page is not None:
                pages.append(page)
        if not pages:
            return None

        result = self.extract_deletion_recipe(company_name, domain, pages)
        if result is None:
            return None
        if not self.verify_recipe(domain, result):
            return None
        result.verified = True
        return result


class NullResearchProvider(DeletionResearchProvider):
    """Makes no outbound requests at all. Every step reports 'nothing found',
    so research() always returns None and nothing is ever fabricated."""

    def search_official_sources(self, company_name: str, domain: str) -> list[CandidateSource]:
        return []

    def inspect_privacy_page(self, url: str, domain: str) -> PageContent | None:
        return None

    def extract_deletion_recipe(self, company_name, domain, pages) -> ResearchResult | None:
        return None

    def verify_recipe(self, domain: str, result: ResearchResult) -> bool:
        return False


class WebResearchProvider(DeletionResearchProvider):
    """The real implementation. Tier A (crawl) always runs; Tier B (search)
    and Pass 2 (LLM extraction) are each optional based on what's configured
    on the injected extractor/search_backend - see build_default_provider()."""

    def __init__(
        self,
        fetcher: PageFetcher | None = None,
        crawler: SameDomainCrawler | None = None,
        search_backend: SearchBackend | None = None,
        extractor: RecipeExtractor | None = None,
    ):
        self._fetcher = fetcher or PageFetcher()
        self._crawler = crawler or SameDomainCrawler()
        self._search_backend = search_backend
        self._extractor = extractor or RecipeExtractor()

    def search_official_sources(self, company_name: str, domain: str) -> list[CandidateSource]:
        """Tier A discovery only - homepage links + guessed common paths.
        Tier B (search) is a SEPARATE, later step orchestrated by
        research() below, triggered only after Tier A has demonstrably
        failed to produce anything USABLE (not merely "this list happens
        to be non-empty" - SameDomainCrawler.discover() always returns at
        least its guessed paths, so that check was never actually
        meaningful)."""
        return self._crawler.discover(domain, self._fetcher)

    def inspect_privacy_page(self, url: str, domain: str) -> PageContent | None:
        return self._fetcher.fetch(url, domain)

    def extract_deletion_recipe(self, company_name, domain, pages) -> ResearchResult | None:
        return self._extractor.extract(company_name, domain, pages)

    def verify_recipe(self, domain: str, result: ResearchResult) -> bool:
        if not result.source_url or not result.source_url.startswith("https://"):
            return False

        source_domain = normalize_domain(urlparse(result.source_url).netloc)
        if source_domain == domain:
            return True

        # Third-party portal: only acceptable if we reached it via a page on
        # the company's own verified domain - and we keep that page as evidence.
        if result.referring_official_url:
            referring_domain = normalize_domain(urlparse(result.referring_official_url).netloc)
            if referring_domain == domain:
                return True

        return False

    def research(self, company_name: str, domain: str) -> ResearchResult | None:
        """Tier A first, always - Tier B (Brave, if configured) triggers
        ONLY after a real Tier A failure: blocked (401/403/429 on the
        homepage or every candidate), candidate exhaustion (nothing
        fetchable at all), or pages were reachable but nothing verifiable
        could be extracted from them. Never triggers once Tier A has
        already produced a verified result.

        Overrides the base class's generic four-step orchestration (which
        NullResearchProvider still uses unchanged) because the two-tier,
        budget-aware, blocked-vs-not-found-aware logic here needs to be a
        single connected decision, not four independently-abstracted
        steps."""
        candidates = self.search_official_sources(company_name, domain)
        pages: list[PageContent] = []
        for candidate in candidates[: config.RESEARCH_MAX_PAGES_PER_COMPANY]:
            page, _status = self._fetcher.fetch_with_status(candidate.url, domain)
            if page is not None:
                pages.append(page)

        if pages:
            result = self.extract_deletion_recipe(company_name, domain, pages)
            if result is not None and self.verify_recipe(domain, result):
                result.verified = True
                return result

        # Tier A didn't produce a verified result (whether from blocking,
        # exhaustion, or an unverifiable extraction) - fall back to Tier B
        # only if it's configured at all.
        if self._search_backend is None:
            return None
        return self._research_via_search(company_name, domain)

    def _research_via_search(self, company_name: str, domain: str) -> ResearchResult | None:
        """Tier B: several targeted queries (brave_query_patterns), each
        result independently fetched and run through the SAME
        verify_recipe() rule as any Tier A candidate - a search hit is
        discovery only, never trusted content. Raises BraveBudgetExhausted
        if today's query budget has no room for a full attempt (never a
        partial one - all-or-nothing, so usage stays predictable), raises
        SourceBlockedDiscovery if an own-domain candidate was found but
        couldn't be fetched, and raises UnverifiedPortalDiscovery if a
        result was extracted but failed the official-source check - both
        exceptions carry the URL forward as a manual-review lead rather
        than silently discarding it."""
        if not self._search_backend.budget.try_consume(config.BRAVE_SEARCH_QUERIES_PER_ATTEMPT):
            raise BraveBudgetExhausted()

        hits = []
        for query in brave_query_patterns(company_name, domain):
            hits.extend(self._search_backend.search(query))
        candidates = search_hits_to_candidates(hits)

        pages: list[PageContent] = []
        blocked_official_url: str | None = None
        for candidate in candidates[: config.RESEARCH_MAX_PAGES_PER_COMPANY]:
            page, status = self._fetcher.fetch_with_status(candidate.url, domain)
            if page is not None:
                pages.append(page)
            elif status in _BLOCKING_STATUSES and blocked_official_url is None:
                candidate_domain = normalize_domain(urlparse(candidate.url).netloc)
                if candidate_domain == domain:
                    blocked_official_url = candidate.url

        if pages:
            result = self.extract_deletion_recipe(company_name, domain, pages)
            if result is not None:
                if self.verify_recipe(domain, result):
                    result.verified = True
                    return result
                raise UnverifiedPortalDiscovery(result.source_url)

        if blocked_official_url:
            raise SourceBlockedDiscovery(blocked_official_url)

        return None


def build_default_provider() -> DeletionResearchProvider:
    """Constructs the provider used app-wide, based on what's configured in
    .env. Zero keys set -> WebResearchProvider still works (Tier A + Pass 1
    only). DELETION_RESEARCH_ENABLED=false -> no outbound requests at all."""
    if not config.DELETION_RESEARCH_ENABLED:
        return NullResearchProvider()

    search_backend = BraveSearchBackend(config.BRAVE_SEARCH_API_KEY) if config.BRAVE_SEARCH_API_KEY else None

    llm_client = None
    if config.ANTHROPIC_API_KEY:
        import anthropic

        llm_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    extractor = RecipeExtractor(llm_client=llm_client, llm_model=config.DELETION_RESEARCH_LLM_MODEL)
    return WebResearchProvider(search_backend=search_backend, extractor=extractor)
