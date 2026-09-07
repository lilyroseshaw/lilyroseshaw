"""Tests for the Just the Essentials research pipeline
(app/privacy_action_research.py) - a SEPARATE pipeline from Full Clean's
deletion_research.py, never a repurposing of it. Fabricated companies only
("Amazon" here is a fabricated test double at amazon-fixture.com, never
the real amazon.com - see test_amazon_shaped_tracking_and_opt_out_mixed_
outcome). No live network access - every fetch goes through
httpx.MockTransport.
"""
import httpx
import pytest

from app.deletion_constants import DeletionMethod, PrivacyActionType
from app.privacy_action_research import (
    OPT_OUT_SCOPE_NOTE,
    TRACKING_CLEANUP_SCOPE_NOTE,
    PrivacyMechanismSourceBlocked,
    PrivacyMechanismUnverified,
    WebPrivacyActionResearchProvider,
    _is_exact_official_host,
    scope_note_for,
    verify_mechanism,
)
from app.research_search import BraveBudgetExhausted, DailyQueryBudget, SearchHit
from app.research_types import PrivacyActionResult


def _provider(domain: str, handler, search_backend=None) -> WebPrivacyActionResearchProvider:
    from app.research_fetch import PageFetcher

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=f"https://{domain}")
    fetcher = PageFetcher(client=client)
    return WebPrivacyActionResearchProvider(fetcher=fetcher, search_backend=search_backend)


class _FakeBrave:
    def __init__(self, hits_by_query=None, daily_budget=100):
        self.hits_by_query = hits_by_query or {}
        self.budget = DailyQueryBudget(daily_budget)
        self.queries_made: list[str] = []

    def search(self, query: str) -> list[SearchHit]:
        self.queries_made.append(query)
        return self.hits_by_query.get(query, [])


# --- official-domain verification -------------------------------------------

def test_verify_mechanism_accepts_same_domain_source():
    result = PrivacyActionResult(
        domain="shopexample.com", action_type=PrivacyActionType.SALE_SHARING_OPT_OUT,
        method=DeletionMethod.WEB_FORM, source_url="https://shopexample.com/privacy-choices",
    )
    assert verify_mechanism("shopexample.com", result) is True


def test_verify_mechanism_rejects_non_https_source():
    result = PrivacyActionResult(
        domain="shopexample.com", action_type=PrivacyActionType.SALE_SHARING_OPT_OUT,
        method=DeletionMethod.WEB_FORM, source_url="http://shopexample.com/privacy-choices",
    )
    assert verify_mechanism("shopexample.com", result) is False


# --- official company-linked third-party mechanism ---------------------------

def test_verify_mechanism_accepts_third_party_portal_only_with_official_referral():
    with_referral = PrivacyActionResult(
        domain="shopexample.com", action_type=PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP,
        method=DeletionMethod.PRIVACY_PORTAL,
        source_url="https://privacyportal.onetrust.com/webform/cookies",
        referring_official_url="https://shopexample.com/privacy",
    )
    assert verify_mechanism("shopexample.com", with_referral) is True

    without_referral = PrivacyActionResult(
        domain="shopexample.com", action_type=PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP,
        method=DeletionMethod.PRIVACY_PORTAL,
        source_url="https://privacyportal.onetrust.com/webform/cookies",
        referring_official_url=None,
    )
    assert verify_mechanism("shopexample.com", without_referral) is False


# --- unrelated third-party result rejected -----------------------------------

def test_verify_mechanism_rejects_unrelated_domain_source():
    """A search result on someone else's site (a blog, an aggregator, a
    data-broker listing) must never be accepted as the source of truth."""
    result = PrivacyActionResult(
        domain="shopexample.com", action_type=PrivacyActionType.SALE_SHARING_OPT_OUT,
        method=DeletionMethod.WEB_FORM,
        source_url="https://totally-unrelated-data-broker.com/profile-of-shop-example",
    )
    assert verify_mechanism("shopexample.com", result) is False


def test_wrong_referral_domain_is_rejected():
    result = PrivacyActionResult(
        domain="shopexample.com", action_type=PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP,
        method=DeletionMethod.PRIVACY_PORTAL,
        source_url="https://privacyportal.onetrust.com/webform/x",
        referring_official_url="https://someone-elses-site.com/page",
    )
    assert verify_mechanism("shopexample.com", result) is False


# --- discovery finds a real, verifiable mechanism ----------------------------

