"""Shared dataclasses for the deletion-research pipeline. Kept separate from
deletion_research.py to avoid a circular import between the interface module
and the concrete crawl/search/extract modules that implement it.
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
