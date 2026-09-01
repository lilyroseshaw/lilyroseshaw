"""Classifies a company's reply to a sent deletion request. Two passes,
same philosophy as research_extract.py:

Pass 1 (always on): regex/keyword heuristics. Deterministic, auditable.
Pass 2 (optional, needs ANTHROPIC_API_KEY): LLM-assisted, only for replies
Pass 1 can't confidently classify. The model must justify its verdict with
a quote that's verified verbatim-present in the actual message text - same
guardrail as recipe extraction (research_extract.py). A COMPLETED verdict
from the LLM is only accepted if a Pass-1-style strong keyword also
matches - the one label that must never be wrong gets a second,
independent check.

Conservative by design: a generic acknowledgement, a ticket/case creation,
or a "we're reviewing your request" message is IN_PROGRESS, never
COMPLETED. Nothing here is trusted enough to mark COMPLETED off one weak
signal.
"""
import json
import re
from dataclasses import dataclass, field

from app.deletion_constants import DeletionStatus

MAX_QUOTE_LEN = 200

COMPLETED_PATTERNS = [
    r"(has been|have been) (permanently )?deleted",
    r"(has been|have been) (permanently )?removed",
    r"we (have )?(successfully )?deleted your",
    r"we (have )?(successfully )?removed your",
    r"your (personal )?(information|data) (has been|have been) (deleted|removed|erased)",
    r"deletion (is|has been) complete",
    r"account (has been|was) (permanently )?closed and (your )?(data|information) (deleted|removed)",
    r"we confirm that your data has been deleted",
]

REJECTED_PATTERNS = [
    r"(unable|cannot|can'?t|not able) to (process|complete|fulfill) (your|this) (deletion )?request",
    r"we (do not|don'?t) sell",
    r"(request|deletion) (has been )?(denied|declined|rejected)",
    r"exempt(ion)? (from|under)",
    r"we are unable to delete",
    r"cannot honor (this|your) request",
]

VERIFICATION_NEEDED_PATTERNS = [
    r"verify your identity",
    r"confirm your (email|identity|request)",
    r"click (the|this) link to (confirm|verify)",
    r"please verify",
    r"identity verification",
]

MORE_INFO_REQUIRED_PATTERNS = [
    r"(need|require) (additional|more) information",
    r"please provide (your|the)",
    r"in order to process your request,? we (need|require)",
    r"could you (please )?(provide|confirm|clarify)",
]

IN_PROGRESS_PATTERNS = [
    r"(we have |we'?ve )?received your request",
    r"(is|are) being processed",
    r"we are (currently )?(reviewing|processing|working on)",
    r"case (number|#|id) ?[:#]?\s*\w+",
    r"ticket (number|#|id) ?[:#]?\s*\w+",
    r"support request .{0,20}(created|opened|received)",
    r"we will (get back to you|respond|follow up)",
]

# Checked in order: more specific/serious signals before generic ones, so a
# reply mentioning both "we received your request" AND "has been deleted"
# (e.g. a follow-up after an earlier ack) resolves to the stronger claim.
_PATTERN_ORDER = [
    (DeletionStatus.COMPLETED, COMPLETED_PATTERNS),
    (DeletionStatus.REJECTED, REJECTED_PATTERNS),
    (DeletionStatus.VERIFICATION_NEEDED, VERIFICATION_NEEDED_PATTERNS),
    (DeletionStatus.MORE_INFO_REQUIRED, MORE_INFO_REQUIRED_PATTERNS),
    (DeletionStatus.IN_PROGRESS, IN_PROGRESS_PATTERNS),
]

LLM_CLASSIFY_PROMPT = """You are reading ONE email reply from a company, sent in response to a data-deletion request that was already sent to them. Classify it into exactly one of these labels:

SUBMITTED, IN_PROGRESS, VERIFICATION_NEEDED, MORE_INFO_REQUIRED, COMPLETED, REJECTED, UNKNOWN_RESPONSE

Rules:
- COMPLETED means the company explicitly states the deletion has ALREADY happened - not that they received the request, opened a ticket, or are reviewing it. A generic acknowledgement is IN_PROGRESS, never COMPLETED.
- REJECTED means the company explicitly declines or says it cannot fulfill the request.
- VERIFICATION_NEEDED means they're asking the sender to verify identity/email before proceeding.
- MORE_INFO_REQUIRED means they're asking for additional information (not identity verification).
- If the message doesn't clearly fit any of these, use UNKNOWN_RESPONSE - do not guess.

Reply text:
---
{message_text}
---

Respond with ONLY strict JSON: {{"status": "...", "confidence": "high"|"medium"|"low", "quote": "the exact sentence from the text above that justifies your answer"}}. The quote MUST be copied verbatim from the text above - do not paraphrase or summarize it."""


