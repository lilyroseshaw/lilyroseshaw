"""Tests for the manual "Research deletion method" button's cooldown-bypass
and concurrency guard: a background (unattended) research attempt must
still respect the retry cooldown, but an explicit user click must bypass
it and run immediately - while never starting two overlapping research
jobs for the same domain (a double-click, or a manual click racing the
background worker's own tick), and never letting a manual click weaken
the Brave daily budget enforcement or the counted-vs-deferred attempt
semantics.
"""
import datetime
import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config
from app.db import Base
from app.deletion_constants import DeletionStatus, EventType, RecipeStatus, ResearchFailureReason
from app.deletion_research import DeletionResearchProvider
from app.deletion_resolver import get_or_create_recipe_stub, process_pending, resolve_deletion_method
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
        deletion_status=DeletionStatus.UNKNOWN,
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


def _recipe_in_cooldown(db, domain, research_attempts=1) -> DeletionRecipe:
    """A recipe that failed recently enough to still be inside
    DELETION_RECIPE_RETRY_COOLDOWN_DAYS - the exact state that must block
    an unattended background retry but never a manual one."""
    recipe = get_or_create_recipe_stub(db, domain)
    recipe.status = RecipeStatus.NEEDS_RESEARCH
    recipe.research_attempts = research_attempts
    recipe.last_attempted_at = datetime.datetime.utcnow() - datetime.timedelta(hours=1)
    db.commit()
    return recipe


class FakeProvider(DeletionResearchProvider):
    """Same minimal stand-in used elsewhere in this suite - research() is
    overridden directly so the abstract four-step methods are never
    exercised."""

    def __init__(self, results: dict | None = None):
        self.results = results or {}
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
        return self.results.get(domain)


# --- 1. Background attempt during cooldown is deferred ---

def test_background_attempt_during_cooldown_is_skipped(db):
    domain = "cooldown.com"
    company = _company(db, domain=domain)
    recipe = _recipe_in_cooldown(db, domain)
    attempted_before = recipe.research_attempts
    last_attempted_before = recipe.last_attempted_at

    provider = FakeProvider({domain: _verified_result(domain)})
    processed = process_pending(db, provider, limit=10)

    assert processed == 0
    assert provider.calls == []  # never even called - the cooldown check happens before research runs
    db.refresh(recipe)
    db.refresh(company)
    assert recipe.research_attempts == attempted_before
    assert recipe.last_attempted_at == last_attempted_before
    assert company.deletion_status == DeletionStatus.UNKNOWN  # untouched, never flipped to METHOD_LOOKUP


# --- 2. Manual (force=True) attempt during cooldown runs anyway ---

def test_manual_attempt_during_cooldown_runs_immediately(db):
    domain = "cooldown.com"
    company = _company(db, domain=domain)
    recipe = _recipe_in_cooldown(db, domain)

    provider = FakeProvider({domain: _verified_result(domain)})
    changed = resolve_deletion_method(db, company, provider, force=True)

    assert changed is True
    assert provider.calls == [domain]  # the explicit click actually triggered a fresh attempt
    db.refresh(recipe)
    db.refresh(company)
    assert recipe.research_attempts == 2  # counted as a real, fresh attempt
    assert company.deletion_status == DeletionStatus.READY


# --- 3. Manual attempt still respects the Brave daily budget ---