def test_finds_tracking_cleanup_mechanism_on_own_domain():
    domain = "shopexample.com"
    home_html = '<html><body><a href="/privacy/preferences">Cookie Preferences</a></body></html>'
    prefs_html = "<html><body><p>Manage your cookie preferences and ad preferences below.</p></body></html>"

    def handler(request):
        if request.url.path == "/":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=home_html)
        if request.url.path == "/privacy/preferences":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=prefs_html)
        return httpx.Response(404)

    provider = _provider(domain, handler)
    result = provider.research(domain, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)

    assert result is not None
    assert result.verified is True
    assert result.method == DeletionMethod.WEB_FORM
    assert result.url == f"https://{domain}/privacy/preferences"
    assert result.scope_note == TRACKING_CLEANUP_SCOPE_NOTE


def test_finds_opt_out_mechanism_requiring_login():
    domain = "shopexample.com"
    home_html = '<html><body><a href="/privacy-choices">Your Privacy Choices</a></body></html>'
    page_html = (
        "<html><body><p>To exercise your right to opt-out of the sale or sharing of your "
        "personal information (do not sell or share), sign in to your account settings.</p></body></html>"
    )

    def handler(request):
        if request.url.path == "/":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=home_html)
        if request.url.path == "/privacy-choices":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=page_html)
        return httpx.Response(404)

    provider = _provider(domain, handler)
    result = provider.research(domain, PrivacyActionType.SALE_SHARING_OPT_OUT)

    assert result is not None
    assert result.verified is True
    assert result.method == DeletionMethod.ACCOUNT_SETTING
    assert result.scope_note == OPT_OUT_SCOPE_NOTE


def test_finds_third_party_cmp_portal_linked_from_official_page():
    domain = "shopexample.com"
    home_html = '<html><body><a href="/cookies">Cookie Settings</a></body></html>'
    page_html = (
        '<html><body><p>Manage your cookie preferences and ad preferences using our '
        'preference center.</p><a href="https://privacyportal.onetrust.com/webform/shopexample-cookies">Cookie Settings</a>'
        "</body></html>"
    )

    def handler(request):
        if request.url.path == "/":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=home_html)
        if request.url.path == "/cookies":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=page_html)
        return httpx.Response(404)

    provider = _provider(domain, handler)
    result = provider.research(domain, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)

    assert result is not None
    assert result.verified is True
    assert result.method == DeletionMethod.PRIVACY_PORTAL
    assert result.url == "https://privacyportal.onetrust.com/webform/shopexample-cookies"
    assert result.referring_official_url == f"https://{domain}/cookies"


def test_returns_none_when_nothing_found():
    domain = "unknowncompany.com"

    def handler(request):
        return httpx.Response(404)

    provider = _provider(domain, handler)
    assert provider.research(domain, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP) is None
    assert provider.research(domain, PrivacyActionType.SALE_SHARING_OPT_OUT) is None


# --- sale/share mechanism does not imply tracking/profile deletion ----------
# --- cookie controls do not imply server-side deletion ----------------------
# --- advertising preference control does not imply historical-data deletion -

def test_scope_notes_are_truthful_and_distinct_per_action_type():
    tracking_note = scope_note_for(PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    opt_out_note = scope_note_for(PrivacyActionType.SALE_SHARING_OPT_OUT)

    assert tracking_note != opt_out_note
    assert "does not" in tracking_note.lower()
    assert "does not" in opt_out_note.lower()
    # The tracking note must never claim historical/derived data was
    # deleted just because a future-facing control was found.
    assert "deleted" in tracking_note.lower()
    # The opt-out note must never claim analytics/profile data was
    # deleted just because a sale/sharing opt-out was found.
    assert "sale" in opt_out_note.lower() or "sharing" in opt_out_note.lower()


def test_every_verified_tracking_result_carries_the_truthful_scope_note():
    domain = "adtechshop.com"
    home_html = '<html><body><a href="/ad-preferences">Ad Preferences</a></body></html>'
    page_html = "<html><body><p>Manage your personalized advertising and interest-based advertising here.</p></body></html>"

    def handler(request):
        if request.url.path == "/":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=home_html)
        if request.url.path == "/ad-preferences":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=page_html)
        return httpx.Response(404)

    provider = _provider(domain, handler)
    result = provider.research(domain, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    assert result is not None
    assert "personalized ads" in result.scope_note.lower() or "analytics" in result.scope_note.lower()
    assert "deleted" in result.scope_note.lower()


def test_every_verified_opt_out_result_carries_the_truthful_scope_note():
    domain = "databroker-client.com"
    home_html = '<html><body><a href="/do-not-sell">Do Not Sell My Info</a></body></html>'
    page_html = "<html><body><p>Submit a request to opt-out of the sale of your personal information.</p></body></html>"

    def handler(request):
        if request.url.path == "/":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=home_html)
        if request.url.path == "/do-not-sell":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=page_html)
        return httpx.Response(404)

    provider = _provider(domain, handler)
    result = provider.research(domain, PrivacyActionType.SALE_SHARING_OPT_OUT)
    assert result is not None
    assert "recalled" in result.scope_note.lower() or "deleted" in result.scope_note.lower()


