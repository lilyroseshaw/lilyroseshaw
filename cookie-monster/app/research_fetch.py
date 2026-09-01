"""Fetches a single public web page politely: HTTPS-only, timeout-bound,
robots.txt-respecting, honest User-Agent. This is the only place in the
research pipeline that makes an outbound HTTP request to a company's site.

Never fetches anything requiring login - only the public pages a company
already exposes to anyone (or any search engine), which is what a privacy
policy / deletion-request page always is.
"""
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app import config

USER_AGENT = "CookieMonsterResearchBot/1.0 (+personal privacy tool; researches a company's own published deletion process)"


@dataclass
class PageContent:
    url: str
    domain: str
    status_code: int
    text: str  # visible body text, boilerplate-ish but script/style stripped
    links: list[tuple[str, str]] = field(default_factory=list)  # (absolute_url, anchor_text), same-domain only
    # Off-domain links found on this (already domain-verified) page. Not evidence
    # by themselves, but this is how a legitimately-linked third-party privacy
    # portal is discovered - see research_extract.py's THIRD_PARTY_PORTAL_DOMAINS
    # check and DeletionRecipe.referring_official_url.
    external_links: list[tuple[str, str]] = field(default_factory=list)
    mailto_links: list[str] = field(default_factory=list)  # email addresses linked via mailto: on this page
    fetched_at: datetime = field(default_factory=datetime.utcnow)


def _extract_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return " ".join(soup.get_text(separator=" ").split())


def _extract_links(soup: BeautifulSoup, domain: str, base_url: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Returns (same_domain_links, external_links), both as (url, anchor_text)."""
    same_domain, external = [], []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        parsed = urlparse(href)
        if parsed.scheme != "https":
            continue
        anchor_text = a.get_text(strip=True)
        if parsed.netloc == domain or parsed.netloc.endswith("." + domain):
            same_domain.append((href, anchor_text))
        else:
            external.append((href, anchor_text))
    return same_domain, external


def _extract_mailto_links(soup: BeautifulSoup) -> list[str]:
    """Email addresses linked via mailto: on an already domain-verified page -
    this is how a real privacy-request email address is found; never guessed."""
    addresses = []
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("mailto:"):
            address = a["href"][len("mailto:"):].split("?")[0].strip()
            if address and address not in addresses:
                addresses.append(address)
    return addresses


class PageFetcher:
    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            timeout=config.RESEARCH_HTTP_TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        self._robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}

    def _robots_allows(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        parser = self._robots_cache.get(origin)
        if parser is None:
            parser = urllib.robotparser.RobotFileParser()
            try:
                resp = self._client.get(urljoin(origin, "/robots.txt"))
                if resp.status_code == 200:
                    parser.parse(resp.text.splitlines())
                else:
                    parser.parse([])  # no robots.txt -> nothing disallowed
            except httpx.HTTPError:
                parser.parse([])  # unreachable robots.txt -> fail open on this check only
            self._robots_cache[origin] = parser
        return parser.can_fetch(USER_AGENT, url)

    def fetch(self, url: str, domain: str) -> PageContent | None:
        """Returns None (never raises) on anything that isn't a clean, allowed,
        HTML 200 response - callers treat that as 'this page doesn't count as
        evidence', never as an error to propagate."""
        page, _status = self.fetch_with_status(url, domain)
        return page

    def fetch_with_status(self, url: str, domain: str) -> tuple["PageContent | None", int | None]:
        """Same as fetch(), but also returns the raw HTTP status code when a
        response was actually received - even on failure. Needed to tell a
        BLOCKING response (401/403/429 - the site is refusing automated
        access) apart from a genuine "not found" (404), which fetch() alone
        can't distinguish (see deletion_research.py's Tier B trigger logic).
        Returns (None, None) for anything that never got a real HTTP
        response at all - a network error, or robots.txt disallowing the
        request before it was ever made."""
        if not url.startswith("https://"):
            return None, None
        if not self._robots_allows(url):
            return None, None
        try:
            resp = self._client.get(url)
        except httpx.HTTPError:
            return None, None
        if resp.status_code != 200:
            return None, resp.status_code
        content_type = resp.headers.get("content-type", "")
        if "html" not in content_type and content_type != "":
            return None, resp.status_code

        soup = BeautifulSoup(resp.text, "lxml")
        same_domain_links, external_links = _extract_links(soup, domain, str(resp.url))
        page = PageContent(
            url=str(resp.url),
            domain=domain,
            status_code=resp.status_code,
            text=_extract_text(soup),
            links=same_domain_links,
            external_links=external_links,
            mailto_links=_extract_mailto_links(soup),
        )
        return page, resp.status_code
