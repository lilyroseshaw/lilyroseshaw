"""Turns fetched page(s) into a ResearchResult. Two passes:

Pass 1 (always on, no cost): regex/keyword heuristics, same philosophy as
app/classifier.py's email evidence detector - deterministic, auditable,
every match records which signal fired.

Pass 2 (optional, needs an Anthropic API key): LLM-assisted extraction for
privacy-policy prose too varied for regex. The model is used purely as a
*locator*, never a source of facts: any URL/email it returns is discarded
unless it's found verbatim in the fetched page text it was given. It cannot
introduce a URL/email that wasn't already on the page.

Neither pass ever fabricates a URL/email. If nothing verifiable is found,
extract() returns None and the caller (deletion_research.py) marks the
recipe NEEDS_RESEARCH rather than guessing.
"""
import json
import re

from app.deletion_constants import DeletionMethod, SourceType
from app.research_fetch import PageContent
from app.research_types import ResearchResult

DELETION_SIGNAL_PATTERNS = [
    r"delete my data", r"delete (your |my )?account", r"right to deletion",
    r"right to be forgotten", r"privacy request", r"data rights request",
    r"ccpa (deletion )?request", r"delete (your )?personal information",
    r"consumer privacy request", r"\bdo not sell\b", r"request (to )?delete",
]
LOGIN_REQUIRED_PATTERNS = [
    r"sign in to (your account|delete)", r"log ?in to (your account|delete)",
    r"from your account settings", r"in your account, (go to|navigate to)",
]
EMAIL_VERIFICATION_PATTERNS = [
    r"verify your email", r"confirmation (link|email)", r"check your (inbox|email) to confirm",
]
IDENTITY_VERIFICATION_PATTERNS = [
    r"verify your identity", r"proof of identity", r"government[- ]issued id", r"identity verification",
]
ACCOUNT_DELETION_PATTERNS = [
    r"delete(s|d)? your account", r"close your account", r"account will be (permanently )?deleted",
    r"this will delete your account", r"deleting your account",
]

# Domains known to host third-party-operated privacy-request portals. A link
# to one of these found on an already domain-verified official page is
# acceptable evidence (see verify_recipe) - but only via that link, never guessed.
THIRD_PARTY_PORTAL_DOMAINS = [
    "onetrust.com", "osano.com", "privacyportal", "transcend.io", "truyo.com",
    "securiti.ai", "mine.com", "datagrail.io", "privado.ai",
]

LLM_EXTRACTION_PROMPT = """You are extracting facts from ONE company's own web page - not researching, not guessing, only reading what's literally on this page.

Company: {company_name}
Page URL: {page_url}
Page text (truncated):
---
{page_text}
---

Based ONLY on the text above, answer in strict JSON with these keys:
- method: one of "EMAIL_REQUEST", "WEB_FORM", "PRIVACY_PORTAL", "ACCOUNT_SETTING", "UNKNOWN"
- email: an email address ONLY if it appears verbatim in the text above, else null
- login_required: true/false/null
- email_verification_expected: true/false/null
- identity_verification_expected: true/false/null
- deletes_account: true/false/null
- known_consequences: a short plain-English sentence, or null
- confidence: "high", "medium", or "low"

Rules: NEVER invent an email address, URL, or fact not present in the text above. If the text doesn't clearly describe a deletion process, set method to "UNKNOWN" and confidence to "low". Respond with ONLY the JSON object, no other text."""