# --- Tier B (Brave) discovery, budget, and blocked/unverified handling ------

def test_tier_b_triggers_after_tier_a_failure():
    """The real mechanism lives at a URL Tier A's own guessed common paths
    (_OPT_OUT_PATHS) would never try - discoverable only via Brave, same
    shape as deletion_research's own Tier-B tests."""
    domain = "hardsite.com"
    brave = _FakeBrave()
    hit = SearchHit(url=f"https://{domain}/legal/privacy-choices-form", title="Privacy Choices", snippet="")
    for q in [
        f'site:{domain} ("do not sell" OR "do not sell or share" OR "opt-out of sale")',
        f'site:{domain} ("your privacy choices" OR "california privacy rights" OR CCPA)',
        f'site:{domain} ("targeted advertising opt-out" OR "global privacy control")',
    ]:
        brave.hits_by_query[q] = [hit]

    def handler(request):
        if request.url.path == "/legal/privacy-choices-form":
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html><body><p>Your privacy choices: opt-out of sale.</p></body></html>")
        return httpx.Response(403)  # homepage and every guessed common path blocked

    provider = _provider(domain, handler, search_backend=brave)
    result = provider.research(domain, PrivacyActionType.SALE_SHARING_OPT_OUT)

    assert result is not None
    assert result.verified is True
    assert result.source_url == f"https://{domain}/legal/privacy-choices-form"
    assert brave.queries_made  # Tier B genuinely ran


