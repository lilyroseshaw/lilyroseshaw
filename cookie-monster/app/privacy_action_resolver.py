"""Synchronous, on-demand resolution for a single PrivacyAction - the "Find
cleanup method" button's server-side counterpart. Deliberately NOT part of
any background queue/worker (see the Just the Essentials research-pipeline
scope guard: no background JTE chase) - unlike deletion_resolver.py's
process_pending(), there is no scheduled retry here; the user decides when
to look again.

Mirrors deletion_resolver.py's safety philosophy in miniature:
  - An in-flight guard (per PrivacyAction id) so a double-click can never
    start two overlapping research attempts racing to write the same row.
  - A Brave-budget-exhausted attempt is never counted as a failed lookup -
    the action is left completely untouched, just deferred (a distinct,
    non-failure audit event), and becomes eligible again on the next click.
  - Every path is auditable via DeletionEvent.
"""
import threading

from sqlalchemy.orm import Session

from app.deletion_constants import EventType, PrivacyActionStatus
from app.deletion_events import record_event
from app.models import Company, PrivacyAction
from app.privacy_action_research import (
    PrivacyActionResearchProvider,
    PrivacyMechanismSourceBlocked,
    PrivacyMechanismUnverified,
)
from app.research_search import BraveBudgetExhausted

# In-process, per-PrivacyAction-id lock - same pattern/reasoning as
# deletion_resolver.py's _in_flight_domains (a double-click, or a second
# tab, must never start two overlapping research attempts for the exact
# same PrivacyAction row). In-memory only, single-process prototype.
_in_flight_ids: set[int] = set()
_in_flight_lock = threading.Lock()


def _try_start(action_id: int) -> bool:
    with _in_flight_lock:
        if action_id in _in_flight_ids:
            return False
        _in_flight_ids.add(action_id)
        return True


def _finish(action_id: int) -> None:
    with _in_flight_lock:
        _in_flight_ids.discard(action_id)


def resolve_privacy_action(
    db: Session, privacy_action: PrivacyAction, company: Company, provider: PrivacyActionResearchProvider,
) -> bool:
    """Runs one research attempt for `privacy_action` and updates it in
    place. Returns True if an attempt actually ran (a double-click on the
    same action while one is already in flight is a silent no-op, False -
    the in-flight attempt resolves on its own shortly).

    A verified mechanism moves the action to USER_ACTION_REQUIRED - never
    further: opening/using it is a user hand-off, never automated
    submission in this milestone (see app/privacy_action_research.py). No
    verified mechanism moves it to NEEDS_REVIEW - a distinct, non-
    fabricated "looked, found nothing safe to use yet" outcome, never left
    at NEEDS_RESEARCH (which means "hasn't been looked at") and never a
    failure state implying something went wrong on Baker's Dozen's end.
    NEEDS_REVIEW is not terminal: calling this again later (e.g. after the
    company changes its site) is always allowed and may find something."""
    if not _try_start(privacy_action.id):
        return False
    try:
        try:
            result = provider.research(company.domain, privacy_action.action_type)
        except BraveBudgetExhausted:
            record_event(
                db, company.id, EventType.RESEARCH_DEFERRED,
                evidence={
                    "domain": company.domain, "action_type": privacy_action.action_type,
                    "reason": "brave_budget_exhausted",
                },
                privacy_action_id=privacy_action.id,
            )
            db.commit()
            return True
        except PrivacyMechanismSourceBlocked as exc:
            _apply_needs_review(db, privacy_action, company, reason="source_blocked", blocked_url=exc.url)
            return True
        except PrivacyMechanismUnverified as exc:
            _apply_needs_review(db, privacy_action, company, reason="unverified_source", unverified_lead_url=exc.url)
            return True

        if result is not None and result.verified:
            privacy_action.method = result.method
            privacy_action.url = result.url
            privacy_action.status = PrivacyActionStatus.USER_ACTION_REQUIRED
            privacy_action.evidence = {
                "source_url": result.source_url,
                "referring_official_url": result.referring_official_url,
                "confidence": result.confidence,
                "reasons": result.reasons,
                "scope_note": result.scope_note,
            }
            record_event(
                db, company.id, EventType.PRIVACY_ACTION_METHOD_FOUND,
                evidence={
                    "action_type": privacy_action.action_type, "method": result.method,
                    "confidence": result.confidence, "source_url": result.source_url,
                },
                privacy_action_id=privacy_action.id,
            )
            db.commit()
        else:
            _apply_needs_review(db, privacy_action, company, reason="no_official_mechanism_found")
        return True
    finally:
        _finish(privacy_action.id)


def _apply_needs_review(
    db: Session, privacy_action: PrivacyAction, company: Company, reason: str,
    blocked_url: str | None = None, unverified_lead_url: str | None = None,
) -> None:
    evidence = {"reason": reason}
    if blocked_url:
        evidence["blocked_url"] = blocked_url
    if unverified_lead_url:
        evidence["unverified_lead_url"] = unverified_lead_url
    privacy_action.status = PrivacyActionStatus.NEEDS_REVIEW
    privacy_action.evidence = evidence
    record_event(
        db, company.id, EventType.PRIVACY_ACTION_NEEDS_REVIEW,
        evidence={"action_type": privacy_action.action_type, **evidence},
        privacy_action_id=privacy_action.id,
    )
    db.commit()
