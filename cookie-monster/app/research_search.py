"""Tier B source discovery: an optional search-engine fallback, used only
when the same-domain crawl (research_crawl.py) finds nothing. Behind a small
interface so a different backend can be swapped in without touching
deletion_research.py.

Search results are candidates, not trusted sources - every result still has
to be fetched and pass verify_recipe's official-domain check before it's
used for anything. A search result on someone else's domain (a blog, a
comparison site) is never accepted as a source of truth.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from app.deletion_constants import SourceType
from app.research_types import CandidateSource

BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


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


class BraveSearchBackend(SearchBackend):
    def __init__(self, api_key: str, client: httpx.Client | None = None, timeout: float = 10.0):
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=timeout)

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
