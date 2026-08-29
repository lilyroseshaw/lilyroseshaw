"""Rule-based evidence classifier.

Deliberately NOT a black box: every match records which pattern fired, so the
UI can show "Detected because: ..." (spec section 11) and a human can
overrule it. Nothing here reads message bodies - only Subject/From/Date
headers, which is all the gmail.metadata scope ever returns.

This is intentionally conservative: a message that matches none of the
evidence patterns below produces NO company record at all (spec section 2:
"do not treat every sender as a company relationship").
"""
import re
from dataclasses import dataclass, field
from email.utils import parseaddr

import tldextract

# suffix_list_urls=() disables tldextract's live fetch of the public suffix
# list at runtime - it uses the bundled snapshot only. This keeps the app's
# network footprint limited to the Google/Gmail API, as documented in the README.
_tld_extract = tldextract.TLDExtract(suffix_list_urls=())

# Ordered by evidentiary strength: a message that matches multiple patterns
# is filed under the strongest signal, since that's the most reliable proof
# of a real relationship.
EVIDENCE_PATTERNS: list[tuple[str, str, list[str]]] = [
    # (evidence_type, relationship_type, [regex patterns against lowercased subject])
    ("order_confirmation", "transactional", [
        r"\border confirm", r"\byour order\b", r"order (has been )?received",
        r"order #\s?\d+", r"thank you for your order", r"order summary",
        r"your .{0,30}order (has been |is |was )?(confirmed|received|placed)",
    ]),
    ("receipt", "transactional", [
        r"\breceipt\b", r"\binvoice\b", r"payment (confirmation|received)",
        r"\byour payment\b", r"billing statement",
    ]),
    ("shipping_confirmation", "transactional", [
        r"has shipped", r"on its way", r"tracking (number|info)",
        r"out for delivery", r"\bdelivered\b", r"shipping confirmation",
    ]),
    ("password_reset", "account", [
        r"reset your password", r"password reset", r"forgot your password",
        r"reset link",
    ]),
    ("account_verification", "account", [
        r"verify your (email|account)", r"confirm your (email|account)",
        r"verification code", r"security code", r"two-factor", r"one-time (code|password)",
    ]),
    ("account_creation", "account", [
        r"welcome to\b", r"account (has been )?created", r"thanks for (signing up|joining)",
        r"your new account", r"get started with",
    ]),
    ("customer_service", "account", [
        r"support ticket", r"case #\s?\d+", r"we('ve| have) received your (request|inquiry|message)",
        r"customer (support|service)", r"your (support|service) request",
    ]),
    ("subscription_confirmation", "subscription", [
        r"subscription confirmed", r"you'?re subscribed", r"subscription (started|renewed|active|has been)",
        r"your plan (has been|is)", r"trial (has started|is ending)",
    ]),
    ("membership", "subscription", [
        r"membership", r"member benefits", r"your member",
    ]),
    ("loyalty_rewards", "subscription", [
        r"\brewards\b", r"\bloyalty\b", r"points balance", r"earned .{0,15}points",
    ]),
    ("marketing_newsletter", "marketing", [
        r"newsletter", r"\d+% off", r"sale ends", r"exclusive offer", r"new arrivals",
    ]),
]

# Consumer webmail / ISP domains are never "companies you have a relationship with"
# for the purposes of this tool, even though people email each other through them.
PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "icloud.com", "me.com", "aol.com", "live.com", "msn.com", "protonmail.com",
    "proton.me", "mail.com", "comcast.net", "verizon.net", "att.net", "sbcglobal.net",
    "gmx.com", "yandex.com", "zoho.com",
}

_SUFFIX_STRIP = re.compile(
    r"\s*(no[-\s]?reply|do[-\s]?not[-\s]?reply|team|support|customer service|notifications?|inc\.?|llc)\s*$",
    re.IGNORECASE,
)


@dataclass
class Classification:
    domain: str
    company_name: str
    evidence_type: str
    relationship_type: str
    reasons: list[str] = field(default_factory=list)
    subject: str = ""


def extract_domain(from_header: str) -> str | None:
    _, email_addr = parseaddr(from_header or "")
    if "@" not in email_addr:
        return None
    raw_domain = email_addr.split("@")[-1].lower().strip()
    ext = _tld_extract(raw_domain)
    if not ext.domain or not ext.suffix:
        return None
    return f"{ext.domain}.{ext.suffix}"


def _guess_company_name(from_header: str, domain: str) -> str:
    display_name, email_addr = parseaddr(from_header or "")
    display_name = (display_name or "").strip().strip('"')
    if display_name and display_name.lower() != email_addr.lower():
        cleaned = _SUFFIX_STRIP.sub("", display_name).strip(" -|,")
        if cleaned and re.search(r"[a-zA-Z]", cleaned):
            return cleaned
    root = _tld_extract(domain).domain
    return root.capitalize()


def classify_message(subject: str, from_header: str) -> Classification | None:
    """Returns a Classification if this message is evidence of a company
    relationship, or None if it should be ignored entirely (not stored)."""
    domain = extract_domain(from_header)
    if not domain or domain in PERSONAL_EMAIL_DOMAINS:
        return None

    subject_lower = (subject or "").lower()
    for evidence_type, relationship_type, patterns in EVIDENCE_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, subject_lower):
                return Classification(
                    domain=domain,
                    company_name=_guess_company_name(from_header, domain),
                    evidence_type=evidence_type,
                    relationship_type=relationship_type,
                    reasons=[
                        f"Sender domain '{domain}' is not a personal email provider",
                        f"Subject matched '{evidence_type}' pattern: /{pattern}/",
                    ],
                    subject=subject or "",
                )
    return None
