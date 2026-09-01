import asyncio
import datetime
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db as dbmod
from app import config, deletion_queue
from app.db import Base
from app.deletion_constants import DeletionStatus
from app.deletion_research import DeletionResearchProvider
from app.deletion_resolver import enqueue_pending
from app.models import Company
from app.research_types import ResearchResult


@pytest.fixture()
def file_db(monkeypatch):
    """The queue worker opens its own sessions via app.db.get_session(), so
    it needs a real file-backed engine (not an in-process :memory: one tied
    to a single connection)."""
    path = tempfile.mktemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(dbmod, "engine", engine)
    monkeypatch.setattr(dbmod, "SessionLocal", sessionmaker(bind=engine))
    yield
    Path(path).unlink(missing_ok=True)


class FakeProvider(DeletionResearchProvider):
    def search_official_sources(self, company_name, domain):
        return []

    def inspect_privacy_page(self, url, domain):
        return None

    def extract_deletion_recipe(self, company_name, domain, pages):
        return None

    def verify_recipe(self, domain, result):
        return True

    def research(self, company_name, domain):
        return ResearchResult(
            domain=domain, method="EMAIL_REQUEST", email=f"privacy@{domain}",
            source_url=f"https://{domain}/privacy", confidence="high", verified=True, reasons=["t"],
        )


def test_worker_tick_processes_queued_companies(file_db, monkeypatch):
    db = dbmod.get_session()
    company = Company(
        name="Widget Co", domain="widgetco.com", relationship_type="transactional", status="confirmed",
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        deletion_status=DeletionStatus.NOT_STARTED,
    )
    db.add(company)
    db.commit()
    enqueue_pending(db, [company])
    db.commit()
    db.close()

    monkeypatch.setattr(config, "DELETION_QUEUE_INTERVAL_SECONDS", 0.05)

    async def run():
        deletion_queue.start_background_worker(provider=FakeProvider())
        await asyncio.sleep(0.2)
        deletion_queue.stop_background_worker()

    asyncio.run(run())

    db2 = dbmod.get_session()
    refreshed = db2.query(Company).filter(Company.domain == "widgetco.com").one()
    assert refreshed.deletion_status == DeletionStatus.READY
    assert refreshed.deletion_email == "privacy@widgetco.com"
    db2.close()


def test_start_background_worker_is_idempotent():
    async def run():
        task1 = deletion_queue.start_background_worker(provider=FakeProvider())
        task2 = deletion_queue.start_background_worker(provider=FakeProvider())
        assert task1 is task2
        deletion_queue.stop_background_worker()

    asyncio.run(run())


def test_worker_tick_survives_provider_exceptions(file_db, monkeypatch):
    """A single research failure must not kill the whole background loop -
    it should log and keep ticking."""

    class ExplodingProvider(DeletionResearchProvider):
        def search_official_sources(self, company_name, domain):
            raise RuntimeError("network is on fire")

        def inspect_privacy_page(self, url, domain):
            return None

        def extract_deletion_recipe(self, company_name, domain, pages):
            return None

        def verify_recipe(self, domain, result):
            return False

    db = dbmod.get_session()
    company = Company(
        name="Widget Co", domain="widgetco.com", relationship_type="transactional", status="confirmed",
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        deletion_status=DeletionStatus.NOT_STARTED,
    )
    db.add(company)
    db.commit()
    enqueue_pending(db, [company])
    db.commit()
    db.close()

    monkeypatch.setattr(config, "DELETION_QUEUE_INTERVAL_SECONDS", 0.05)

    async def run():
        deletion_queue.start_background_worker(provider=ExplodingProvider())
        await asyncio.sleep(0.2)  # would raise/kill the loop if unhandled
        deletion_queue.stop_background_worker()

    asyncio.run(run())  # test passes simply by not raising
