"""Tests for the recipe-verification reliability fixes: the startup
backfill (so an existing/legacy company becomes visible to research
without a rescan), the METHOD_LOOKUP transient status actually being used
(and never left stuck), the NO_METHOD_FOUND threshold status, dashboard
rendering of all the distinct states, and bounded concurrency in
process_pending.
"""
import datetime
import threading
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config
from app.db import Base
from app.deletion_constants import DeletionStatus, EventType, RecipeStatus, ResearchFailureReason
from app.deletion_research import DeletionResearchProvider, build_default_provider, WebResearchProvider
from app.deletion_resolver import (
    backfill_all_companies,
    enqueue_pending,
    get_or_create_recipe_stub,
    process_pending,
    recover_stuck_method_lookup,
    resolve_deletion_method,
)
from app.models import Company, DeletionEvent, DeletionRecipe
from app.research_types import ResearchResult


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _company(db, name="Widget Co", domain="widgetco.com", **overrides) -> Company:
    defaults = dict(
        name=name, domain=domain, relationship_type="transactional", status="confirmed",
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        deletion_status=DeletionStatus.NOT_STARTED,
    )
    defaults.update(overrides)
    company = Company(**defaults)
    db.add(company)
    db.commit()
    return company


def _verified_result(domain, email="privacy@example.com"):
    return ResearchResult(
        domain=domain, method="EMAIL_REQUEST", email=email,
        source_url=f"https://{domain}/privacy", confidence="high", verified=True, reasons=["test"],
    )


class FakeProvider(DeletionResearchProvider):
    def __init__(self, results: dict | None = None, raises: bool = False):
        self.results = results or {}
        self.raises = raises
        self.calls: list[str] = []

    def search_official_sources(self, company_name, domain):
        return []

    def inspect_privacy_page(self, url, domain):
        return None

    def extract_deletion_recipe(self, company_name, domain, pages):
        return None

    def verify_recipe(self, domain, result):
        return result.verified

    def research(self, company_name, domain):
        self.calls.append(domain)
        if self.raises:
            raise RuntimeError("simulated provider crash")
        return self.results.get(domain)


# --- 1. Backfill / enqueue gap ---

def test_backfill_creates_stub_for_legacy_unresolved_company(db):
    """A company that predates this pipeline - never touched by
    enqueue_pending, no DeletionRecipe row at all - must become visible
    once the startup backfill runs, with no rescan required."""
    company = _company(db)
    assert db.query(DeletionRecipe).filter(DeletionRecipe.domain == "widgetco.com").count() == 0

    created = backfill_all_companies(db)
    assert created == 1
    assert db.query(DeletionRecipe).filter(DeletionRecipe.domain == "widgetco.com").count() == 1
    db.refresh(company)
    assert company.deletion_status == DeletionStatus.UNKNOWN  # stub applied, not yet researched


def test_backfill_is_idempotent(db):
    _company(db)
    backfill_all_companies(db)
    recipe_id_after_first = db.query(DeletionRecipe).filter(DeletionRecipe.domain == "widgetco.com").one().id

    backfill_all_companies(db)
    recipes = db.query(DeletionRecipe).filter(DeletionRecipe.domain == "widgetco.com").all()
    assert len(recipes) == 1  # no duplicate
    assert recipes[0].id == recipe_id_after_first
    assert recipes[0].research_attempts == 0  # backfill never triggers research


def test_backfill_never_overwrites_a_verified_recipe(db):
    company = _company(db)
    db.add(DeletionRecipe(
        domain="widgetco.com", method="ACCOUNT_SETTING", url="https://widgetco.com/delete",
        status=RecipeStatus.VERIFIED, origin="seed", source_url="https://widgetco.com/delete",
        verified_at=datetime.datetime.utcnow(),
        expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=100),
    ))
    db.commit()

    backfill_all_companies(db)

    recipe = db.query(DeletionRecipe).filter(DeletionRecipe.domain == "widgetco.com").one()
    assert recipe.status == RecipeStatus.VERIFIED
    assert recipe.method == "ACCOUNT_SETTING"
    assert recipe.url == "https://widgetco.com/delete"
    db.refresh(company)
    assert company.deletion_status == DeletionStatus.READY
    assert company.deletion_verified is True


