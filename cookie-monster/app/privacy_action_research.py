"""Discovers and verifies company-specific mechanisms for the two Just the
Essentials PrivacyAction types - the research pipeline app/privacy_action.py
always lacked (see its module docstring: "no research pipeline exists yet").

Deliberately a SEPARATE pipeline from deletion_research.py/
deletion_resolver.py, never a repurposing of it: a company's verified FULL
DELETION mechanism (an email address, a portal, an account-settings page for
deleting the account and its personal data) is not automatically a valid
mechanism for a completely different privacy outcome - stopping nonessential
tracking, or opting out of sale/sharing/targeted advertising. This module
never reads DeletionRecipe/Company.deletion_* fields as if they applied
here, and never writes to them.

Same safety/verification philosophy as deletion_research.py:
  - Tier A (same-domain crawl, always on) before Tier B (Brave Search,
    optional, only after Tier A fails).
  - A result is only VERIFIED if its source is the company's own domain, or
    a third-party portal explicitly linked to from an already domain-
    verified official page (see verify_mechanism) - never an arbitrary
    search result, blog, aggregator, or data-broker site.
  - No LLM/AI extraction - regex/keyword heuristics only (deliberately no
    Pass 2, unlike research_extract.py's optional LLM pass - see the Just
    the Essentials research-pipeline scope guard: no AI/LLM dependencies).
  - Never fabricates a URL: if nothing verifiable is found, research()
    returns None and the caller (privacy_action_resolver.py) marks the
    PrivacyAction NEEDS_REVIEW, never guesses.

CRITICAL - finding a mechanism is not proof of what it actually
accomplishes. A "Do Not Sell/Share" control does not prove analytics/
profile data was deleted; a cookie/ad-preference control does not prove
server-side historical data was removed. See TRACKING_CLEANUP_SCOPE_NOTE/
OPT_OUT_SCOPE_NOTE below, always attached to a verified result so the UI
can never overstate what was actually found.
"""
import re
from abc import ABC, abstractmethod
from urllib.parse import urlparse

from app import config
from app.classifier import normalize_domain
from app.deletion_constants import DeletionMethod, PrivacyActionType, SourceType
from app.research_fetch import PageContent, PageFetcher
from app.research_search import BraveBudgetExhausted, SearchBackend, search_hits_to_candidates
from app.research_types import CandidateSource, PrivacyActionResult

# Domains known to host third-party consent-management/opt-out portals - a
# link to one of these found on an already domain-verified official page is
# acceptable evidence (see verify_mechanism), but only via that link, never
# guessed. Deliberately a separate list from research_extract.py's
# THIRD_PARTY_PORTAL_DOMAINS (a full-deletion-request list): several of
# these vendors sell both a privacy-request product AND a distinct consent/
# ad-choices product, and this list is scoped to the latter.
THIRD_PARTY_PRIVACY_PORTAL_DOMAINS = [
    "onetrust.com", "cookielaw.org", "privacyportal", "osano.com", "trustarc.com",
    "cookiebot.com", "consensu.org", "transcend.io", "securiti.ai", "didomi.io",
    "usercentrics.com", "quantcast.com",
]

_LOGIN_REQUIRED_PATTERNS = [
    r"sign in to (your account|manage)", r"log ?in to (your account|manage)",
    r"from your account settings", r"in your account, (go to|navigate to)",
    r"account settings",
]

_TRACKING_SIGNAL_PATTERNS = [
    r"cookie preferences", r"cookie settings", r"manage (your )?cookies",
    r"ad preferences", r"advertising preferences", r"personalized advertising",
    r"interest-based advertising", r"interest based ads", r"tracking preferences",
    r"privacy preference center", r"analytics (preferences|settings|opt-?out)",
    r"\bad choices\b", r"your ad choices", r"manage your privacy",
]