def _find_signal(text_lower: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        if re.search(pattern, text_lower):
            return pattern
    return None


class RecipeExtractor:
    def __init__(self, llm_client=None, llm_model: str | None = None):
        self._llm_client = llm_client
        self._llm_model = llm_model

    def extract(self, company_name: str, domain: str, pages: list[PageContent]) -> ResearchResult | None:
        heuristic = self._extract_heuristic(domain, pages)
        if heuristic is not None and heuristic.confidence == "high":
            return heuristic

        if self._llm_client is not None:
            llm_result = self._extract_with_llm(company_name, domain, pages)
            if llm_result is not None and (heuristic is None or llm_result.confidence != "low"):
                return llm_result

        return heuristic

    # --- Pass 1: regex/keyword heuristics ---

    def _extract_heuristic(self, domain: str, pages: list[PageContent]) -> ResearchResult | None:
        for page in pages:
            text_lower = page.text.lower()
            deletion_signal = _find_signal(text_lower, DELETION_SIGNAL_PATTERNS)
            if not deletion_signal and not page.mailto_links:
                continue

            reasons = []
            if deletion_signal:
                reasons.append(f"page text matched deletion-request phrase: /{deletion_signal}/")

            portal_link = next(
                (href for href, _ in page.external_links if any(p in href for p in THIRD_PARTY_PORTAL_DOMAINS)),
                None,
            )

            login_signal = _find_signal(text_lower, LOGIN_REQUIRED_PATTERNS)
            email_verify_signal = _find_signal(text_lower, EMAIL_VERIFICATION_PATTERNS)
            identity_verify_signal = _find_signal(text_lower, IDENTITY_VERIFICATION_PATTERNS)
            account_deletion_signal = _find_signal(text_lower, ACCOUNT_DELETION_PATTERNS)

            if portal_link:
                reasons.append(f"official page links to known privacy-portal domain: {portal_link}")
                return ResearchResult(
                    domain=domain, method=DeletionMethod.PRIVACY_PORTAL, url=portal_link,
                    login_required=bool(login_signal),
                    email_verification_expected=bool(email_verify_signal),
                    identity_verification_expected=bool(identity_verify_signal),
                    deletes_account=bool(account_deletion_signal),
                    known_consequences="This may delete your account." if account_deletion_signal else None,
                    source_url=portal_link, referring_official_url=page.url,
                    source_type=SourceType.THIRD_PARTY_VIA_OFFICIAL_LINK,
                    confidence="high", reasons=reasons,
                )

            if deletion_signal and login_signal:
                reasons.append(f"login required per: /{login_signal}/")
                return ResearchResult(
                    domain=domain, method=DeletionMethod.ACCOUNT_SETTING, url=page.url,
                    login_required=True,
                    email_verification_expected=bool(email_verify_signal),
                    identity_verification_expected=bool(identity_verify_signal),
                    deletes_account=bool(account_deletion_signal),
                    known_consequences="This may delete your account." if account_deletion_signal else None,
                    source_url=page.url, source_type=SourceType.OFFICIAL_ACCOUNT_HELP,
                    confidence="high", reasons=reasons,
                )

            if deletion_signal and page.mailto_links:
                reasons.append(f"privacy-request email found on same page: {page.mailto_links[0]}")
                return ResearchResult(
                    domain=domain, method=DeletionMethod.EMAIL_REQUEST, email=page.mailto_links[0],
                    login_required=False,
                    email_verification_expected=bool(email_verify_signal),
                    identity_verification_expected=bool(identity_verify_signal),
                    deletes_account=bool(account_deletion_signal),
                    known_consequences="This may delete your account." if account_deletion_signal else None,
                    source_url=page.url, source_type=SourceType.OFFICIAL_PRIVACY_RIGHTS_PAGE,
                    confidence="high", reasons=reasons,
                )

            if deletion_signal:
                # Found a clear deletion-request signal but no email/portal on this
                # page - treat the page itself as a web form / instructions page.
                reasons.append("deletion-request language found, but no email or portal link on this page")
                return ResearchResult(
                    domain=domain, method=DeletionMethod.WEB_FORM, url=page.url,
                    login_required=bool(login_signal),
                    email_verification_expected=bool(email_verify_signal),
                    identity_verification_expected=bool(identity_verify_signal),
                    deletes_account=bool(account_deletion_signal),
                    known_consequences="This may delete your account." if account_deletion_signal else None,
                    source_url=page.url, source_type=SourceType.OFFICIAL_PRIVACY_RIGHTS_PAGE,
                    confidence="medium", reasons=reasons,
                )

            if page.mailto_links:
                # Only a generic mailto with no deletion-specific language nearby -
                # plausible but not strong enough to trust outright.
                reasons.append(f"email present on page but no explicit deletion-request phrase: {page.mailto_links[0]}")
                return ResearchResult(
                    domain=domain, method=DeletionMethod.EMAIL_REQUEST, email=page.mailto_links[0],
                    source_url=page.url, source_type=SourceType.OFFICIAL_PRIVACY_POLICY,
                    confidence="low", reasons=reasons,
                )

        return None

    # --- Pass 2: LLM-assisted extraction (optional) ---

    def _extract_with_llm(self, company_name: str, domain: str, pages: list[PageContent]) -> ResearchResult | None:
        for page in pages[:2]:  # cap cost: at most the top 2 candidate pages
            prompt = LLM_EXTRACTION_PROMPT.format(
                company_name=company_name, page_url=page.url, page_text=page.text[:6000]
            )
            try:
                response = self._llm_client.messages.create(
                    model=self._llm_model,
                    max_tokens=400,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw = response.content[0].text
                parsed = json.loads(raw)
            except Exception:  # noqa: BLE001 - any LLM/parsing failure just skips this page
                continue

            method = parsed.get("method", "UNKNOWN")
            if method not in DeletionMethod.ALL or method == DeletionMethod.UNKNOWN:
                continue

            email = parsed.get("email")
            # Hard guardrail: the model is a locator, not a source of truth - any
            # email it returns must appear verbatim in the page we gave it.
            if email and email not in page.text:
                email = None
            if method == DeletionMethod.EMAIL_REQUEST and not email:
                continue  # claimed an email method but couldn't verify one - don't trust it

            confidence = parsed.get("confidence", "low")
            if confidence not in ("high", "medium", "low"):
                confidence = "low"

            return ResearchResult(
                domain=domain, method=method, email=email, url=page.url if method != DeletionMethod.EMAIL_REQUEST else None,
                login_required=parsed.get("login_required"),
                email_verification_expected=parsed.get("email_verification_expected"),
                identity_verification_expected=parsed.get("identity_verification_expected"),
                deletes_account=parsed.get("deletes_account"),
                known_consequences=parsed.get("known_consequences"),
                source_url=page.url, source_type=SourceType.OFFICIAL_PRIVACY_POLICY,
                confidence=confidence,
                reasons=[f"LLM-extracted from {page.url}, verified against page text"],
            )
        return None
