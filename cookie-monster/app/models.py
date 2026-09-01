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

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.deletion_constants import ActionCapability, DeletionMethod, DeletionStatus


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
    # True only if sourced from a human-reviewed registry entry - never set by
    # unattended inference. See deletion_registry.py / deletion_resolver.py.
    deletion_verified: Mapped[bool] = mapped_column(default=False)
    # The official page a human confirmed this deletion method against.
    deletion_source_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    deletion_last_checked: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    deletion_requested_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    deletion_completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    # Structured, non-secret evidence only: message IDs, HTTP status, confirmation
    # references, timestamps. Never credentials, tokens, or third-party account data.
    deletion_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    deletion_error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class OAuthToken(Base):
    """Single-row table: this is a single-local-user prototype, not a multi-tenant app."""

    __tablename__ = "oauth_token"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gmail_address: Mapped[str] = mapped_column(String(255))
    encrypted_refresh_token: Mapped[str] = mapped_column(String(2000))
    scopes_granted: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
