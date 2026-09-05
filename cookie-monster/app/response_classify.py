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

# A gap-matching building block used by several patterns below to span a
# variable amount of intervening text (an "associated with <email>"
# clause, a compound "X and Y" object list, ...) WITHOUT ever drifting
# into a separate, unrelated sentence. A literal `.` in a character class
# would also block the dot inside an email domain like "gmail.com",
# which is exactly the kind of token these gaps need to span - so this
# instead excludes only a period immediately followed by whitespace or
# end-of-string (a real sentence break), via a negative lookahead.
_NOT_SENTENCE_BREAK = r"(?:(?!\.(?:\s|$))[^\n])"

# A completion verdict is the one label that must never be wrong, so every
# pattern below REQUIRES the deleted/removed OBJECT to be an explicit
# personal-data phrase ("(personal) information/data", or a clear
# synonym like "all data we hold about you"). This was NOT always true:
# an audit prompted by a real company reply ("We can confirm that the
# account ... and its details have already been deleted as requested")
# found that two earlier patterns here - a bare "(has been|have been)
# deleted/removed" and "we deleted your" with nothing required after
# "your" - would happily match "Your account has been deleted.", "Your
# profile has been deleted.", "The ticket has been deleted.", "We have
# deleted your account.", or any other object entirely unrelated to the
# user's personal data. Those two patterns are gone; every replacement
# below names its object. See ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED_PATTERNS
# below for the (deliberately separate, deliberately NOT completion)
# status that now catches "account/profile/membership record deleted"
# instead.
COMPLETED_PATTERNS = [
    # Passive voice, object required: "your personal information/data has
    # been deleted", "all personal information associated with you has
    # been deleted", "personal data has been permanently deleted", "the
    # information we hold about you has been erased". Optional "all"/
    # "your" prefix and an optional "associated with you"/"we hold/held/
    # have about you" qualifier both still require the core object to be
    # (personal) information/data - never a bare "has been deleted".
    r"(all |your )?(personal )?(information|data)( associated with you| we (hold|held|have) (about|on) you)? (has been|have been) (permanently )?(deleted|removed|erased)",
    # Active voice, object required after "your" (previously missing -
    # this is what let "we deleted your account" through as COMPLETED).
    r"we (have )?(successfully )?deleted (all )?your (personal )?(information|data)\b",
    r"we (have )?(successfully )?removed (all )?your (personal )?(information|data)\b",
    # Active voice, relative-clause object: "we deleted all data we held
    # about you" - the object is described rather than named "your X".
    r"we (have )?(successfully )?deleted all (data|information) we (hold|held|have) (about|on) you",
    # A closure/deletion verb governing BOTH "account" (or equivalent) AND
    # a personal-data object via "and" - e.g. "we deleted your account and
    # all personal information associated with you". The second conjunct
    # must still explicitly name "your"/"all (personal) information/data"
    # - a bare "and some other information" (e.g. "we deleted an old test
    # account and some unrelated cache information") does NOT qualify.
    rf"delet(ed|ing){_NOT_SENTENCE_BREAK}{{0,15}}\b(account|profile|membership)\b{_NOT_SENTENCE_BREAK}{{0,10}}\band\b{_NOT_SENTENCE_BREAK}{{0,10}}(your (personal )?(information|data)|all (personal )?(information|data)( associated with you)?)\b",
    # "Deletion is/has been complete" - safe as an objectless phrase ONLY
    # because, unlike "X has been deleted", nothing else is named as the
    # deleted object; still excluded when "account" is the stated subject
    # of that deletion ("your account deletion is complete") since that
    # narrows it back down to the same account-only claim as everything
    # else in this file.
    r"(?<!account )deletion (is|has been) complete",
    r"account (has been|was) (permanently )?closed and (your )?(data|information) (deleted|removed)",
    r"we confirm that your data has been deleted",
    # Forward-order: a closure verb, then "account", then an explicit
    # "deleted your ... data" claim later in the same sentence/clause
    # (e.g. "we closed your account and deleted your personal
    # information"). Anchored to an actual closure verb immediately
    # before "account" specifically so this can't fire on a bare "deleted
    # your information" appearing with no closure context at all (which
    # would also match awkward negations like "we have not deleted your
    # information") - this only ever catches the same explicit compound
    # claim COMPLETED already recognizes in passive voice above.
    r"(deactivat(ed|ing)|clos(ed|ing)|terminat(ed|ing)|cancell?(ed|ing))\b.{0,60}\baccount\b.{0,40}\bdeleted (your )?(personal )?(information|data)",
]

