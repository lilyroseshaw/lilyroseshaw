"""Tier B source discovery: an optional search-engine fallback, used only
when the same-domain crawl (research_crawl.py) finds nothing. Behind a small
interface so a different backend can be swapped in without touching
deletion_research.py.

Search results are candidates, not trusted sources - every result still has
to be fetched and pass verify_recipe's official-domain check before it's
used for anything. A search result on someone else's domain (a blog, a
comparison site) is never accepted as a source of truth.
"""
import datetime
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from app import config
from app.deletion_constants import SourceType
from app.research_types import CandidateSource

BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class BraveBudgetExhausted(Exception):
    """Raised by deletion_research.WebResearchProvider when a Tier B
    attempt is needed but today's Brave query budget
    (config.BRAVE_SEARCH_DAILY_QUERY_BUDGET) has none left. Caught
    specifically in deletion_resolver.py's _run_research_only and turned
    into a deferral - never counted as a failed research attempt."""


class DailyQueryBudget:
    """Thread-safe daily query counter for a paid search API - process_pending()
    (app/deletion_resolver.py) may run several research attempts
    concurrently, all sharing one BraveSearchBackend instance, so the
    check-and-consume has to be atomic across threads. Resets automatically
    at the next UTC date change. No persistence: this is a single-process
    prototype, so a reset-on-restart budget is an accepted tradeoff - it
    only ever makes the cap MORE conservative (a restart never grants extra
    budget), never less safe."""

    def __init__(self, daily_limit: int):
        self.daily_limit = daily_limit
        self._lock = threading.Lock()
        self._date = datetime.date.today()
        self._used = 0

    def try_consume(self, n: int) -> bool:
        """Atomically checks whether n more queries fit in today's budget
        and, if so, reserves them. All-or-nothing: never partially
        consumes a batch."""
        with self._lock:
            self._roll_over_if_new_day()
            if self._used + n > self.daily_limit:
                return False
            self._used += n
            return True

    @property
    def used_today(self) -> int:
        with self._lock:
            self._roll_over_if_new_day()
            return self._used

    def _roll_over_if_new_day(self) -> None:
        today = datetime.date.today()
        if today != self._date:
            self._date = today
            self._used = 0


@dataclass
class SearchHit:
    url: str
    title: str
    snippet: str


class SearchBackend(ABC):
    @abstractmethod
    def search(self, query: str) -> list[SearchHit]:
        ...


def site_scoped_query(company_name: str, domain: str) -> str:
    return (
        f'site:{domain} privacy "delete my data" OR "delete account" OR '
        f'"right to deletion" OR "right to be forgotten" OR CCPA OR "data rights request"'
    )


def brave_query_patterns(company_name: str, domain: str) -> list[str]:
    """Several distinct, narrowly-scoped angles on the same question ("how
    does this company handle a deletion request"), not one generic query -
    real companies phrase and organize this very differently, so covering
    the deletion-page, general data-rights-page, and privacy-contact-email
    angles separately meaningfully improves discovery over a single query.
    Always site:-scoped to the company's OWN domain - this is discovery of
    an official source, never a general open web search. Truncated to
    config.BRAVE_SEARCH_QUERIES_PER_ATTEMPT so the per-attempt query count
    stays a single, named, configurable value (see deletion_research.py)."""
    patterns = [
        f'site:{domain} ("delete my data" OR "delete my account" OR "right to deletion" OR '
        f'"right to be forgotten" OR "ccpa deletion request")',
        f'site:{domain} ("privacy rights" OR "data subject request" OR "california privacy rights" OR '
        f'"do not sell my personal information")',
        f'site:{domain} ("privacy@" OR "data protection officer" OR "privacy inquiries" OR "contact privacy team")',
    ]
    return patterns[: config.BRAVE_SEARCH_QUERIES_PER_ATTEMPT]


class BraveSearchBackend(SearchBackend):
    def __init__(
        self, api_key: str, client: httpx.Client | None = None, timeout: float = 10.0,
        daily_budget: int | None = None,
    ):
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=timeout)
        # Defaulted from config at construction time (not read fresh per
        # call) so a single backend instance has one consistent budget for
        # its whole lifetime - matches how build_default_provider() builds
        # this once per process.
        self.budget = DailyQueryBudget(
            daily_budget if daily_budget is not None else config.BRAVE_SEARCH_DAILY_QUERY_BUDGET
        )

    def search(self, query: str) -> list[SearchHit]:
        try:
            resp = self._client.get(
                BRAVE_SEARCH_ENDPOINT,
                params={"q": query, "count": 10},
                headers={"Accept": "application/json", "X-Subscription-Token": self._api_key},
            )
        except httpx.HTTPError:
            return []
        if resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        results = data.get("web", {}).get("results", [])
        return [
            SearchHit(url=r.get("url", ""), title=r.get("title", ""), snippet=r.get("description", ""))
            for r in results
            if r.get("url")
        ]


def search_hits_to_candidates(hits: list[SearchHit]) -> list[CandidateSource]:
    return [
        CandidateSource(
            url=hit.url,
            kind=SourceType.OFFICIAL_PRIVACY_POLICY,
            discovered_via="search:brave",
            anchor_text=hit.title,
        )
        for hit in hits
    ]
