"""Tier A source discovery: crawl the company's own domain for a privacy/
deletion page. No external API, no cost, always on - this is what makes
WebResearchProvider produce real results without any key configured.

Every candidate here is a URL on the company's own already-verified domain
(from Gmail evidence) - a *path* may be guessed, but the resulting page is
always actually fetched and later verified before anything is trusted, so
this never becomes "guessing a URL and assuming it's right".
"""
from app.deletion_constants import SourceType
from app.research_fetch import PageFetcher
from app.research_types import CandidateSource

# Conventional paths worth trying directly - cheap, no crawl required.
COMMON_PATHS = [
    "/privacy",
    "/privacy-policy",
    "/privacy-rights",
    "/legal/privacy",
    "/legal/privacy-policy",
    "/ccpa",
    "/data-rights",
    "/privacy/requests",
    "/privacy/data-request",
    "/privacy-center",
    "/account/delete",
    "/help/privacy",
    "/support/privacy",
]

# Matched case-insensitively against a link's href or anchor text.
PRIVACY_KEYWORDS = [
    "privacy", "ccpa", "cpra", "delete my data", "delete account",
    "right to deletion", "right to be forgotten", "data rights",
    "data request", "do not sell", "personal information", "opt-out",
    "consumer privacy",
]


def _guess_kind(url: str, anchor_text: str) -> str:
    haystack = f"{url} {anchor_text}".lower()
    if "delete" in haystack or "right to" in haystack or "deletion" in haystack:
        return SourceType.OFFICIAL_DELETION_PAGE
    if "ccpa" in haystack or "cpra" in haystack or "data-rights" in haystack or "privacy-rights" in haystack:
        return SourceType.OFFICIAL_PRIVACY_RIGHTS_PAGE
    if "help" in haystack or "support" in haystack:
        return SourceType.OFFICIAL_SUPPORT_DOCS
    return SourceType.OFFICIAL_PRIVACY_POLICY


class SameDomainCrawler:
    def __init__(self, max_candidates: int = 8):
        self._max_candidates = max_candidates

    def discover(self, domain: str, fetcher: PageFetcher) -> list[CandidateSource]:
        candidates: list[CandidateSource] = []
        seen_urls: set[str] = set()

        homepage = fetcher.fetch(f"https://{domain}/", domain)
        if homepage:
            for href, anchor_text in homepage.links:
                haystack = f"{href} {anchor_text}".lower()
                if any(keyword in haystack for keyword in PRIVACY_KEYWORDS) and href not in seen_urls:
                    candidates.append(
                        CandidateSource(
                            url=href,
                            kind=_guess_kind(href, anchor_text),
                            discovered_via="homepage_link",
                            anchor_text=anchor_text,
                        )
                    )
                    seen_urls.add(href)

        for path in COMMON_PATHS:
            url = f"https://{domain}{path}"
            if url not in seen_urls:
                candidates.append(
                    CandidateSource(url=url, kind=_guess_kind(url, ""), discovered_via="common_path_guess")
                )
                seen_urls.add(url)

        return candidates[: self._max_candidates]