_OPT_OUT_SIGNAL_PATTERNS = [
    r"do not sell", r"do not sell or share", r"do not sell my personal information",
    r"opt-?out of (the )?sale", r"your privacy choices", r"california privacy rights",
    r"limit the use of my sensitive personal information", r"targeted advertising opt-?out",
    r"cross-context behavioral advertising", r"global privacy control",
]

_TRACKING_PATHS = [
    "/privacy/preferences", "/cookie-preferences", "/cookies", "/manage-cookies",
    "/ad-preferences", "/privacy-choices", "/your-privacy-choices", "/privacy/ad-choices",
    "/adchoices",
]
_OPT_OUT_PATHS = [
    "/privacy-choices", "/your-privacy-choices", "/do-not-sell", "/do-not-sell-or-share",
    "/ccpa", "/opt-out", "/privacy/opt-out", "/privacy-rights",
]

_ANCHOR_KEYWORDS = {
    PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP: [
        "cookie", "tracking", "ad preferences", "advertising preferences",
        "personalized ads", "ad choices", "analytics preferences",
    ],
    PrivacyActionType.SALE_SHARING_OPT_OUT: [
        "do not sell", "privacy choices", "opt-out", "opt out", "ccpa", "privacy rights",
    ],
}
_SIGNAL_PATTERNS = {
    PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP: _TRACKING_SIGNAL_PATTERNS,
    PrivacyActionType.SALE_SHARING_OPT_OUT: _OPT_OUT_SIGNAL_PATTERNS,
}
_COMMON_PATHS = {
    PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP: _TRACKING_PATHS,
    PrivacyActionType.SALE_SHARING_OPT_OUT: _OPT_OUT_PATHS,
}

# Truthful limitation, ALWAYS attached to a verified result - see module
# docstring. Deliberately one per action_type, not per exact mechanism:
# every mechanism this pipeline can find for either type is a *control*,
# never itself proof of historical/derived-data deletion.
TRACKING_CLEANUP_SCOPE_NOTE = (
    "This can stop some future nonessential tracking (like personalized ads or analytics), "
    "but it does not by itself confirm that previously collected profiling or analytics data "
    "has been deleted."
)
OPT_OUT_SCOPE_NOTE = (
    "This can register an opt-out from the sale/sharing of your data or cross-context "
    "behavioral advertising, but it does not by itself confirm that data already shared "
    "with other companies has been recalled or deleted."
)
_SCOPE_NOTES = {
    PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP: TRACKING_CLEANUP_SCOPE_NOTE,
    PrivacyActionType.SALE_SHARING_OPT_OUT: OPT_OUT_SCOPE_NOTE,
}


def scope_note_for(action_type: str) -> str:
    return _SCOPE_NOTES.get(action_type, "")


class PrivacyMechanismSourceBlocked(Exception):
    """Mirrors deletion_research.SourceBlockedDiscovery for this separate
    pipeline: an own-domain candidate was found but our fetcher couldn't
    reach it (401/403/429) - never bypassed, kept only as a manual-review
    lead (see privacy_action_resolver.py)."""

    def __init__(self, url: str):
        self.url = url
        super().__init__(f"source blocked: {url}")


class PrivacyMechanismUnverified(Exception):
    """Mirrors deletion_research.UnverifiedPortalDiscovery: a candidate
    produced a result but verify_mechanism rejected it - kept only as a
    manual-review lead, never accepted."""

    def __init__(self, url: str | None):
        self.url = url
        super().__init__(f"unverified candidate: {url}")


def verify_mechanism(domain: str, result: PrivacyActionResult) -> bool:
    """Same official-source rule as deletion_research.py's verify_recipe:
    a result is only trusted if it comes from the company's own domain, or
    a third-party portal reached via a link on an already domain-verified
    official page. An arbitrary search result, blog, aggregator, or
    data-broker site is never accepted, no matter how plausible its
    content looks."""
    if not result.source_url or not result.source_url.startswith("https://"):
        return False
    source_domain = normalize_domain(urlparse(result.source_url).netloc)
    if source_domain == domain:
        return True
    if result.referring_official_url:
        referring_domain = normalize_domain(urlparse(result.referring_official_url).netloc)
        if referring_domain == domain:
            return True
    return False


