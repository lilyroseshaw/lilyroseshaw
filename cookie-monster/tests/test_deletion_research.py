import httpx

from app.deletion_research import NullResearchProvider, WebResearchProvider
from app.deletion_constants import DeletionMethod
from app.research_crawl import SameDomainCrawler
from app.research_extract import RecipeExtractor
from app.research_fetch import PageFetcher
from app.research_types import ResearchResult

HOME_HTML = '<html><body><a href="/privacy">Privacy &amp; CCPA Rights</a></body></html>'
PRIVACY_HTML = """<html><body>
<p>California residents have the right to deletion. Please email us to submit a data rights request.</p>
<a href="mailto:privacy@shopexample.com">privacy@shopexample.com</a>
</body></html>"""


def _provider():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        if request.url.path == "/":
            return httpx.Response(200, text=HOME_HTML, headers={"content-type": "text/html"})
        if request.url.path == "/privacy":
            return httpx.Response(200, text=PRIVACY_HTML, headers={"content-type": "text/html"})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://shopexample.com")
    fetcher = PageFetcher(client=client)
    return WebResearchProvider(fetcher=fetcher, crawler=SameDomainCrawler(), extractor=RecipeExtractor())


def test_web_research_provider_finds_and_verifies_a_real_recipe():
    result = _provider().research("Shop Example", "shopexample.com")
    assert result is not None
    assert result.verified is True
    assert result.method == DeletionMethod.EMAIL_REQUEST
    assert result.email == "privacy@shopexample.com"
    assert result.source_url == "https://shopexample.com/privacy"


def test_web_research_provider_returns_none_when_nothing_found():
    def handler(request):
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://unknowncompany.com")
    provider = WebResearchProvider(fetcher=PageFetcher(client=client), crawler=SameDomainCrawler(), extractor=RecipeExtractor())
    assert provider.research("Unknown Co", "unknowncompany.com") is None


def test_verify_recipe_accepts_same_domain_source():
    provider = _provider()
    result = ResearchResult(
        domain="shopexample.com", method=DeletionMethod.EMAIL_REQUEST, email="privacy@shopexample.com",
        source_url="https://shopexample.com/privacy",
    )
    assert provider.verify_recipe("shopexample.com", result) is True


def test_verify_recipe_rejects_unrelated_domain_source():
    """A search result on someone else's site (a blog, a review) must never
    be accepted as the source of truth, even if extraction somehow produced
    a plausible-looking result from it."""
    provider = _provider()
    result = ResearchResult(
        domain="shopexample.com", method=DeletionMethod.EMAIL_REQUEST, email="privacy@shopexample.com",
        source_url="https://totally-unrelated-blog.com/some-review-of-shop-example",
    )
    assert provider.verify_recipe("shopexample.com", result) is False


def test_verify_recipe_accepts_third_party_portal_only_with_official_referral():
    provider = _provider()
    with_referral = ResearchResult(
        domain="shopexample.com", method=DeletionMethod.PRIVACY_PORTAL,
        source_url="https://privacyportal.onetrust.com/webform/x",
        referring_official_url="https://shopexample.com/privacy",
    )
    assert provider.verify_recipe("shopexample.com", with_referral) is True

    without_referral = ResearchResult(
        domain="shopexample.com", method=DeletionMethod.PRIVACY_PORTAL,
        source_url="https://privacyportal.onetrust.com/webform/x",
        referring_official_url=None,
    )
    assert provider.verify_recipe("shopexample.com", without_referral) is False

    wrong_referral = ResearchResult(
        domain="shopexample.com", method=DeletionMethod.PRIVACY_PORTAL,
        source_url="https://privacyportal.onetrust.com/webform/x",
        referring_official_url="https://someone-elses-site.com/page",
    )
    assert provider.verify_recipe("shopexample.com", wrong_referral) is False


def test_verify_recipe_rejects_non_https_source():
    provider = _provider()
    result = ResearchResult(
        domain="shopexample.com", method=DeletionMethod.WEB_FORM, source_url="http://shopexample.com/privacy",
    )
    assert provider.verify_recipe("shopexample.com", result) is False


def test_null_research_provider_never_returns_anything():
    provider = NullResearchProvider()
    assert provider.research("Any Company", "anycompany.com") is None
    assert provider.search_official_sources("Any Company", "anycompany.com") == []
    assert provider.inspect_privacy_page("https://anycompany.com/privacy", "anycompany.com") is None
