import httpx

from app.research_crawl import COMMON_PATHS, SameDomainCrawler
from app.research_fetch import PageFetcher

HOME_HTML = """<html><body>
<a href="/privacy">Privacy &amp; Your CCPA Rights</a>
<a href="/about">About us</a>
</body></html>"""


def _client():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if request.url.path == "/":
            return httpx.Response(200, text=HOME_HTML, headers={"content-type": "text/html"})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://shopexample.com")


def test_homepage_link_found_before_common_path_guesses():
    fetcher = PageFetcher(client=_client())
    candidates = SameDomainCrawler().discover("shopexample.com", fetcher)
    assert candidates[0].url == "https://shopexample.com/privacy"
    assert candidates[0].discovered_via == "homepage_link"


def test_irrelevant_homepage_links_are_ignored():
    fetcher = PageFetcher(client=_client())
    candidates = SameDomainCrawler().discover("shopexample.com", fetcher)
    assert not any(c.url.endswith("/about") for c in candidates)


def test_common_paths_fill_in_after_homepage_links():
    fetcher = PageFetcher(client=_client())
    candidates = SameDomainCrawler(max_candidates=20).discover("shopexample.com", fetcher)
    common_path_urls = {c.url for c in candidates if c.discovered_via == "common_path_guess"}
    # /privacy (COMMON_PATHS[0]) was already found via the homepage link, so
    # it's deduped to that discovery, not double-listed as a guess too.
    assert f"https://shopexample.com{COMMON_PATHS[0]}" not in common_path_urls
    assert f"https://shopexample.com{COMMON_PATHS[1]}" in common_path_urls


def test_candidates_are_capped():
    fetcher = PageFetcher(client=_client())
    candidates = SameDomainCrawler(max_candidates=3).discover("shopexample.com", fetcher)
    assert len(candidates) == 3


def test_no_homepage_falls_back_to_common_paths_only():
    def handler(request):
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://shopexample.com")
    fetcher = PageFetcher(client=client)
    candidates = SameDomainCrawler().discover("shopexample.com", fetcher)
    assert all(c.discovered_via == "common_path_guess" for c in candidates)
    assert len(candidates) > 0
