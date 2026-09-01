import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.deletion_constants import EventSource, EventType
from app.deletion_events import record_event
from app.models import Company, DeletionEvent


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _company(db) -> Company:
    company = Company(
        name="Test Co", domain="testco.com", relationship_type="transactional", status="confirmed",
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
    )
    db.add(company)
    db.commit()
    return company


def test_record_event_persists_with_evidence(db):
    company = _company(db)
    record_event(db, company.id, EventType.EMAIL_SENT, evidence={"gmail_message_id": "abc"})
    db.commit()

    events = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).all()
    assert len(events) == 1
    assert events[0].event_type == EventType.EMAIL_SENT
    assert events[0].evidence == {"gmail_message_id": "abc"}
    assert events[0].source == EventSource.SYSTEM  # default


def test_record_event_source_can_be_user(db):
    company = _company(db)
    record_event(db, company.id, EventType.USER_MARKED_COMPLETE, source=EventSource.USER)
    db.commit()
    event = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).one()
    assert event.source == EventSource.USER


def test_events_accumulate_a_history_not_just_current_status(db):
    company = _company(db)
    record_event(db, company.id, EventType.METHOD_DISCOVERED)
    record_event(db, company.id, EventType.USER_CONFIRMED, source=EventSource.USER)
    record_event(db, company.id, EventType.EMAIL_SENT)
    db.commit()

    events = db.query(DeletionEvent).filter(DeletionEvent.company_id == company.id).order_by(DeletionEvent.id).all()
    assert [e.event_type for e in events] == [
        EventType.METHOD_DISCOVERED, EventType.USER_CONFIRMED, EventType.EMAIL_SENT,
    ]
