from app.classifier import classify_message


def test_order_confirmation_is_transactional():
    result = classify_message(
        "Your Amazon.com order has been confirmed",
        "Amazon.com <auto-confirm@amazon.com>",
    )
    assert result is not None
    assert result.domain == "amazon.com"
    assert result.evidence_type == "order_confirmation"
    assert result.relationship_type == "transactional"
    assert result.reasons  # explanations retained


def test_welcome_email_is_account_creation():
    result = classify_message(
        "Welcome to Notion!",
        "Notion <team@mail.notion.so>",
    )
    assert result is not None
    assert result.evidence_type == "account_creation"
    assert result.relationship_type == "account"


def test_password_reset_detected():
    result = classify_message(
        "Reset your password",
        "Spotify <no-reply@spotify.com>",
    )
    assert result.evidence_type == "password_reset"


def test_personal_gmail_sender_is_ignored():
    result = classify_message(
        "Your order has shipped",  # even matching subject text
        "A Friend <friend@gmail.com>",
    )
    assert result is None


def test_unmatched_subject_is_ignored():
    result = classify_message(
        "Let's catch up sometime",
        "Colleague <colleague@somecompany.com>",
    )
    assert result is None


def test_company_name_falls_back_to_domain_when_display_name_is_email():
    result = classify_message(
        "Your receipt from Uber",
        "receipts@uber.com",
    )
    assert result.company_name == "Uber"


def test_company_name_strips_noreply_suffix():
    result = classify_message(
        "Your order confirmation",
        "Target Team <orders@target.com>",
    )
    assert result.company_name == "Target"