def test_process_pending_can_now_see_previously_invisible_company(db):
    """Reproduces the actual reported gap, then confirms the fix: before
    the backfill, a legacy company with no recipe row is invisible to
    process_pending (nothing to find); after, it's processed normally."""
    _company(db, domain="invisible.com")
    provider = FakeProvider({"invisible.com": _verified_result("invisible.com")})

    processed_before = process_pending(db, provider, limit=10)
    assert processed_before == 0
    assert provider.calls == []

    backfill_all_companies(db)

    processed_after = process_pending(db, provider, limit=10)
    assert processed_after == 1
    assert provider.calls == ["invisible.com"]


def test_backfill_covers_every_company_across_multiple_domains(db):
    _company(db, name="A", domain="a.com")
    _company(db, name="B", domain="b.com")
    _company(db, name="C", domain="c.com")
    created = backfill_all_companies(db)
    assert created == 3
    assert db.query(DeletionRecipe).count() == 3
    assert {r.domain for r in db.query(DeletionRecipe).all()} == {"a.com", "b.com", "c.com"}


# --- 2. METHOD_LOOKUP actually used, never left stuck ---

class _ObservingProvider(DeletionResearchProvider):
    """Records what deletion_status the company had at the moment research()
    was called, by querying the SAME db session (safe here since this runs
    synchronously, single-threaded, in resolve_deletion_method)."""

    def __init__(self, db, company_id, result=None):
        self.db = db
        self.company_id = company_id
        self.result = result
        self.observed_status = None

    def search_official_sources(self, company_name, domain):
        return []

    def inspect_privacy_page(self, url, domain):
        return None

    def extract_deletion_recipe(self, company_name, domain, pages):
        return None

    def verify_recipe(self, domain, result):
        return result.verified if result else False

    def research(self, company_name, domain):
        company = self.db.get(Company, self.company_id)
        self.observed_status = company.deletion_status
        return self.result


def test_method_lookup_is_set_during_a_research_attempt(db):
    company = _company(db)
    provider = _ObservingProvider(db, company.id, result=_verified_result("widgetco.com"))
    resolve_deletion_method(db, company, provider)
    assert provider.observed_status == DeletionStatus.METHOD_LOOKUP


def test_method_lookup_cleared_on_success(db):
    company = _company(db)
    provider = FakeProvider({"widgetco.com": _verified_result("widgetco.com")})
    resolve_deletion_method(db, company, provider)
    assert company.deletion_status == DeletionStatus.READY


def test_method_lookup_cleared_on_failure(db):
    company = _company(db, domain="mystery.com")
    provider = FakeProvider({})
    resolve_deletion_method(db, company, provider)
    assert company.deletion_status == DeletionStatus.UNKNOWN


def test_method_lookup_cleared_on_provider_exception(db):
    """A crash inside the provider must never leave the company stuck
    showing 'Researching...' forever, and must never propagate out and
    crash the caller."""
    company = _company(db)
    provider = FakeProvider(raises=True)
    changed = resolve_deletion_method(db, company, provider)  # must not raise
    assert changed is True
    assert company.deletion_status == DeletionStatus.UNKNOWN

    event = (
        db.query(DeletionEvent)
        .filter(DeletionEvent.company_id == company.id, DeletionEvent.event_type == EventType.RESEARCH_FAILED)
        .one()
    )
    assert event.evidence["reason"] == ResearchFailureReason.TECHNICAL_ERROR
    assert "detail" in event.evidence  # capped exception text, audit-only


def test_method_lookup_set_and_cleared_in_process_pending_batch(db):
    c1 = _company(db, "A", "a.com")
    c2 = _company(db, "B", "b.com")
    enqueue_pending(db, [c1, c2])
    db.commit()
    provider = FakeProvider({"a.com": _verified_result("a.com"), "b.com": None})
    process_pending(db, provider, limit=10)
    db.refresh(c1)
    db.refresh(c2)
    assert c1.deletion_status == DeletionStatus.READY
    assert c2.deletion_status == DeletionStatus.UNKNOWN
    assert c1.deletion_status != DeletionStatus.METHOD_LOOKUP
    assert c2.deletion_status != DeletionStatus.METHOD_LOOKUP


