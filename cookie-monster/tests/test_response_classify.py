import json
from unittest.mock import MagicMock

from app.deletion_constants import DeletionStatus
from app.response_classify import ResponseClassifier

# --- Pass 1: regex heuristics, one per required state ---

def test_completed_requires_explicit_confirmation():
    result = ResponseClassifier().classify(
        "Your personal information has been deleted from our systems as requested."
    )
    assert result.status == DeletionStatus.COMPLETED
    assert result.confidence == "high"


def test_generic_acknowledgement_is_in_progress_never_completed():
    """The single most important guarantee: a company just saying 'we got
    your request' must never be read as 'we finished it'."""
    result = ResponseClassifier().classify(
        "We have received your request and opened case #48213. We are currently reviewing it."
    )
    assert result.status == DeletionStatus.IN_PROGRESS
    assert result.status != DeletionStatus.COMPLETED


def test_ticket_created_is_in_progress_not_completed():
    result = ResponseClassifier().classify("Thank you for contacting us. Your support request has been created.")
    assert result.status == DeletionStatus.IN_PROGRESS


def test_verification_needed_detected():
    result = ResponseClassifier().classify(
        "Please verify your identity by clicking the link below before we can proceed."
    )
    assert result.status == DeletionStatus.VERIFICATION_NEEDED


def test_more_info_required_detected():
    result = ResponseClassifier().classify(
        "In order to process your request, we need you to provide your account email."
    )
    assert result.status == DeletionStatus.MORE_INFO_REQUIRED


def test_rejected_detected():
    result = ResponseClassifier().classify(
        "We are unable to process your deletion request because you are not eligible under this policy."
    )
    assert result.status == DeletionStatus.REJECTED


def test_ambiguous_reply_is_unknown_response_not_guessed():
    result = ResponseClassifier().classify("Thanks for reaching out! We love hearing from our customers.")
    assert result.status == DeletionStatus.UNKNOWN_RESPONSE
    assert result.confidence == "low"


def test_quote_is_capped_and_never_the_full_message():
    long_text = "We have received your request. " + ("padding " * 100)
    result = ResponseClassifier().classify(long_text)
    assert len(result.quote) <= 200


# --- Account closure/deactivation vs. actual data deletion (real Goop
# Kitchen case) - a company confirming it closed/deactivated the ACCOUNT
# must never be conflated with confirming the underlying personal DATA
# was deleted. See ACCOUNT_CLOSED_DATA_UNVERIFIED_PATTERNS's docstring. ---

GOOP_REAL_TEXT = (
    "Thank you for reaching out. We've already deactivated the gK Insider account "
    "associated with your email as requested.\n\n"
    "Please rest assured that we take data privacy very seriously. Your information "
    "is protected, handled securely, and never shared publicly.\n\n"
    "We hope this information helps! Please let us know if you have any questions "
    "or concerns. We're always happy to help!"
)


def test_real_goop_kitchen_reply_is_account_closed_not_completed():
    result = ResponseClassifier().classify(GOOP_REAL_TEXT)
    assert result.status == DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED
    assert result.status != DeletionStatus.COMPLETED


def test_account_deactivated_as_requested_alone_is_not_completed():
    result = ResponseClassifier().classify("We have deactivated your account as requested.")
    assert result.status == DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED
    assert result.status != DeletionStatus.COMPLETED


def test_account_closed_alone_is_not_completed():
    """Distinct wording ('closed' vs 'deactivated') must be caught the
    same way - this is about the semantic category, not one exact verb."""
    result = ResponseClassifier().classify("We have closed your account as requested.")
    assert result.status == DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED
    assert result.status != DeletionStatus.COMPLETED