def test_budget_exhausted_raises_before_any_brave_call():
    domain = "hardsite.com"
    brave = _FakeBrave(daily_budget=1)
    brave.budget.try_consume(1)  # pre-exhausted

    def handler(request):
        return httpx.Response(403)

    provider = _provider(domain, handler, search_backend=brave)
    with pytest.raises(BraveBudgetExhausted):
        provider.research(domain, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    assert brave.queries_made == []


def test_source_blocked_kept_as_manual_review_lead():
    domain = "blockedco.com"
    brave = _FakeBrave()
    hit = SearchHit(url=f"https://{domain}/privacy-choices", title="Privacy Choices", snippet="")
    for q in [
        f'site:{domain} ("do not sell" OR "do not sell or share" OR "opt-out of sale")',
        f'site:{domain} ("your privacy choices" OR "california privacy rights" OR CCPA)',
        f'site:{domain} ("targeted advertising opt-out" OR "global privacy control")',
    ]:
        brave.hits_by_query[q] = [hit]

    def handler(request):
        return httpx.Response(403)  # everything, including the Brave-found URL

    provider = _provider(domain, handler, search_backend=brave)
    with pytest.raises(PrivacyMechanismSourceBlocked) as exc_info:
        provider.research(domain, PrivacyActionType.SALE_SHARING_OPT_OUT)
    assert exc_info.value.url == f"https://{domain}/privacy-choices"


def test_unverified_external_portal_kept_as_manual_review_lead_not_verified():
    domain = "portalco.com"
    brave = _FakeBrave()
    for q in [
        f'site:{domain} ("cookie preferences" OR "cookie settings" OR "ad preferences" OR "advertising preferences")',
        f'site:{domain} ("personalized advertising" OR "interest-based advertising" OR "tracking preferences")',
        f'site:{domain} ("privacy preference center" OR "manage your privacy" OR "ad choices")',
    ]:
        brave.hits_by_query[q] = [SearchHit(url="https://privacyportal.onetrust.com/webform/portalco-cookies", title="Cookies", snippet="")]

    def router(request):
        if request.url.host == domain:
            return httpx.Response(404)  # Tier A: nothing
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html><body><p>Manage your cookie preferences here.</p></body></html>")

    from app.research_fetch import PageFetcher
    client = httpx.Client(transport=httpx.MockTransport(router), base_url=f"https://{domain}")
    provider = WebPrivacyActionResearchProvider(fetcher=PageFetcher(client=client), search_backend=brave)

    with pytest.raises(PrivacyMechanismUnverified) as exc_info:
        provider.research(domain, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    assert exc_info.value.url == "https://privacyportal.onetrust.com/webform/portalco-cookies"


# --- a fabricated Amazon-shaped test case: mixed outcome --------------------

def test_amazon_shaped_tracking_and_opt_out_mixed_outcome():
    """A FABRICATED test double ('Amazon' as a company name, at
    amazon-fixture.com - never the real amazon.com) shaped like the real
    kind of finding this pipeline should produce: an advertising-
    preferences control is findable and verifiable (so tracking cleanup
    becomes actionable), but no do-not-sell/opt-out language exists
    anywhere on the fabricated site (so the opt-out action correctly stays
    unresolved) - demonstrating a truthful, non-fabricated MIXED outcome
    across the two action types, resolved fully independently."""
    domain = "amazon-fixture.com"
    home_html = (
        '<html><body>'
        '<a href="/privacy/ad-preferences">Advertising Preferences</a>'
        '</body></html>'
    )
    ad_prefs_html = (
        "<html><body><p>Manage your personalized advertising and interest-based "
        "advertising preferences below.</p></body></html>"
    )

    def handler(request):
        if request.url.path == "/":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=home_html)
        if request.url.path == "/privacy/ad-preferences":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=ad_prefs_html)
        return httpx.Response(404)  # no do-not-sell page exists on this fabricated site

    provider = _provider(domain, handler)

    tracking_result = provider.research(domain, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    opt_out_result = provider.research(domain, PrivacyActionType.SALE_SHARING_OPT_OUT)

    assert tracking_result is not None
    assert tracking_result.verified is True
    assert tracking_result.scope_note  # truthfully caveated, never a bare "done"

    assert opt_out_result is None  # honestly unresolved - never fabricated


# --- Live-Amazon-test regression: a related corporate domain (aws.amazon.com
# for amazon.com's own consumer service) must never be accepted as a
# verified mechanism just because it shares a registrable root, discusses
# the relevant privacy law, or carries privacy keywords. Fixtures below use
# "amazon-fixture.com"/"aws.amazon-fixture.com" as an explicitly FABRICATED
# stand-in shaped like the real bug - never a request to any real domain.

def test_related_corporate_domain_alone_is_insufficient():
    """Same registrable root (amazon-fixture.com) is NOT the same host as
    the target's own canonical site - a related corporate subdomain must
    fail verification with no referral, even though normalize_domain()
    would collapse both to the same registrable root."""
    result = PrivacyActionResult(
        domain="amazon-fixture.com", action_type=PrivacyActionType.SALE_SHARING_OPT_OUT,
        method=DeletionMethod.WEB_FORM,
        source_url="https://aws.amazon-fixture.com/compliance/california-consumer-privacy-act/",
        referring_official_url=None,
    )
    assert verify_mechanism("amazon-fixture.com", result) is False


def test_is_exact_official_host_rejects_related_subdomains():
    assert _is_exact_official_host("amazon-fixture.com", "amazon-fixture.com") is True
    assert _is_exact_official_host("amazon-fixture.com", "www.amazon-fixture.com") is True
    assert _is_exact_official_host("amazon-fixture.com", "aws.amazon-fixture.com") is False
    assert _is_exact_official_host("google-fixture.com", "cloud.google-fixture.com") is False


def test_informational_compliance_page_is_not_an_actionable_mechanism_end_to_end():
    """An affiliate compliance/informational page discussing the relevant
    privacy law (CCPA, "california privacy rights") in depth, discoverable
    only via search (never linked from the target's own official page),
    must never resolve as a verified mechanism - discussing the law and
    carrying privacy keywords is not evidence it applies to the target
    consumer service."""
    domain = "amazon-fixture.com"
    brave = _FakeBrave()
    hit = SearchHit(
        url="https://aws.amazon-fixture.com/compliance/california-consumer-privacy-act/",
        title="AWS CCPA Compliance", snippet="",
    )
    for q in [
        f'site:{domain} ("do not sell" OR "do not sell or share" OR "opt-out of sale")',
        f'site:{domain} ("your privacy choices" OR "california privacy rights" OR CCPA)',
        f'site:{domain} ("targeted advertising opt-out" OR "global privacy control")',
    ]:
        brave.hits_by_query[q] = [hit]

    def router(request):
        if request.url.host == domain:
            return httpx.Response(403)  # Tier A: the target's own site is unreachable in this scenario
        return httpx.Response(
            200, headers={"content-type": "text/html"},
            text=(
                "<html><body><p>This page explains AWS's compliance posture under the California "
                "Consumer Privacy Act (CCPA) and your privacy choices as an AWS customer regarding "
                "the sale or sharing of information and targeted advertising opt-out requests.</p>"
                "</body></html>"
            ),
        )

    from app.research_fetch import PageFetcher
    client = httpx.Client(transport=httpx.MockTransport(router), base_url=f"https://{domain}")
    provider = WebPrivacyActionResearchProvider(fetcher=PageFetcher(client=client), search_backend=brave)

    with pytest.raises(PrivacyMechanismUnverified) as exc_info:
        provider.research(domain, PrivacyActionType.SALE_SHARING_OPT_OUT)
    # Kept only as a manual-review lead, never as a verified mechanism.
    assert exc_info.value.url == "https://aws.amazon-fixture.com/compliance/california-consumer-privacy-act/"


def test_mechanism_must_apply_to_target_service_not_merely_official_domain_family():
    """Even when the affiliate page IS reachable (no 403) and matches every
    textual signal, sharing the corporate family alone must never be
    enough - verify_mechanism must reject it regardless of reachability."""
    domain = "amazon-fixture.com"

    def router(request):
        if request.url.host == domain:
            return httpx.Response(404)  # nothing on the target's own site
        return httpx.Response(
            200, headers={"content-type": "text/html"},
            text="<html><body><p>Do not sell or share: your privacy choices as an AWS customer.</p></body></html>",
        )

    from app.research_fetch import PageFetcher
    brave = _FakeBrave()
    for q in [
        f'site:{domain} ("do not sell" OR "do not sell or share" OR "opt-out of sale")',
        f'site:{domain} ("your privacy choices" OR "california privacy rights" OR CCPA)',
        f'site:{domain} ("targeted advertising opt-out" OR "global privacy control")',
    ]:
        brave.hits_by_query[q] = [SearchHit(url="https://aws.amazon-fixture.com/privacy-choices", title="AWS", snippet="")]

    client = httpx.Client(transport=httpx.MockTransport(router), base_url=f"https://{domain}")
    provider = WebPrivacyActionResearchProvider(fetcher=PageFetcher(client=client), search_backend=brave)

    with pytest.raises(PrivacyMechanismUnverified):
        provider.research(domain, PrivacyActionType.SALE_SHARING_OPT_OUT)


def test_amazon_like_fixture_prefers_consumer_privacy_control_over_affiliate_compliance_explainer():
    """Regression for the exact live-test finding: NONESSENTIAL_TRACKING_CLEANUP
    correctly found the real consumer mechanism, but SALE_SHARING_OPT_OUT
    incorrectly resolved to an AWS compliance page. Here, the target's OWN
    homepage links directly to its real "Your Ads Privacy Choices"-shaped
    consumer control (covering both personalized advertising AND
    cross-context behavioral advertising / sale-sharing opt-out), while an
    unrelated AWS-shaped compliance explainer is ALSO discoverable via
    search under the same registrable root. The resolver must prefer the
    genuinely applicable consumer mechanism for BOTH action types and never
    the affiliate page."""
    domain = "amazon-fixture.com"
    home_html = '<html><body><a href="/adprefs">Your Ads Privacy Choices</a></body></html>'
    ads_privacy_html = (
        "<html><body><p>Your Ads Privacy Choices lets you manage personalized advertising and "
        "exercise your right to opt-out of the sale or sharing of your information for cross-context "
        "behavioral advertising.</p></body></html>"
    )
    aws_compliance_html = (
        "<html><body><p>AWS California Consumer Privacy Act compliance: your privacy choices, "
        "do not sell or share, targeted advertising opt-out for AWS services.</p></body></html>"
    )

    def router(request):
        if request.url.host == domain:
            if request.url.path == "/":
                return httpx.Response(200, headers={"content-type": "text/html"}, text=home_html)
            if request.url.path == "/adprefs":
                return httpx.Response(200, headers={"content-type": "text/html"}, text=ads_privacy_html)
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html"}, text=aws_compliance_html)

    brave = _FakeBrave()
    for q in [
        f'site:{domain} ("do not sell" OR "do not sell or share" OR "opt-out of sale")',
        f'site:{domain} ("your privacy choices" OR "california privacy rights" OR CCPA)',
        f'site:{domain} ("targeted advertising opt-out" OR "global privacy control")',
    ]:
        brave.hits_by_query[q] = [SearchHit(url="https://aws.amazon-fixture.com/compliance", title="AWS Compliance", snippet="")]

    from app.research_fetch import PageFetcher
    client = httpx.Client(transport=httpx.MockTransport(router), base_url=f"https://{domain}")
    fetcher = PageFetcher(client=client)
    provider = WebPrivacyActionResearchProvider(fetcher=fetcher, search_backend=brave)

    tracking_result = provider.research(domain, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    opt_out_result = provider.research(domain, PrivacyActionType.SALE_SHARING_OPT_OUT)

    for result, label in ((tracking_result, "tracking"), (opt_out_result, "opt_out")):
        assert result is not None, f"{label} should have found the real consumer mechanism"
        assert result.verified is True
        assert result.source_url == f"https://{domain}/adprefs", (
            f"{label} incorrectly resolved to {result.source_url!r} instead of the target's own page"
        )
        assert "aws" not in result.source_url.lower()

    # Tier B was never even needed - Tier A's own exact-host page satisfied
    # both action types on its own, so the affiliate page was never reached.
    assert brave.queries_made == []


def test_one_applicable_mechanism_supports_multiple_action_types_independently():
    """A single, genuinely applicable exact-host page whose official
    evidence actually covers both concerns (personalized advertising AND
    cross-context behavioral advertising) may legitimately verify for BOTH
    PrivacyActionTypes - each resolved independently, each with its OWN
    correct, distinct scope note."""
    domain = "onepage-fixture.com"
    home_html = '<html><body><a href="/privacy/ad-preferences">Privacy Choices</a></body></html>'
    page_html = (
        "<html><body><p>Manage your personalized advertising preferences and exercise your "
        "right to opt-out of the sale or sharing of your information for cross-context "
        "behavioral advertising, all from this one page.</p></body></html>"
    )

    def handler(request):
        if request.url.path == "/":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=home_html)
        if request.url.path == "/privacy/ad-preferences":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=page_html)
        return httpx.Response(404)

    provider = _provider(domain, handler)
    tracking_result = provider.research(domain, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    opt_out_result = provider.research(domain, PrivacyActionType.SALE_SHARING_OPT_OUT)

    assert tracking_result is not None and tracking_result.verified is True
    assert opt_out_result is not None and opt_out_result.verified is True
    assert tracking_result.source_url == opt_out_result.source_url == f"https://{domain}/privacy/ad-preferences"
    # Each keeps its OWN truthful, distinct scope note - never conflated.
    assert tracking_result.scope_note == TRACKING_CLEANUP_SCOPE_NOTE
    assert opt_out_result.scope_note == OPT_OUT_SCOPE_NOTE
    assert tracking_result.scope_note != opt_out_result.scope_note


def test_existing_same_domain_mechanism_still_verifies_after_the_fix():
    """Regression guard: the straightforward, common case (a mechanism on
    the target's own exact domain) must still work exactly as before."""
    result = PrivacyActionResult(
        domain="shopexample.com", action_type=PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP,
        method=DeletionMethod.WEB_FORM, source_url="https://shopexample.com/cookie-preferences",
    )
    assert verify_mechanism("shopexample.com", result) is True


def test_existing_officially_linked_third_party_portal_still_verifies_after_the_fix():
    """Regression guard: a genuinely linked third-party CMP portal, reached
    via the target's own exact-host page, must still verify - the fix only
    tightens what counts as "the target's own page," not the separate,
    already-strict third-party-referral rule."""
    result = PrivacyActionResult(
        domain="shopexample.com", action_type=PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP,
        method=DeletionMethod.PRIVACY_PORTAL,
        source_url="https://privacyportal.onetrust.com/webform/shopexample-cookies",
        referring_official_url="https://shopexample.com/cookies",
    )
    assert verify_mechanism("shopexample.com", result) is True