REJECTED_PATTERNS = [
    r"(unable|cannot|can'?t|not able) to (process|complete|fulfill) (your|this) (deletion )?request",
    r"we (do not|don'?t) sell",
    r"(request|deletion) (has been )?(denied|declined|rejected)",
    r"exempt(ion)? (from|under)",
    r"we are unable to delete",
    r"cannot honor (this|your) request",
]

# Account closure/deactivation is NOT deletion confirmation - a company
# saying it closed/deactivated the account "as requested" only confirms
# the ACCOUNT action, never that the underlying PERSONAL DATA was
# deleted (some companies retain data after account closure for legal/
# operational reasons). Deliberately narrow (requires "account" AND a
# closure verb close together) so this never fires on unrelated text -
# and deliberately does NOT include generic privacy/security assurance
# language ("information is protected", "handled securely", "never
# shared publicly") as a signal of anything: that's boilerplate, not
# deletion evidence, and is intentionally left unmatched everywhere.
# Checked AFTER COMPLETED/REJECTED in _PATTERN_ORDER, so an explicit
# stronger claim (e.g. "account closed and your data deleted") still
# resolves to COMPLETED first - this category only ever catches the
# account-only case COMPLETED_PATTERNS deliberately doesn't.
#
# The last (reverse-order, "account ... verb") pattern is deliberately
# NOT a wide character-count gap like the four "verb ... account"
# patterns above - a real company reply (Goop Kitchen) needed
# "the account associated with <email> has been deactivated" to match,
# and an email address alone can run past any reasonably tight char
# budget while a truly unrelated "account ... closed" pairing (e.g. "your
# account is fine; separately, our office was closed for the holidday")
# can easily fall inside a loose one. So this only tolerates two named,
# narrow shapes glued directly onto "account": an optional "associated
# with <token>" clause (the one real shape that needs to span a long
# token) and an optional short passive auxiliary ("has been"/"was"/
# "is"/"been") - not an arbitrary run of intervening text. Anything
# else between "account" and the verb fails closed to UNKNOWN_RESPONSE,
# same as before.
ACCOUNT_CLOSED_DATA_UNVERIFIED_PATTERNS = [
    r"deactivat(ed|ing).{0,40}account",
    r"clos(ed|ing).{0,40}account",
    r"terminat(ed|ing).{0,40}account",
    r"cancell?(ed|ing).{0,40}account",
    r"account(?:\s+associated with\s+\S+)?(?:\s+(?:has been|was|is|been))?\s+(deactivated|closed|terminated|cancell?ed)",
]

# A stronger claim than ACCOUNT_CLOSED_DATA_UNVERIFIED: the company says
# the account/account record/profile/membership record was DELETED (not
# merely closed/deactivated), but - per COMPLETED_PATTERNS's own
# docstring above - that alone is never enough to confirm the user's
# personal information more broadly was deleted (some companies retain
# data outside the account record itself: order/support history,
# marketing lists, backups). A real company reply exposed the gap this
# closes: "We can confirm that the account associated with this email
# address, <email>, and its details have already been deleted as
# requested." - note the long, comma-heavy "associated with <email>,
# and its details" clause between "account" and "deleted", which is why
# the gap below uses _NOT_SENTENCE_BREAK (defined at the top of this
# file) rather than a bare `.{0,N}` or a `[^.]{0,N}` character class -
# the latter would also block the dot inside an email domain like
# "gmail.com", which sits directly in that gap for the real fixture.
ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED_PATTERNS = [
    rf"delet(ed|ing){_NOT_SENTENCE_BREAK}{{0,40}}\b(account|profile|membership)\b",
    rf"\b(account|profile|membership)\b{_NOT_SENTENCE_BREAK}{{0,110}}\bdelet(ed|ing)\b",
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
    # Broadened from "received your request" - real helpdesk auto-replies
    # routinely say "your email"/"your message" instead of "your request"
    # (e.g. MALK Organics: "We have received your email...") - still a
    # purely generic acknowledgment either way, never COMPLETED/REJECTED/
    # etc., which are all checked earlier in _PATTERN_ORDER and win first.
    r"(we have |we'?ve )?received your (request|email|message)",
    r"(is|are) being processed",
    r"we are (currently )?(reviewing|processing|working on)",
    r"case (number|#|id) ?[:#]?\s*\w+",
    r"ticket (number|#|id) ?[:#]?\s*\w+",
    r"support request .{0,20}(created|opened|received)",
    # Broadened from "we will ..." - "someone from our team"/"our team"
    # will respond is the same generic acknowledgment, just phrased with a
    # different subject (e.g. MALK Organics: "someone from our team will
    # get back to you").
    r"(we|someone( from our team)?|our team) will (get back to you|respond|follow up)",
]

