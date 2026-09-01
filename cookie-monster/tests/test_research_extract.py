import json
from unittest.mock import MagicMock

from app.deletion_constants import DeletionMethod
from app.research_extract import RecipeExtractor
from app.research_fetch import PageContent


def _page(text, mailto_links=None, external_links=None, url="https://shopexample.com/privacy"):
    return PageContent(
        url=url, domain="shopexample.com", status_code=200, text=text,
        mailto_links=mailto_links or [], external_links=external_links or [],
    )


def test_no_signal_returns_none():
    extractor = RecipeExtractor()
    result = extractor.extract("Shop", "shopexample.com", [_page("We make great widgets since 1990.")])
    assert result is None


def test_email_request_detected():
    page = _page(
        "California residents have the right to deletion. Submit a data rights request by email.",
        mailto_links=["privacy@shopexample.com"],
    )
    result = RecipeExtractor().extract("Shop", "shopexample.com", [page])
    assert result.method == DeletionMethod.EMAIL_REQUEST
    assert result.email == "privacy@shopexample.com"
    assert result.confidence == "high"
    assert result.reasons


def test_third_party_portal_detected_with_referring_page():
    page = _page(
        "Submit your CCPA deletion request through our privacy portal.",
        external_links=[("https://privacyportal.onetrust.com/webform/x", "Submit")],
    )
    result = RecipeExtractor().extract("Shop", "shopexample.com", [page])
    assert result.method == DeletionMethod.PRIVACY_PORTAL
    assert result.url == "https://privacyportal.onetrust.com/webform/x"
    assert result.referring_official_url == page.url


def test_login_required_detected():
    page = _page("To delete your account, sign in to your account and go to settings.")
    result = RecipeExtractor().extract("Shop", "shopexample.com", [page])
    assert result.method == DeletionMethod.ACCOUNT_SETTING
    assert result.login_required is True


def test_llm_pass_only_runs_when_heuristic_is_inconclusive():
    weak_page = _page("Please contact us for questions about your data.", mailto_links=["hello@shopexample.com"])
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(text=json.dumps({"method": "UNKNOWN", "confidence": "low"}))]
    )
    RecipeExtractor(llm_client=mock_client, llm_model="fake-model").extract("Shop", "shopexample.com", [weak_page])
    mock_client.messages.create.assert_called_once()


def test_llm_pass_never_invents_an_email_not_on_the_page():
    weak_page = _page("Contact our support team for help with your account.")
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(text=json.dumps({
            "method": "EMAIL_REQUEST",
            "email": "totally-made-up@nowhere.com",  # NOT present in weak_page.text
            "confidence": "high",
        }))]
    )
    result = RecipeExtractor(llm_client=mock_client, llm_model="fake-model").extract(
        "Shop", "shopexample.com", [weak_page]
    )
    # The claimed email isn't verbatim in the page - must be rejected entirely.
    assert result is None


def test_llm_pass_accepts_email_that_is_verbatim_on_the_page():
    page = _page("For privacy inquiries, contact dataprotection@shopexample.com and we will assist you.")
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(
        content=[MagicMock(text=json.dumps({
            "method": "EMAIL_REQUEST",
            "email": "dataprotection@shopexample.com",
            "confidence": "high",
        }))]
    )
    result = RecipeExtractor(llm_client=mock_client, llm_model="fake-model").extract(
        "Shop", "shopexample.com", [page]
    )
    assert result is not None
    assert result.email == "dataprotection@shopexample.com"


def test_llm_failure_falls_back_to_heuristic_result():
    page = _page(
        "You have the right to deletion. Email us to submit a data rights request.",
        mailto_links=["privacy@shopexample.com"],
    )
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = RuntimeError("API down")
    result = RecipeExtractor(llm_client=mock_client, llm_model="fake-model").extract(
        "Shop", "shopexample.com", [page]
    )
    assert result is not None
    assert result.method == DeletionMethod.EMAIL_REQUEST
