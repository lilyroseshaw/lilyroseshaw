import httpx

from app.research_fetch import PageFetcher

HTML = """<html><body>
<a href="/privacy">Privacy Policy</a>
<a href="https://shopexample.com/ccpa">Your CCPA Rights</a>
<a href="mailto:privacy@shopexample.com">Email us</a>
<a href="https://privacyportal.onetrust.com/webform/x">Submit a request</a>
<script>ignoreMe()</script>
<p>Some privacy text about delete my data.</p>
</body></html>"""


def _client(robots_text="", extra_routes=None):
    extra_routes = extra_routes or {}

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots_text)
        if request.url.path in extra_routes:
            return extra_routes[request.url.path]
        return httpx.Response(200, text=HTML, headers={"content-type": "text/html"})

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://shopexample.com")


def test_fetch_extracts_text_links_and_mailto():
    fetcher = PageFetcher(client=_client())
    page = fetcher.fetch("https://shopexample.com/privacy", "shopexample.com")
    assert "delete my data" in page.text.lower()
    assert ("https://shopexample.com/privacy", "Privacy Policy") in page.links
    assert ("https://shopexample.com/ccpa", "Your CCPA Rights") in page.links
    assert page.mailto_links == ["privacy@shopexample.com"]
    # scripts stripped from extracted text
    assert "ignoreMe" not in page.text


def test_fetch_separates_external_links_for_third_party_portal_detection():
    fetcher = PageFetcher(client=_client())
    page = fetcher.fetch("https://shopexample.com/privacy", "shopexample.com")
    assert any("onetrust.com" in href for href, _ in page.external_links)
    assert not any("onetrust.com" in href for href, _ in page.links)


def test_robots_txt_disallow_is_honored():
    fetcher = PageFetcher(client=_client(robots_text="User-agent: *\nDisallow: /blocked\n"))
    assert fetcher.fetch("https://shopexample.com/blocked", "shopexample.com") is None


def test_non_https_url_is_rejected():
    fetcher = PageFetcher(client=_client())
    assert fetcher.fetch("http://shopexample.com/privacy", "shopexample.com") is None


def test_non_200_response_returns_none():
    fetcher = PageFetcher(client=_client(extra_routes={"/missing": httpx.Response(404)}))
    assert fetcher.fetch("https://shopexample.com/missing", "shopexample.com") is None


def test_network_error_returns_none_not_raises():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://shopexample.com")
    fetcher = PageFetcher(client=client)
    assert fetcher.fetch("https://shopexample.com/privacy", "shopexample.com") is None
