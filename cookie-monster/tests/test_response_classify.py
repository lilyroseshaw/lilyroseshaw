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
