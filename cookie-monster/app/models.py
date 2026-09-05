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

    # --- 24-hour chase (see chase_engine.py) ---
    # Whose move it is right now - COMPANY/USER/ESCALATION_NEEDED/None
    # (None = not an active chase case at all, e.g. never sent or already
    # terminal). Drives whether the scheduler will ever touch this company.
    waiting_on: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # When the next automated follow-up is due - None means nothing is
    # scheduled (paused, waiting on the user, or terminal). Only ever
    # advanced by a REAL event: the original send, a confirmed follow-up
    # send, or the user completing a required action - never by a generic
    # company acknowledgment, which must NOT push this further out.
    next_followup_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    # How many follow-ups Baker's Dozen has itself actually sent (never
    # incremented by company replies) - internal audit/idempotency counter,
    # never shown to the company in the follow-up text itself.
    followup_attempt: Mapped[int] = mapped_column(Integer, default=0)
    last_followup_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    # Set immediately before attempting a follow-up Gmail send, cleared on
    # a definite outcome (success or confirmed-not-sent) - see
    # chase_engine.reconcile_ambiguous_followup for what happens if a
    # process dies with this still set.
    followup_locked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    # User-controlled, independent of waiting_on - "why is nothing
    # scheduled" must always be distinguishable as paused vs waiting-on-
    # user vs escalation vs terminal, never collapsed into one flag.
    followups_paused: Mapped[bool] = mapped_column(default=False)


class PrivacyCase(Base):
    """The user's explicit disposition for a company - see RecipeChoice in
    deletion_constants.py. Persists ONLY user intent/case identity, never a
    derived outcome: whether the account was closed, personal data was
    deleted, etc. is computed fresh from Company/DeletionEvent evidence by
    derive_case_outcome() (to be added in a later commit of this milestone),
    never cached here - there must be exactly one source of truth for that,
    and it's the existing audited deletion/chase state, not a second stored
    projection of it.

    selected_recipe is nullable, and NULL is a legitimate, permanent state:
    "no Cleanup Recipe explicitly selected" - either a brand-new company the
    user hasn't chosen a recipe for yet, or a legacy case that predates the
    recipe picker (see migrations.py's backfill, which creates one of these
    per pre-existing Company with selected_recipe left NULL rather than
    inferring FULL_CLEAN from old deletion-request activity). Pantry
    membership is derived as selected_recipe == RecipeChoice.LEAVE_IT_BE,
    never stored as a separate flag - see the "no is_pantry column" rule in
    the Cleanup Recipes design.

    One row per Company (1:1) for this milestone - company_id is unique."""

    __tablename__ = "privacy_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), unique=True, index=True)
    # RecipeChoice.* or None ("no Cleanup Recipe explicitly selected").
    selected_recipe: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # When selected_recipe was last set - None whenever selected_recipe is
    # None. See EventType.RECIPE_SELECTED for the append-only history of
    # every selection/re-selection, of which this is only the latest.
    recipe_selected_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow
    )


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
    # Set only for a RECIPE_SELECTED event (see EventType.RECIPE_SELECTED) -
    # nullable/backward-compatible so every pre-existing event row, and every
    # event type unrelated to Cleanup Recipes, is untouched by this column's
    # addition. Not a general-purpose link: other event types leave this NULL.
    privacy_case_id: Mapped[int | None] = mapped_column(ForeignKey("privacy_cases.id"), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)


class MailMessage(Base):
    """One row per Gmail message that is part of a company's tracked
    privacy-request correspondence - inbound (a company's reply) or
    outbound (a request Baker's Dozen sent, including a follow-up reply
    sent through the mailbox's Respond flow). This is the ONLY new table
    the mailbox feature needs; a company's current mailbox
    state (unread/action-needed/etc.) is deliberately NOT stored here -
    see app/mail.py's MailState, computed fresh from these rows plus
    Company.deletion_status every time, so there is never a second,
    driftable copy of what deletion_status/response tracking already
    know. Never holds attachments, full raw MIME, or the quoted history
    of a reply - see app/mail.py for what's stripped before a row is ever
    created, and deletion_response_tracker.py for the privacy boundary
    (this table is only ever populated from a thread already fetched via
    that module's single-thread-only Gmail read)."""

    __tablename__ = "mail_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)

    # "inbound" (a company's reply) | "outbound" (a request/reply Baker's
    # Dozen sent, as the connected user, after explicit approval)
    direction: Mapped[str] = mapped_column(String(16))

    # Gmail's own message id - the dedup key. Always set: nothing is ever
    # persisted here before a real send/receive actually happened (no
    # drafts - see app/mail.py's preview/send split, same pattern as the
    # existing deletion/preview + deletion/execute routes).
    gmail_message_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    # Mirrors Company.deletion_thread_id at the time this row was created -
    # denormalized for display/audit convenience only, never a second
    # source of truth for which thread is tracked.
    gmail_thread_id: Mapped[str] = mapped_column(String(100), index=True)
    # The RFC822 Message-ID header (distinct from gmail_message_id, Gmail's
    # own API id) - inbound only, needed so an outbound reply can set
    # In-Reply-To/References and land in the same Gmail thread correctly.
    rfc822_message_id: Mapped[str | None] = mapped_column(String(300), nullable=True)

    occurred_at: Mapped[datetime.datetime] = mapped_column(DateTime)
    # Inbound: the message's "From" header, shown as-is in the letter UI -
    # the exact same transparency the existing attach-thread preview
    # already shows the user. Outbound: "You".
    from_display: Mapped[str] = mapped_column(String(300))
    subject: Mapped[str] = mapped_column(String(300))
    # The NEW content only - quoted history already stripped (see
    # app/mail.py), capped well short of a full message. Never the raw
    # MIME, never an attachment.
    body_excerpt: Mapped[str] = mapped_column(String(4000))

    # Inbound only - the same DeletionStatus.* classification
    # response_classify.py already produces for this exact message.
    # Never set for an outbound row.
    classification_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    classification_confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    classification_quote: Mapped[str | None] = mapped_column(String(300), nullable=True)

    # NULL = unread. Outbound rows are stamped read at creation (the user
    # just sent it) so unread-counting logic only ever needs to look at
    # this one column, never branch on direction.
    read_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)


class OAuthToken(Base):
    """Single-row table: this is a single-local-user prototype, not a multi-tenant app."""

    __tablename__ = "oauth_token"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gmail_address: Mapped[str] = mapped_column(String(255))
    encrypted_refresh_token: Mapped[str] = mapped_column(String(2000))
    scopes_granted: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
