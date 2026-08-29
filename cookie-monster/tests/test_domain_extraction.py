from app.classifier import PERSONAL_EMAIL_DOMAINS, extract_domain


def test_extract_plain_domain():
    assert extract_domain("Amazon.com <auto-confirm@amazon.com>") == "amazon.com"


def test_extract_domain_collapses_subdomain():
    assert extract_domain("Netflix <info@mailer.netflix.com>") == "netflix.com"


def test_extract_domain_handles_multi_part_suffix():
    assert extract_domain("Tesco <no-reply@notifications.tesco.co.uk>") == "tesco.co.uk"


def test_extract_domain_no_at_sign_returns_none():
    assert extract_domain("not-an-email") is None


def test_extract_domain_empty_header():
    assert extract_domain("") is None


def test_personal_domains_flagged():
    assert extract_domain("A Friend <someone@gmail.com>") in PERSONAL_EMAIL_DOMAINS