def test_generic_reassurance_phrases_never_independently_establish_deletion():
    """'as requested' / 'request processed' / 'privacy is important to
    us' / 'information is protected' / 'handled securely' / 'not shared
    publicly' must never, on their own, produce COMPLETED (or any
    confident status at all) - they carry no deletion evidence."""
    phrases = [
        "We processed your request as requested. Thank you.",
        "Your request has been processed.",
        "Your privacy is important to us.",
        "Your information is protected and handled securely.",
        "Your data is never shared publicly.",
    ]
    for text in phrases:
        result = ResponseClassifier().classify(text)
        assert result.status != DeletionStatus.COMPLETED, f"{text!r} must never resolve to COMPLETED"
        assert result.status != DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED, f"{text!r} mentions no account action"


def test_explicit_personal_data_deletion_still_classifies_completed():
    """The new account-closure category must never crowd out a genuine,
    explicit deletion confirmation - COMPLETED_PATTERNS is checked FIRST
    in _PATTERN_ORDER and is completely untouched by this change."""
    result = ResponseClassifier().classify("Your personal information has been deleted from our systems.")
    assert result.status == DeletionStatus.COMPLETED


def test_account_closure_plus_explicit_deletion_resolves_to_completed():
    """A message that BOTH confirms account closure AND explicitly
    confirms data deletion (even alongside generic reassurance language)
    must resolve to the stronger, more specific claim - COMPLETED - never
    the weaker ACCOUNT_CLOSED_DATA_UNVERIFIED."""
    text = (
        "We have closed your account as requested. Your personal information has been "
        "deleted from our systems. Your privacy is important to us and your data is "
        "handled securely."
    )
    result = ResponseClassifier().classify(text)
    assert result.status == DeletionStatus.COMPLETED


def test_forward_order_closure_plus_explicit_deletion_resolves_to_completed():
    """The forward-order counterpart to the test above ('we closed your
    account AND deleted your information', not 'account has been closed
    and ... deleted') must also resolve to COMPLETED, not the weaker
    ACCOUNT_CLOSED_DATA_UNVERIFIED."""
    result = ResponseClassifier().classify(
        "We closed your account and deleted your personal information."
    )
    assert result.status == DeletionStatus.COMPLETED


def test_negated_deletion_language_does_not_false_positive_into_completed():
    """The new forward-order COMPLETED pattern is anchored to an actual
    closure verb immediately before 'account' specifically so it can't
    fire on a bare 'deleted your information' with no closure context -
    which would otherwise catch obviously negated claims like this."""
    result = ResponseClassifier().classify(
        "We have not deleted your personal information because your account is still active."
    )
    assert result.status != DeletionStatus.COMPLETED


# =========================================================================
# Company-agnosticism: every rule above must generalize to ANY company's
# wording, not just the two real regressions (MALK Organics, Goop Kitchen)
# that originally exposed these gaps. None of response_classify.py's logic
# ever references a company name, domain, or message id - these fabricated
# companies/domains prove that empirically, independent of the real-case
# regression tests further down this file.
# =========================================================================

def test_arbitrary_company_account_closure_wording_is_account_closed_not_completed():
    result = ResponseClassifier().classify(
        "Hi there, this is Acme Rewards support. We have closed your rewards account per your request."
    )
    assert result.status == DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED
    assert result.status != DeletionStatus.COMPLETED


def test_arbitrary_company_reverse_order_closure_with_email_is_account_closed():
    result = ResponseClassifier().classify(
        "This confirms that the account associated with user@example.com has been deactivated."
    )
    assert result.status == DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED
    assert result.status != DeletionStatus.COMPLETED


def test_arbitrary_company_explicit_account_and_data_deletion_is_completed():
    result = ResponseClassifier().classify(
        "Team at Widgetify here - we closed your account and deleted your personal information."
    )
    assert result.status == DeletionStatus.COMPLETED


def test_arbitrary_company_ambiguous_reply_is_unknown_response():
    result = ResponseClassifier().classify(
        "Thanks so much for being a valued customer of Widgetify! We love hearing from you."
    )
    assert result.status == DeletionStatus.UNKNOWN_RESPONSE


