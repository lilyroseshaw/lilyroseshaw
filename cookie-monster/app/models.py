"""Persisted schema.

Deliberately NOT stored anywhere in this app: message IDs, email headers
beyond what's aggregated below, message bodies/snippets (never fetched -
see config.GMAIL_SCOPES), attachments, sender email addresses, names,
order numbers, addresses, or payment info.

`Company` rows are the aggregate evidence record described in the README:
company / domain / relationship_type / evidence_type / first_seen /
last_seen / confidence - plus a small, capped list of example subject
lines and human-readable detection reasons for transparency.

Also holds deletion-request tracking (see deletion_resolver.py /
deletion_engine.py): what method a company uses, how automatable it is,
and a structured status/evidence trail. `deletion_evidence` is restricted
to non-secret operational evidence (message IDs, timestamps, confirmation
references) - never credentials, tokens, or third-party account data.
"""
import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.deletion_constants import (
    ActionCapability,
    DeletionMethod,
    DeletionStatus,
    EventSource,
    EventType,
    RecipeOrigin,
    RecipeStatus,
)


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)

    # transactional | account | subscription | marketing | mixed
    relationship_type: Mapped[str] = mapped_column(String(32))

    # pending | confirmed | rejected
    status: Mapped[str] = mapped_column(String(16), default="pending")

    # high | medium | low
    confidence: Mapped[str] = mapped_column(String(16))

    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    evidence_types: Mapped[list] = mapped_column(JSON, default=list)  # e.g. ["order_confirmation", "receipt"]
    example_subjects: Mapped[list] = mapped_column(JSON, default=list)  # capped at 3, truncated
    detection_reasons: Mapped[list] = mapped_column(JSON, default=list)  # capped, human-readable

    first_seen: Mapped[datetime.date] = mapped_column(DateTime)
    last_seen: Mapped[datetime.date] = mapped_column(DateTime)

    user_corrected: Mapped[bool] = mapped_column(default=False)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow
    )

    # --- Deletion-request tracking (see deletion_resolver.py / deletion_engine.py) ---
    # How this company accepts deletion requests. One of DeletionMethod.*
    deletion_method: Mapped[str] = mapped_column(String(32), default=DeletionMethod.UNKNOWN)
    # How much of the request Cookie Monster can perform itself. One of ActionCapability.*
    deletion_action_capability: Mapped[str] = mapped_column(String(32), default=ActionCapability.UNKNOWN)
    # Current progress through the request. One of DeletionStatus.*
    deletion_status: Mapped[str] = mapped_column(String(32), default=DeletionStatus.NOT_STARTED)

    deletion_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    deletion_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    deletion_instructions: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # True only when copied from a DeletionRecipe with status=VERIFIED (either a
    # seed entry or one that passed DeletionResearchProvider.verify_recipe) -
    # never set by unattended inference. See deletion_resolver.py.
    deletion_verified: Mapped[bool] = mapped_column(default=False)
    # The official page the applied recipe was verified against.
    deletion_source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    deletion_last_checked: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    deletion_requested_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    deletion_completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    # Structured, non-secret evidence only: message IDs, HTTP status, confirmation
    # references, timestamps. Never credentials, tokens, or third-party account data.
    deletion_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    deletion_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Gmail thread ID for a sent EMAIL_REQUEST - captured now so a future
    # response-tracker (Phase 2) can monitor only this specific thread,
    # never the general inbox.
    deletion_thread_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # --- Response tracking (see deletion_response_tracker.py) ---
    # The last Gmail message ID in deletion_thread_id that was classified -
    # dedup marker so a reply is never processed twice.
    deletion_last_response_message_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Last time a check was *attempted* (success or failure) - used with
    # deletion_response_check_failures to compute when this thread is next
    # due, per config.RESPONSE_CHECK_MIN_INTERVAL_HOURS / the backoff policy.
    deletion_response_checked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    # Consecutive *technical* check failures (Gmail API/network errors) -
    # reset to 0 on any successful check. Drives exponential backoff.
    # Never incremented by a company's reply content, only by the check
    # mechanism itself failing to run.
    deletion_response_check_failures: Mapped[int] = mapped_column(Integer, default=0)


class DeletionRecipe(Base):
    """The shared, reusable cache/knowledge base: 'how does this domain handle
    deletion requests'. One row per normalized domain, independent of any one
    Company row or user - populated once (by a seed, research, or manual
    entry) and reused by every company/scan that ever hits that domain again.
    This is the thing that's supposed to grow automatically as Cookie Monster
    encounters new companies - see deletion_resolver.py.
    """

    __tablename__ = "deletion_recipes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    canonical_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    method: Mapped[str] = mapped_column(String(32), default=DeletionMethod.UNKNOWN)
    action_capability: Mapped[str] = mapped_column(String(32), default=ActionCapability.UNKNOWN)

    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    login_required: Mapped[bool | None] = mapped_column(nullable=True)
    email_verification_expected: Mapped[bool | None] = mapped_column(nullable=True)
    identity_verification_expected: Mapped[bool | None] = mapped_column(nullable=True)
    deletes_account: Mapped[bool | None] = mapped_column(nullable=True)

    known_consequences: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    instructions: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    required_subject: Mapped[str | None] = mapped_column(String(300), nullable=True)
    required_request_fields: Mapped[list] = mapped_column(JSON, default=list)  # e.g. ["full_name", "account_email"]
    jurisdiction_notes: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # The definitive page this recipe was verified against.
    source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # If source_url is a third-party portal, the company's own official page
    # that linked to it - required evidence for accepting a third-party portal.
    referring_official_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_type: Mapped[str | None] = mapped_column(String(50), nullable=True)  # SourceType.*

    confidence: Mapped[str] = mapped_column(String(16), default="low")  # high | medium | low
    status: Mapped[str] = mapped_column(String(16), default=RecipeStatus.UNKNOWN)  # RecipeStatus.*
    origin: Mapped[str] = mapped_column(String(16), default=RecipeOrigin.RESEARCHED)  # RecipeOrigin.*
    recipe_version: Mapped[int] = mapped_column(Integer, default=1)

    verified_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    research_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_attempted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow
    )


class DeletionEvent(Base):
    """Append-only audit trail of what actually happened for a deletion
    request, so status is never the only record - see deletion_events.py.
    """

    __tablename__ = "deletion_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(32))  # EventType.*
    source: Mapped[str] = mapped_column(String(16), default=EventSource.SYSTEM)  # EventSource.*
    occurred_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
    # Safe, non-secret evidence only - same rule as Company.deletion_evidence.
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    recipe_id: Mapped[int | None] = mapped_column(ForeignKey("deletion_recipes.id"), nullable=True)
    recipe_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)


class OAuthToken(Base):
    """Single-row table: this is a single-local-user prototype, not a multi-tenant app."""

    __tablename__ = "oauth_token"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gmail_address: Mapped[str] = mapped_column(String(255))
    encrypted_refresh_token: Mapped[str] = mapped_column(String(2000))
    scopes_granted: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