def test_recover_stuck_method_lookup_resets_after_a_restart(db):
    """Simulates the one gap no in-process handler can cover: the process
    was killed mid-attempt, leaving a company committed in METHOD_LOOKUP.
    The startup recovery pass must reset it so it re-enters the normal
    retry cycle instead of showing 'Researching...' forever."""
    company = _company(db, deletion_status=DeletionStatus.METHOD_LOOKUP)
    recovered = recover_stuck_method_lookup(db)
    assert recovered == 1
    db.refresh(company)
    assert company.deletion_status == DeletionStatus.UNKNOWN


def test_recover_stuck_method_lookup_is_a_no_op_when_nothing_stuck(db):
    _company(db, deletion_status=DeletionStatus.READY)
    assert recover_stuck_method_lookup(db) == 0


# --- 3. NO_METHOD_FOUND threshold ---

def test_early_failures_stay_retry_scheduled_not_no_method_found(db):
    company = _company(db, domain="hard.com")
    provider = FakeProvider({})
    for _ in range(config.DELETION_RECIPE_FAILURE_THRESHOLD - 1):
        resolve_deletion_method(db, company, provider, force=True)
    assert company.deletion_status == DeletionStatus.UNKNOWN


def test_threshold_failures_produce_no_method_found(db):
    company = _company(db, domain="hard.com")
    provider = FakeProvider({})
    for _ in range(config.DELETION_RECIPE_FAILURE_THRESHOLD):
        resolve_deletion_method(db, company, provider, force=True)
    assert company.deletion_status == DeletionStatus.NO_METHOD_FOUND

    recipe = db.query(DeletionRecipe).filter(DeletionRecipe.domain == "hard.com").one()
    assert recipe.research_attempts == config.DELETION_RECIPE_FAILURE_THRESHOLD


def test_manual_retry_still_works_from_no_method_found(db):
    """Reaching NO_METHOD_FOUND must never block future manual retries -
    a later successful attempt still moves the company to READY."""
    company = _company(db, domain="hard.com")
    failing = FakeProvider({})
    for _ in range(config.DELETION_RECIPE_FAILURE_THRESHOLD):
        resolve_deletion_method(db, company, failing, force=True)
    assert company.deletion_status == DeletionStatus.NO_METHOD_FOUND

    succeeding = FakeProvider({"hard.com": _verified_result("hard.com")})
    resolve_deletion_method(db, company, succeeding, force=True)
    assert company.deletion_status == DeletionStatus.READY


def test_no_method_found_never_applies_to_an_already_verified_recipe(db):
    """A recipe that's VERIFIED must never be downgraded to NO_METHOD_FOUND
    just because a later re-check attempt failed - same "never destroy
    last-known-good data" rule as the existing UNKNOWN case."""
    company = _company(db)
    db.add(DeletionRecipe(
        domain="widgetco.com", method="ACCOUNT_SETTING", status=RecipeStatus.VERIFIED,
        source_url="https://widgetco.com/delete",
        verified_at=datetime.datetime.utcnow() - datetime.timedelta(days=200),
        expires_at=datetime.datetime.utcnow() - datetime.timedelta(days=50),
    ))
    db.commit()
    failing = FakeProvider({})
    for _ in range(config.DELETION_RECIPE_FAILURE_THRESHOLD + 2):
        resolve_deletion_method(db, company, failing, force=True)
    assert company.deletion_status == DeletionStatus.READY
    recipe = db.query(DeletionRecipe).filter(DeletionRecipe.domain == "widgetco.com").one()
    assert recipe.status == RecipeStatus.VERIFIED


# --- 4. Dashboard renders all distinct states ---

@pytest.fixture()
def client_db(tmp_path, monkeypatch):
    import app.db as dbmod

    path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(dbmod, "engine", engine)
    monkeypatch.setattr(dbmod, "SessionLocal", Session)
    monkeypatch.setattr(config, "DELETION_QUEUE_INTERVAL_SECONDS", 9999)
    session = Session()
    yield session
    session.close()


def test_dashboard_renders_all_distinct_research_states(client_db):
    from fastapi.testclient import TestClient
    from app.main import app

    db = client_db
    _company(db, "Not Started Co", "notstarted.com", deletion_status=DeletionStatus.NOT_STARTED)
    _company(db, "Researching Co", "researching.com", deletion_status=DeletionStatus.METHOD_LOOKUP)
    _company(db, "Retry Co", "retry.com", deletion_status=DeletionStatus.UNKNOWN)
    _company(db, "Failed Co", "failed.com", deletion_status=DeletionStatus.NO_METHOD_FOUND)
    _company(
        db, "Ready Co", "ready.com", deletion_status=DeletionStatus.READY,
        deletion_method="EMAIL_REQUEST", deletion_verified=True,
    )

    client = TestClient(app, base_url="http://localhost:8000")
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    text = resp.text

    assert "Deletion method not found yet" in text
    assert "Searching…" in text
    assert "Retry scheduled" in text
    assert "Couldn't find a deletion method" in text
    assert "Deletion method ready" in text


