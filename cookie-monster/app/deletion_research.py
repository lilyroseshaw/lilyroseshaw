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
from app.research_search import BraveSearchBackend, SearchBackend, search_hits_to_candidates, site_scoped_query
from app.research_types import CandidateSource, ResearchResult


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
        candidates = self._crawler.discover(domain, self._fetcher)
        if not candidates and self._search_backend is not None:
            hits = self._search_backend.search(site_scoped_query(company_name, domain))
            candidates = search_hits_to_candidates(hits)
        return candidates

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