def test_arbitrary_company_unrelated_account_and_closed_mentions_do_not_false_positive():
    result = ResponseClassifier().classify(
        "Your Widgetify account is in good standing. Note that our office was closed "
        "yesterday for a company holiday."
    )
    assert result.status != DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED
    assert result.status != DeletionStatus.COMPLETED


# --- Regression: the EXACT real Goop Kitchen reply that was live-tested
# against commit 00553b7. The classifier missed this because Python's `.`
# never matches `\n`, and this reply happens to hard-wrap the line right
# in between "...Insider" and "account associated with..." - a plain-text
# formatting artifact, not a wording gap. See
# _normalize_whitespace's docstring in response_classify.py. ---

GOOP_LIVE_REPLY_EXACT = (
    "Hi Lily,\n\n"
    "Thank you for reaching out.  We’ve already deactivated the gK Insider\n"
    "account associated with lilyroseshaw@gmail.com as requested.\n\n"
    " Please rest assured that we take data privacy very seriously. Your\n"
    "information is protected, handled securely, and never shared publicly.\n\n"
    "We hope this information helps! Please let us know if you have any\n"
    "questions or concerns. We're always happy to help!\n\n"
    "Best,\n\n"
    "In Your Service | Guest Experience Team\n\n"
    "p: 310.954.1286\n\n"
    "goopkitchen.com @goopkitchen <https://instagram.com/goopkitchen>"
)


def test_real_goop_live_reply_with_line_wrap_and_curly_apostrophe_is_account_closed():
    """The exact body captured from the live Goop Kitchen thread (company
    38) - curly apostrophe, mid-sentence hard line wrap, and all. Must
    classify as ACCOUNT_CLOSED_DATA_UNVERIFIED, not UNKNOWN_RESPONSE (the
    live miss) and not COMPLETED."""
    result = ResponseClassifier().classify(GOOP_LIVE_REPLY_EXACT)
    assert result.status == DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED
    assert result.status != DeletionStatus.UNKNOWN_RESPONSE
    assert result.status != DeletionStatus.COMPLETED


def test_account_closure_detected_regardless_of_apostrophe_style_and_line_wrap():
    """Straight vs curly apostrophe, and whether the sentence happens to
    wrap onto a second line, must never change the outcome - only the
    semantic content matters."""
    variants = [
        "We've already deactivated the gK Insider account associated with your email as requested.",
        "We’ve already deactivated the gK Insider account associated with your email as requested.",
        "We've already deactivated the gK Insider\naccount associated with your email as requested.",
        "We’ve already deactivated the gK Insider\naccount associated with your email as requested.",
    ]
    for text in variants:
        result = ResponseClassifier().classify(text)
        assert result.status == DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED, f"failed for {text!r}"


def test_account_closure_detected_with_intervening_brand_or_program_name():
    """A closure verb and 'account' separated by a brand/program name (not
    just 'your'/'the') must still be caught - the gap is about intervening
    descriptive words, not the exact pronoun used."""
    variants = [
        "we deactivated your account",
        "we've deactivated your account",
        "we have deactivated the account",
        "we've already deactivated the gK Insider account",
        "we've already deactivated the Rewards Program membership account",
        "your account has been closed",
        "we closed your account",
    ]
    for text in variants:
        result = ResponseClassifier().classify(text)
        assert result.status == DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED, f"failed for {text!r}"
        assert result.status != DeletionStatus.COMPLETED


def test_account_associated_with_email_has_been_deactivated_reverse_order():
    """The reversed shape - 'account' named first, closure verb later,
    with a long token (an email address) in between - must also match.
    This is the one shape that genuinely needs a wider gap than a small
    fixed character budget (see the pattern's own comment for why that
    gap is a named, narrow clause rather than an arbitrary wildcard)."""
    result = ResponseClassifier().classify(
        "The account associated with lilyroseshaw@gmail.com has been deactivated."
    )
    assert result.status == DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED
    assert result.status != DeletionStatus.COMPLETED