def test_dashboard_shows_attempt_count_and_retry_timing(client_db):
    from fastapi.testclient import TestClient
    from app.main import app

    db = client_db
    company = _company(db, "Retry Co", "retry.com", deletion_status=DeletionStatus.UNKNOWN)
    last_attempted = datetime.datetime.utcnow() - datetime.timedelta(days=1)
    recipe = get_or_create_recipe_stub(db, "retry.com")
    recipe.status = RecipeStatus.NEEDS_RESEARCH
    recipe.research_attempts = 2
    recipe.last_attempted_at = last_attempted
    db.commit()
    from app.deletion_events import record_event
    record_event(
        db, company.id, EventType.RESEARCH_FAILED,
        evidence={"domain": "retry.com", "research_attempts": 2, "reason": ResearchFailureReason.NO_OFFICIAL_SOURCE_FOUND},
    )
    db.commit()

    client = TestClient(app, base_url="http://localhost:8000")
    resp = client.get("/dashboard")
    text = resp.text
    assert "tried 2 of" in text
    assert last_attempted.strftime("%b %d, %Y") in text
    assert "find an official deletion/privacy page" in text
    # No raw exception dumps or secrets - only the safe category label.
    assert "Traceback" not in text
    assert "RuntimeError" not in text


# --- 5. No API key required for Tier A ---

def test_tier_a_provider_built_with_zero_api_keys(monkeypatch):
    monkeypatch.setattr(config, "BRAVE_SEARCH_API_KEY", "")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(config, "DELETION_RESEARCH_ENABLED", True)
    provider = build_default_provider()
    assert isinstance(provider, WebResearchProvider)
    assert provider._search_backend is None  # Tier B off, Tier A still fully functional


# --- 6. Bounded concurrency ---

class _ConcurrencyTrackingProvider(DeletionResearchProvider):
    """Records simulated work duration and the peak number of concurrent
    in-flight research() calls, to prove process_pending actually runs
    them concurrently AND respects the configured bound."""

    def __init__(self, delay=0.05):
        self.delay = delay
        self._lock = threading.Lock()
        self._in_flight = 0
        self.peak_in_flight = 0
        self.calls: list[str] = []

    def search_official_sources(self, company_name, domain):
        return []

    def inspect_privacy_page(self, url, domain):
        return None

    def extract_deletion_recipe(self, company_name, domain, pages):
        return None

    def verify_recipe(self, domain, result):
        return False

    def research(self, company_name, domain):
        with self._lock:
            self._in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
            self.calls.append(domain)
        time.sleep(self.delay)
        with self._lock:
            self._in_flight -= 1
        return None


def test_process_pending_runs_research_concurrently(db, monkeypatch):
    monkeypatch.setattr(config, "DELETION_RESEARCH_MAX_CONCURRENCY", 3)
    companies = [_company(db, f"Co {i}", f"co{i}.com") for i in range(3)]
    enqueue_pending(db, companies)
    db.commit()

    provider = _ConcurrencyTrackingProvider(delay=0.1)
    start = time.monotonic()
    processed = process_pending(db, provider, limit=3)
    elapsed = time.monotonic() - start

    assert processed == 3
    assert len(provider.calls) == 3
    # Sequential would take ~0.3s; concurrent should take closer to ~0.1s.
    assert elapsed < 0.25
    assert provider.peak_in_flight > 1  # actually ran concurrently, not one-at-a-time


def test_process_pending_concurrency_is_bounded(db, monkeypatch):
    monkeypatch.setattr(config, "DELETION_RESEARCH_MAX_CONCURRENCY", 2)
    companies = [_company(db, f"Co {i}", f"co{i}.com") for i in range(6)]
    enqueue_pending(db, companies)
    db.commit()

    provider = _ConcurrencyTrackingProvider(delay=0.05)
    processed = process_pending(db, provider, limit=6)

    assert processed == 6
    assert provider.peak_in_flight <= 2  # never exceeds the configured bound
