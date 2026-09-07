"""Shared dataclasses for the deletion-research AND privacy-action-research
pipelines (deletion_research.py and privacy_action_research.py) - kept
separate from either to avoid a circular import between each interface
module and the concrete crawl/search/extract modules that implement it.
CandidateSource/ResearchResult are Full Clean's own; PrivacyActionResult is
Just the Essentials' - deliberately not reused as one shape (see
PrivacyActionResult's own docstring).
"""
from dataclasses import dataclass, field


@dataclass
class CandidateSource:
    url: str
    kind: str  # SourceType.* - refined during extraction, this is a pre-fetch guess
    discovered_via: str  # "common_path_guess" | "homepage_link" | "search:brave" | ...
    anchor_text: str = ""


@dataclass
class ResearchResult:
    domain: str
    method: str  # DeletionMethod.*
    url: str | None = None
    email: str | None = None
    login_required: bool | None = None
    email_verification_expected: bool | None = None
    identity_verification_expected: bool | None = None
    deletes_account: bool | None = None
    known_consequences: str | None = None
    required_subject: str | None = None
    instructions: str | None = None
    source_url: str = ""
    referring_official_url: str | None = None
    source_type: str = ""  # SourceType.*
    confidence: str = "low"  # high | medium | low
    verified: bool = False
    reasons: list[str] = field(default_factory=list)


@dataclass
class PrivacyActionResult:
    """Result of researching ONE PrivacyActionType for a domain - see
    app/privacy_action_research.py. Deliberately a separate dataclass from
    ResearchResult, not a reuse of it: a PrivacyAction mechanism (stop
    nonessential tracking, opt out of sale/sharing) is a different kind of
    outcome than a full-deletion mechanism, and conflating the two
    dataclasses would make it easy to accidentally pass one pipeline's
    result to the other's caller.

    `scope_note` is the truthful, always-attached limitation of what a
    verified mechanism actually accomplishes (e.g. a "Do Not Sell/Share"
    control does not by itself prove analytics/profile data was deleted) -
    see app/privacy_action_research.py's TRACKING_CLEANUP_SCOPE_NOTE/
    OPT_OUT_SCOPE_NOTE."""
    domain: str
    action_type: str  # PrivacyActionType.*
    method: str  # DeletionMethod.* (reused vocabulary - see models.PrivacyAction)
    url: str | None = None
    login_required: bool | None = None
    source_url: str = ""
    referring_official_url: str | None = None
    source_type: str = ""  # SourceType.*
    confidence: str = "low"  # high | medium | low
    scope_note: str = ""
    verified: bool = False
    reasons: list[str] = field(default_factory=list)