def _discover_candidates(domain: str, action_type: str, fetcher: PageFetcher) -> list[CandidateSource]:
    """Tier A: same-domain crawl, tailored to action_type's own keyword/
    path vocabulary. Deliberately NOT deletion_research's SameDomainCrawler,
    which is scoped to full-deletion-request language and would bias
    discovery toward the wrong mechanism entirely."""
    candidates: list[CandidateSource] = []
    seen: set[str] = set()
    anchor_keywords = _ANCHOR_KEYWORDS[action_type]

    homepage = fetcher.fetch(f"https://{domain}/", domain)
    if homepage:
        for href, anchor_text in homepage.links:
            haystack = f"{href} {anchor_text}".lower()
            if any(k in haystack for k in anchor_keywords) and href not in seen:
                candidates.append(
                    CandidateSource(
                        url=href, kind=SourceType.OFFICIAL_PRIVACY_POLICY,
                        discovered_via="homepage_link", anchor_text=anchor_text,
                    )
                )
                seen.add(href)

    for path in _COMMON_PATHS[action_type]:
        url = f"https://{domain}{path}"
        if url not in seen:
            candidates.append(
                CandidateSource(url=url, kind=SourceType.OFFICIAL_PRIVACY_POLICY, discovered_via="common_path_guess")
            )
            seen.add(url)

    return candidates[:8]


def _extract(domain: str, action_type: str, pages: list[PageContent]) -> PrivacyActionResult | None:
    """Pass 1 only (regex/keyword heuristics) - no LLM pass, deliberately,
    for this pipeline (see module docstring). Never fabricates a URL: only
    ever returns a page/portal link that was actually fetched or actually
    linked to from a fetched page."""
    patterns = _SIGNAL_PATTERNS[action_type]
    for page in pages:
        text_lower = page.text.lower()
        signal = next((p for p in patterns if re.search(p, text_lower)), None)
        if not signal:
            continue

        portal_link = next(
            (href for href, _ in page.external_links if any(d in href for d in THIRD_PARTY_PRIVACY_PORTAL_DOMAINS)),
            None,
        )
        login_signal = any(re.search(p, text_lower) for p in _LOGIN_REQUIRED_PATTERNS)

        reasons = [f"page text matched pattern: /{signal}/"]
        if portal_link:
            reasons.append(f"official page links to known privacy-portal domain: {portal_link}")
            method = DeletionMethod.PRIVACY_PORTAL
        elif login_signal:
            reasons.append(f"login/account-settings required per: /{[p for p in _LOGIN_REQUIRED_PATTERNS if re.search(p, text_lower)][0]}/")
            method = DeletionMethod.ACCOUNT_SETTING
        else:
            method = DeletionMethod.WEB_FORM

        return PrivacyActionResult(
            domain=domain, action_type=action_type, method=method,
            url=portal_link or page.url,
            login_required=bool(login_signal) if not portal_link else None,
            source_url=portal_link or page.url,
            referring_official_url=page.url if portal_link else None,
            source_type=SourceType.THIRD_PARTY_VIA_OFFICIAL_LINK if portal_link else SourceType.OFFICIAL_PRIVACY_POLICY,
            confidence="high" if (portal_link or login_signal) else "medium",
            scope_note=scope_note_for(action_type),
            reasons=reasons,
        )
    return None


class PrivacyActionResearchProvider(ABC):
    @abstractmethod
    def research(self, domain: str, action_type: str) -> PrivacyActionResult | None:
        ...


