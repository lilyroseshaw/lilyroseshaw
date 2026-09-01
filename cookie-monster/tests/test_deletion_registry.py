from app.deletion_constants import ActionCapability, DeletionMethod
from app.deletion_registry import get_provider


def test_lyft_is_account_setting():
    provider = get_provider("lyft.com")
    assert provider is not None
    assert provider.method == DeletionMethod.ACCOUNT_SETTING
    assert provider.automation == ActionCapability.USER_ACTION_REQUIRED
    assert provider.url == "https://account.lyft.com/privacy/data/delete"
    assert "account" in provider.consequences.lower()


def test_edikted_is_privacy_portal():
    provider = get_provider("edikted.com")
    assert provider is not None
    assert provider.method == DeletionMethod.PRIVACY_PORTAL
    assert "ccpa-compliance" in provider.url


def test_domain_normalization_matches_registry():
    # A subdomain should resolve to the same registry entry as the root domain.
    assert get_provider("mail.notifications.lyft.com") == get_provider("lyft.com")


def test_unknown_domain_returns_none():
    assert get_provider("some-company-not-in-the-registry.com") is None