def test_manual_attempt_respects_exhausted_budget(db):
    """A manual click bypasses the retry *cooldown*, never the Brave
    daily query budget - if today's budget is already exhausted, the
    manual attempt must be deferred exactly like a background one: no
    counted attempt, no status change, a RESEARCH_DEFERRED event instead
    of RESEARCH_FAILED."""
    domain = "budget.com"
    company = _company(db, domain=domain)
    recipe = get_or_create_recipe_stub(db, domain)
    recipe.status = RecipeStatus.NEEDS_RESEARCH
    recipe.research_attempts = 1
    db.commit()

    class BudgetExhaustedProvider(DeletionResearchProvider):
        def search_official_sources(self, company_name, domain):
            return []

        def inspect_privacy_page(self, url, domain):
            return None

        def extract_deletion_recipe(self, company_name, domain, pages):
            return None

        def verify_recipe(self, domain, result):
            return False

        def research(self, company_name, domain):
            from app.research_search import BraveBudgetExhausted
            raise BraveBudgetExhausted()

    changed = resolve_deletion_method(db, company, BudgetExhaustedProvider(), force=True)

    assert changed is True  # a deferral is still a "something happened" signal to the caller...
    db.refresh(recipe)
    db.refresh(company)
    assert recipe.research_attempts == 1  # ...but never a counted attempt
    assert company.deletion_status == DeletionStatus.UNKNOWN  # restored to exactly what it was

    event = (
        db.query(DeletionEvent)
        .filter(DeletionEvent.company_id == company.id, DeletionEvent.event_type == EventType.RESEARCH_DEFERRED)
        .one()
    )
    assert event.evidence["reason"] == ResearchFailureReason.BUDGET_EXHAUSTED
    assert db.query(DeletionEvent).filter(
        DeletionEvent.company_id == company.id, DeletionEvent.event_type == EventType.RESEARCH_FAILED
    ).count() == 0


# --- 4. Duplicate/concurrent manual requests never start two jobs ---

class _SlowProvider(DeletionResearchProvider):
    """Blocks inside research() until released, so a test can deterministically
    land a second manual request while the first is still in flight."""

    def __init__(self, result, started_event: threading.Event, release_event: threading.Event):
        self.result = result
        self.started_event = started_event
        self.release_event = release_event
        self.call_count = 0
        self._lock = threading.Lock()

    def search_official_sources(self, company_name, domain):
        return []

    def inspect_privacy_page(self, url, domain):
        return None

    def extract_deletion_recipe(self, company_name, domain, pages):
        return None

    def verify_recipe(self, domain, result):
        return result.verified

    def research(self, company_name, domain):
        with self._lock:
            self.call_count += 1
        self.started_event.set()
        self.release_event.wait(timeout=5)
        return self.result


def test_concurrent_manual_clicks_never_start_two_jobs(tmp_path):
    """Two overlapping manual 'Research deletion method' requests for the
    SAME company (a double-click, or a click landing while the background
    worker's own tick is already researching this domain) must result in
    exactly one research job actually running - the second must be a
    same-outcome no-op, not a duplicate/racing attempt."""
    domain = "doubleclick.com"
    engine = create_engine(f"sqlite:///{tmp_path/'test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    setup_db = Session()
    company = _company(setup_db, domain=domain)
    company_id = company.id
    setup_db.close()

    started_event = threading.Event()
    release_event = threading.Event()
    provider = _SlowProvider(_verified_result(domain), started_event, release_event)

    results = {}

    def run_first():
        db_a = Session()
        company_a = db_a.get(Company, company_id)
        results["first"] = resolve_deletion_method(db_a, company_a, provider, force=True)
        db_a.close()

    thread_a = threading.Thread(target=run_first)
    thread_a.start()
    assert started_event.wait(timeout=5)  # first call has claimed the domain and is now "in flight"

    db_b = Session()
    company_b = db_b.get(Company, company_id)
    results["second"] = resolve_deletion_method(db_b, company_b, provider, force=True)
    db_b.close()

    release_event.set()
    thread_a.join(timeout=5)

    assert provider.call_count == 1  # only one research job ever actually ran
    assert results["first"] is True  # the in-flight one completed normally
    assert results["second"] is False  # the overlapping one was a no-op, not a duplicate attempt

    verify_db = Session()
    recipe = verify_db.query(DeletionRecipe).filter(DeletionRecipe.domain == domain).one()
    assert recipe.research_attempts == 1  # never double-counted
    verify_db.close()