@dataclass
class ResponseClassification:
    status: str  # DeletionStatus.*
    confidence: str  # high | medium | low
    quote: str  # short, capped evidence excerpt - never the full message
    reasons: list[str] = field(default_factory=list)


def _find_match(text_lower: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        if re.search(pattern, text_lower):
            return pattern
    return None


def _quote_around(text: str, pattern: str) -> str:
    match = re.search(pattern, text.lower())
    if not match:
        return text[:MAX_QUOTE_LEN]
    start = max(0, match.start() - 40)
    end = min(len(text), match.end() + 80)
    return text[start:end].strip()[:MAX_QUOTE_LEN]


class ResponseClassifier:
    def __init__(self, llm_client=None, llm_model: str | None = None):
        self._llm_client = llm_client
        self._llm_model = llm_model

    def classify(self, message_text: str) -> ResponseClassification:
        heuristic = self._classify_heuristic(message_text)
        if heuristic.status != DeletionStatus.UNKNOWN_RESPONSE and heuristic.confidence == "high":
            return heuristic

        if self._llm_client is not None:
            llm_result = self._classify_with_llm(message_text)
            if llm_result is not None:
                return llm_result

        return heuristic

    def _classify_heuristic(self, message_text: str) -> ResponseClassification:
        text_lower = message_text.lower()
        for status, patterns in _PATTERN_ORDER:
            match = _find_match(text_lower, patterns)
            if match:
                return ResponseClassification(
                    status=status, confidence="high",
                    quote=_quote_around(message_text, match),
                    reasons=[f"matched /{match}/"],
                )
        return ResponseClassification(
            status=DeletionStatus.UNKNOWN_RESPONSE, confidence="low",
            quote=message_text.strip()[:MAX_QUOTE_LEN], reasons=["no known pattern matched"],
        )

    def _classify_with_llm(self, message_text: str) -> ResponseClassification | None:
        prompt = LLM_CLASSIFY_PROMPT.format(message_text=message_text[:6000])
        try:
            response = self._llm_client.messages.create(
                model=self._llm_model, max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            parsed = json.loads(response.content[0].text)
        except Exception:  # noqa: BLE001 - any LLM/parsing failure falls back to the heuristic result
            return None

        status = parsed.get("status")
        if status not in DeletionStatus.ALL:
            return None

        quote = (parsed.get("quote") or "").strip()
        # Hard guardrail: the model's justification must be verbatim in the
        # actual message - if it isn't, the verdict can't be trusted at all.
        if not quote or quote not in message_text:
            return None

        if status == DeletionStatus.COMPLETED:
            # Extra guardrail specific to the one label that must never be
            # wrong: an LLM COMPLETED verdict also needs a Pass-1-style
            # strong keyword match, independent of the LLM's own reasoning.
            if _find_match(message_text.lower(), COMPLETED_PATTERNS) is None:
                return None

        confidence = parsed.get("confidence", "low")
        if confidence not in ("high", "medium", "low"):
            confidence = "low"

        return ResponseClassification(
            status=status, confidence=confidence, quote=quote[:MAX_QUOTE_LEN],
            reasons=["LLM-classified; quote verified verbatim in message"],
        )


def build_default_classifier() -> ResponseClassifier:
    """Reuses the same optional Anthropic client/model as recipe extraction
    (app.deletion_research) - no separate key needed. Zero keys configured
    -> regex-only classification, still fully functional, just conservative
    about anything Pass 1's patterns don't cover (falls to UNKNOWN_RESPONSE)."""
    from app import config

    llm_client = None
    if config.ANTHROPIC_API_KEY:
        import anthropic

        llm_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return ResponseClassifier(llm_client=llm_client, llm_model=config.DELETION_RESEARCH_LLM_MODEL)