# Checked in order: more specific/serious signals before generic ones, so a
# reply mentioning both "we received your request" AND "has been deleted"
# (e.g. a follow-up after an earlier ack) resolves to the stronger claim.
_PATTERN_ORDER = [
    (DeletionStatus.COMPLETED, COMPLETED_PATTERNS),
    (DeletionStatus.REJECTED, REJECTED_PATTERNS),
    (DeletionStatus.ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED, ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED_PATTERNS),
    (DeletionStatus.ACCOUNT_CLOSED_DATA_UNVERIFIED, ACCOUNT_CLOSED_DATA_UNVERIFIED_PATTERNS),
    (DeletionStatus.VERIFICATION_NEEDED, VERIFICATION_NEEDED_PATTERNS),
    (DeletionStatus.MORE_INFO_REQUIRED, MORE_INFO_REQUIRED_PATTERNS),
    (DeletionStatus.IN_PROGRESS, IN_PROGRESS_PATTERNS),
]

LLM_CLASSIFY_PROMPT = """You are reading ONE email reply from a company, sent in response to a data-deletion request that was already sent to them. Classify it into exactly one of these labels:

SUBMITTED, IN_PROGRESS, VERIFICATION_NEEDED, MORE_INFO_REQUIRED, ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED, ACCOUNT_CLOSED_DATA_UNVERIFIED, COMPLETED, REJECTED, UNKNOWN_RESPONSE

Rules:
- COMPLETED means the company explicitly states that the user's PERSONAL INFORMATION/DATA (not merely an account, account record, profile, or membership record) has ALREADY been deleted - not that they received the request, opened a ticket, or are reviewing it. A generic acknowledgement is IN_PROGRESS, never COMPLETED. "Your account has been deleted", "your profile has been deleted", or similar account-scoped-only language is NEVER enough on its own - use ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED for that instead.
- ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED means the company confirms the ACCOUNT, account record, profile, or membership record was DELETED (a stronger claim than closure - e.g. "the account associated with your email has been deleted"), but does NOT explicitly confirm the user's personal information more broadly (which may extend beyond the account record) was deleted.
- ACCOUNT_CLOSED_DATA_UNVERIFIED means the company confirms closing/deactivating the ACCOUNT (e.g. "we've deactivated your account as requested"), but does NOT explicitly confirm the underlying personal DATA/information was deleted. Generic privacy/security assurances ("your information is protected", "handled securely", "never shared publicly") are NOT deletion confirmation and must never upgrade this to COMPLETED - use COMPLETED only if the reply separately, explicitly states the data itself was deleted/removed/erased.
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


def _normalize_whitespace(text: str) -> str:
    """Collapses any run of whitespace - including the mid-sentence line
    wraps real email clients routinely insert (a real Goop Kitchen reply
    split "...Insider" and "account associated with..." across a hard
    line break) - into a single space. Python's `.` does not match `\\n`
    by default, so a gap pattern like `deactivat(ed|ing).{0,40}account`
    silently fails to match its own target phrase whenever the wrap
    happens to land inside that gap - not a wording problem, a plain-text
    formatting artifact the classifier must not be sensitive to. Applied
    once, before every pattern in _PATTERN_ORDER, so this fixes the same
    latent gap for all of them, not just the account-closure category."""
    return re.sub(r"\s+", " ", text)


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
        # Matching AND quoting both run against the same normalized text
        # (see _normalize_whitespace's docstring) so a match's position is
        # always valid for slicing the text _quote_around uses - never mix
        # a match found in normalized text with offsets into the raw one.
        normalized = _normalize_whitespace(message_text)
        text_lower = normalized.lower()
        for status, patterns in _PATTERN_ORDER:
            match = _find_match(text_lower, patterns)
            if match:
                return ResponseClassification(
                    status=status, confidence="high",
                    quote=_quote_around(normalized, match),
                    reasons=[f"matched /{match}/"],
                )
        return ResponseClassification(
            status=DeletionStatus.UNKNOWN_RESPONSE, confidence="low",
            quote=normalized.strip()[:MAX_QUOTE_LEN], reasons=["no known pattern matched"],
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
