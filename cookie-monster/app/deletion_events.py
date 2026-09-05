"""Append-only audit trail for deletion requests (see DeletionEvent in
models.py). Every meaningful transition records one of these, so the current
status column is never the only record of what actually happened.
"""
import datetime

from sqlalchemy.orm import Session

from app.deletion_constants import EventSource
from app.models import DeletionEvent


def record_event(
    db: Session,
    company_id: int,
    event_type: str,
    evidence: dict | None = None,
    source: str = EventSource.SYSTEM,
    recipe_id: int | None = None,
    recipe_version: int | None = None,
    privacy_case_id: int | None = None,
) -> DeletionEvent:
    """Adds the event to the session but does not commit - callers include it
    in the same transaction as the status change it documents.

    privacy_case_id is None by default and only ever set for a
    RECIPE_SELECTED event (see app/privacy_case.py's select_recipe) -
    every existing call site is unaffected by this parameter's addition."""
    event = DeletionEvent(
        company_id=company_id,
        event_type=event_type,
        source=source,
        evidence=evidence or {},
        recipe_id=recipe_id,
        recipe_version=recipe_version,
        privacy_case_id=privacy_case_id,
        occurred_at=datetime.datetime.utcnow(),
    )
    db.add(event)
    return event