def test_unrelated_account_and_closed_mentions_do_not_false_positive():
    """The widened reverse-direction pattern must NOT turn into a generic
    'account' ... 'closed' fuzzy match - unrelated sentences that merely
    contain both words must still fail closed to UNKNOWN_RESPONSE."""
    texts = [
        "We understand your concerns about your account. Separately, our "
        "office will be closed for the holidays.",
        "Your account is in good standing. Note that support tickets are "
        "typically closed within 48 hours.",
        "Thanks for your account inquiry - by the way, applications for "
        "this program closed last week.",
    ]
    for text in texts:
        result = ResponseClassifier().classify(text)
        assert result.status != DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED, f"false positive for {text!r}"
        assert result.status != DeletionStatus.COMPLETED


# --- Pass 2: LLM-assisted, with guardrails ---

def _mock_llm(payload: dict):
    client = MagicMock()
    client.messages.create.return_value = MagicMock(content=[MagicMock(text=json.dumps(payload))])
    return client


def test_llm_only_invoked_when_heuristic_is_inconclusive():
    client = _mock_llm({"status": "UNKNOWN_RESPONSE", "confidence": "low", "quote": "hi"})
    ResponseClassifier(llm_client=client, llm_model="fake").classify("Some ambiguous filler text with no signal.")
    client.messages.create.assert_called_once()


def test_llm_not_invoked_when_heuristic_is_confident():
    client = _mock_llm({"status": "COMPLETED", "confidence": "high", "quote": "irrelevant"})
    ResponseClassifier(llm_client=client, llm_model="fake").classify(
        "Your data has been deleted from our systems."
    )
    client.messages.create.assert_not_called()


def test_llm_completed_verdict_rejected_without_corroborating_keyword():
    """The LLM saying COMPLETED isn't enough on its own - a Pass-1 keyword
    must also match, or the verdict is discarded entirely."""
    text = "We appreciate your patience while our team looks into this further."
    client = _mock_llm({"status": "COMPLETED", "confidence": "high", "quote": text})
    result = ResponseClassifier(llm_client=client, llm_model="fake").classify(text)
    assert result.status != DeletionStatus.COMPLETED


def test_llm_quote_must_be_verbatim_in_message():
    client = _mock_llm({"status": "REJECTED", "confidence": "high", "quote": "this sentence is not in the message"})
    result = ResponseClassifier(llm_client=client, llm_model="fake").classify("Some other unrelated ambiguous text.")
    assert result.status == DeletionStatus.UNKNOWN_RESPONSE  # LLM verdict discarded, falls back to heuristic


def test_llm_invalid_status_label_rejected():
    client = _mock_llm({"status": "TOTALLY_MADE_UP", "confidence": "high", "quote": "text"})
    result = ResponseClassifier(llm_client=client, llm_model="fake").classify("Ambiguous message text here.")
    assert result.status == DeletionStatus.UNKNOWN_RESPONSE


def test_llm_failure_falls_back_to_heuristic():
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("API is down")
    result = ResponseClassifier(llm_client=client, llm_model="fake").classify(
        "We are unable to fulfill your request at this time."
    )
    assert result.status == DeletionStatus.REJECTED


def test_llm_malformed_json_falls_back_to_heuristic():
    client = MagicMock()
    client.messages.create.return_value = MagicMock(content=[MagicMock(text="not valid json at all")])
    result = ResponseClassifier(llm_client=client, llm_model="fake").classify(
        "Please verify your identity to continue."
    )
    assert result.status == DeletionStatus.VERIFICATION_NEEDED


def test_valid_llm_verdict_with_verbatim_quote_and_corroboration_is_accepted():
    text = "We confirm that your data has been deleted from our records as of today."
    client = _mock_llm({"status": "COMPLETED", "confidence": "high", "quote": text})
    result = ResponseClassifier(llm_client=client, llm_model="fake").classify(text)
    assert result.status == DeletionStatus.COMPLETED