class NullPrivacyActionResearchProvider(PrivacyActionResearchProvider):
    """Makes no outbound requests at all. Used when
    config.DELETION_RESEARCH_ENABLED is false - always reports 'nothing
    found', so nothing is ever fabricated."""

    def research(self, domain: str, action_type: str) -> PrivacyActionResult | None:
        return None


_BLOCKING_STATUSES = (401, 403, 429)


class WebPrivacyActionResearchProvider(PrivacyActionResearchProvider):
    """The real implementation. Tier A (crawl) always runs; Tier B (Brave
    Search) is optional and only triggers once Tier A has demonstrably
    failed to produce a verified result."""

    def __init__(self, fetcher: PageFetcher | None = None, search_backend: SearchBackend | None = None):
        self._fetcher = fetcher or PageFetcher()
        self._search_backend = search_backend

    def research(self, domain: str, action_type: str) -> PrivacyActionResult | None:
        candidates = _discover_candidates(domain, action_type, self._fetcher)
        pages: list[PageContent] = []
        for candidate in candidates[: config.RESEARCH_MAX_PAGES_PER_COMPANY]:
            page, _status = self._fetcher.fetch_with_status(candidate.url, domain)
            if page is not None:
                pages.append(page)

        if pages:
            result = _extract(domain, action_type, pages)
            if result is not None and verify_mechanism(domain, result):
                result.verified = True
                return result

        if self._search_backend is None:
            return None
        return self._research_via_search(domain, action_type)

    def _research_via_search(self, domain: str, action_type: str) -> PrivacyActionResult | None:
        if not self._search_backend.budget.try_consume(config.BRAVE_SEARCH_QUERIES_PER_ATTEMPT):
            raise BraveBudgetExhausted()

        hits = []
        for query in _brave_query_patterns(domain, action_type):
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
            result = _extract(domain, action_type, pages)
            if result is not None:
                if verify_mechanism(domain, result):
                    result.verified = True
                    return result
                raise PrivacyMechanismUnverified(result.source_url)

        if blocked_official_url:
            raise PrivacyMechanismSourceBlocked(blocked_official_url)

        return None


def _brave_query_patterns(domain: str, action_type: str) -> list[str]:
    """Site-scoped discovery queries, distinct per action_type so a
    tracking-cleanup lookup is never biased toward opt-out language or vice
    versa - see the module-level concept list this mirrors."""
    if action_type == PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP:
        patterns = [
            f'site:{domain} ("cookie preferences" OR "cookie settings" OR "ad preferences" OR "advertising preferences")',
            f'site:{domain} ("personalized advertising" OR "interest-based advertising" OR "tracking preferences")',
            f'site:{domain} ("privacy preference center" OR "manage your privacy" OR "ad choices")',
        ]
    else:
        patterns = [
            f'site:{domain} ("do not sell" OR "do not sell or share" OR "opt-out of sale")',
            f'site:{domain} ("your privacy choices" OR "california privacy rights" OR CCPA)',
            f'site:{domain} ("targeted advertising opt-out" OR "global privacy control")',
        ]
    return patterns[: config.BRAVE_SEARCH_QUERIES_PER_ATTEMPT]


def build_default_privacy_action_provider(
    search_backend: SearchBackend | None = None,
) -> PrivacyActionResearchProvider:
    """Constructs the provider used app-wide. Zero keys set -> still works
    (Tier A only). DELETION_RESEARCH_ENABLED=false -> no outbound requests
    at all - the SAME flag deletion_research.py's build_default_provider
    uses, since it gates whether this prototype makes web requests at all,
    not a deletion-specific setting.

    `search_backend`, when given, is shared with the Full Clean pipeline's
    own provider (see main.py) so both draw from ONE Brave daily query
    budget rather than silently doubling real API usage against the same
    key."""
    if not config.DELETION_RESEARCH_ENABLED:
        return NullPrivacyActionResearchProvider()
    return WebPrivacyActionResearchProvider(search_backend=search_backend)
