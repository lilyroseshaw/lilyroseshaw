"""Tests for the "Find cleanup method" orchestration layer
(app/privacy_action_resolver.py) and its API route/UI wiring - each
PrivacyAction resolves fully independently, a Brave-budget deferral never
counts as a failed lookup, opening a verified page is never submission/
confirmation, and no internal vocabulary (NEEDS_RESEARCH,
USER_ACTION_REQUIRED, PrivacyAction, resolver, Tier A/Tier B) ever reaches
user-facing copy. Fabricated companies only - no live network access
anywhere in this file (every research call goes through a fake/mock
provider or httpx.MockTransport).
"""
import datetime
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config
from app.db import Base
from app.deletion_constants import (
    DeletionMethod,
    DeletionStatus,
    EventType,
    PrivacyActionStatus,
    PrivacyActionType,
    RecipeChoice,
)
from app.models import Company, DeletionEvent, PrivacyAction, PrivacyCase
from app.privacy_action import ensure_just_the_essentials_actions, just_the_essentials_review
from app.privacy_action_research import PrivacyActionResearchProvider
from app.privacy_action_resolver import resolve_privacy_action
from app.research_search import BraveBudgetExhausted
from app.research_types import PrivacyActionResult


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


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


@pytest.fixture()
def client():
    from app.main import app
    return TestClient(app, base_url="http://localhost:8000")


def _company(db, name="Fabricated Co", domain="fabricated-corp.com", **overrides) -> Company:
    defaults = dict(
        name=name, domain=domain, relationship_type="transactional", status="confirmed",
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        deletion_method=DeletionMethod.EMAIL_REQUEST, deletion_status=DeletionStatus.READY,
        deletion_email="privacy@" + domain, deletion_verified=True,
    )
    defaults.update(overrides)
    company = Company(**defaults)
    db.add(company)
    db.commit()
    return company


def _case(db, company, selected_recipe=RecipeChoice.JUST_THE_ESSENTIALS) -> PrivacyCase:
    case = PrivacyCase(company_id=company.id, selected_recipe=selected_recipe, recipe_selected_at=None)
    db.add(case)
    db.commit()
    return case


class _FakeProvider(PrivacyActionResearchProvider):
    """Returns whatever's queued for (domain, action_type), or raises
    whatever's queued as an exception - lets tests drive
    resolve_privacy_action's every branch without any real network access."""

    def __init__(self):
        self.results: dict[tuple[str, str], object] = {}
        self.calls: list[tuple[str, str]] = []

    def queue_result(self, domain, action_type, value):
        self.results[(domain, action_type)] = value

    def research(self, domain, action_type):
        self.calls.append((domain, action_type))
        value = self.results.get((domain, action_type))
        if isinstance(value, Exception):
            raise value
        return value


def _verified_result(domain, action_type, method=DeletionMethod.WEB_FORM, url=None) -> PrivacyActionResult:
    url = url or f"https://{domain}/privacy-choices"
    return PrivacyActionResult(
        domain=domain, action_type=action_type, method=method, url=url,
        source_url=url, confidence="high", scope_note="Truthful scope note.", verified=True,
        reasons=["test fixture"],
    )


# --- a verified mechanism moves an action to USER_ACTION_REQUIRED, never further ---

def test_verified_mechanism_moves_action_to_user_action_required(db):
    company = _company(db)
    case = _case(db, company)
    actions = ensure_just_the_essentials_actions(db, case)
    tracking = next(a for a in actions if a.action_type == PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)

    provider = _FakeProvider()
    provider.queue_result(
        company.domain, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP,
        _verified_result(company.domain, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP, method=DeletionMethod.ACCOUNT_SETTING),
    )

    ran = resolve_privacy_action(db, tracking, company, provider)
    assert ran is True

    db.refresh(tracking)
    assert tracking.status == PrivacyActionStatus.USER_ACTION_REQUIRED
    assert tracking.method == DeletionMethod.ACCOUNT_SETTING
    assert tracking.url == f"https://{company.domain}/privacy-choices"

    event = (
        db.query(DeletionEvent)
        .filter(DeletionEvent.event_type == EventType.PRIVACY_ACTION_METHOD_FOUND)
        .one()
    )
    assert event.evidence["action_type"] == PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP


# --- no mechanism -> safe Needs Review, never fabricated --------------------

def test_no_mechanism_found_moves_action_to_needs_review_not_a_failure(db):
    company = _company(db)
    case = _case(db, company)
    actions = ensure_just_the_essentials_actions(db, case)
    opt_out = next(a for a in actions if a.action_type == PrivacyActionType.SALE_SHARING_OPT_OUT)

    provider = _FakeProvider()
    provider.queue_result(company.domain, PrivacyActionType.SALE_SHARING_OPT_OUT, None)

    resolve_privacy_action(db, opt_out, company, provider)

    db.refresh(opt_out)
    assert opt_out.status == PrivacyActionStatus.NEEDS_REVIEW
    assert opt_out.url is None  # never fabricated
    assert opt_out.method == "UNKNOWN"

    event = (
        db.query(DeletionEvent)
        .filter(DeletionEvent.event_type == EventType.PRIVACY_ACTION_NEEDS_REVIEW)
        .one()
    )
    assert event.evidence["reason"] == "no_official_mechanism_found"


# --- each PrivacyAction resolves independently; mixed outcome --------------

def test_each_privacy_action_resolves_independently_mixed_outcome(db):
    company = _company(db)
    case = _case(db, company)
    actions = ensure_just_the_essentials_actions(db, case)
    tracking = next(a for a in actions if a.action_type == PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    opt_out = next(a for a in actions if a.action_type == PrivacyActionType.SALE_SHARING_OPT_OUT)

    provider = _FakeProvider()
    provider.queue_result(
        company.domain, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP,
        _verified_result(company.domain, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP),
    )
    provider.queue_result(company.domain, PrivacyActionType.SALE_SHARING_OPT_OUT, None)

    resolve_privacy_action(db, tracking, company, provider)
    resolve_privacy_action(db, opt_out, company, provider)

    db.refresh(tracking)
    db.refresh(opt_out)
    assert tracking.status == PrivacyActionStatus.USER_ACTION_REQUIRED  # actionable
    assert opt_out.status == PrivacyActionStatus.NEEDS_REVIEW  # honestly unresolved

    # Resolving one never touched the other's row at all.
    assert opt_out.url is None
    assert opt_out.method == "UNKNOWN"


# --- a Brave-budget deferral is never counted as a failed lookup -----------

def test_budget_exhaustion_leaves_action_untouched(db):
    company = _company(db)
    case = _case(db, company)
    actions = ensure_just_the_essentials_actions(db, case)
    tracking = next(a for a in actions if a.action_type == PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)

    provider = _FakeProvider()
    provider.queue_result(company.domain, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP, BraveBudgetExhausted())

    ran = resolve_privacy_action(db, tracking, company, provider)
    assert ran is True

    db.refresh(tracking)
    assert tracking.status == PrivacyActionStatus.NEEDS_RESEARCH  # completely untouched, never NEEDS_REVIEW

    event = (
        db.query(DeletionEvent)
        .filter(DeletionEvent.event_type == EventType.RESEARCH_DEFERRED)
        .one()
    )
    assert event.evidence["reason"] == "brave_budget_exhausted"
    assert db.query(DeletionEvent).filter(
        DeletionEvent.event_type == EventType.PRIVACY_ACTION_NEEDS_REVIEW
    ).count() == 0


# --- double-click in-flight guard -------------------------------------------

def test_double_resolve_call_is_a_no_op_while_in_flight(db, monkeypatch):
    company = _company(db)
    case = _case(db, company)
    actions = ensure_just_the_essentials_actions(db, case)
    tracking = next(a for a in actions if a.action_type == PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)

    from app import privacy_action_resolver

    monkeypatch.setitem(privacy_action_resolver.__dict__, "_in_flight_ids", {tracking.id})
    provider = _FakeProvider()
    ran = resolve_privacy_action(db, tracking, company, provider)
    assert ran is False
    assert provider.calls == []  # never even called the provider


# --- opening a verified page is never submitted/confirmed -------------------

def test_opening_a_verified_page_is_never_submitted_or_confirmed(client_db, client):
    company = _company(client_db)
    _case(client_db, company)
    client.post(f"/api/companies/{company.id}/privacy-case/recipe", data={"recipe": "JUST_THE_ESSENTIALS"})

    with patch("app.main._privacy_action_provider") as mock_provider:
        mock_provider.research.return_value = _verified_result(company.domain, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
        resp = client.post(
            f"/api/companies/{company.id}/just-the-essentials/"
            f"{PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP}/research"
        )
    assert resp.status_code == 200
    entry = next(a for a in resp.json()["actions"] if a["action_type"] == PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    assert entry["status"] == PrivacyActionStatus.USER_ACTION_REQUIRED
    assert entry["cta_url"]

    # Merely fetching the preview again ("opening"/viewing) repeatedly must
    # never advance the status toward SUBMITTED/CONFIRMED - there is no
    # execution route in this milestone that could do so, and the read-only
    # preview must stay read-only.
    for _ in range(3):
        preview = client.get(f"/api/companies/{company.id}/just-the-essentials/preview")
        entry = next(a for a in preview.json()["actions"] if a["action_type"] == PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
        assert entry["status"] == PrivacyActionStatus.USER_ACTION_REQUIRED
        assert entry["status"] not in (PrivacyActionStatus.SUBMITTED, PrivacyActionStatus.CONFIRMED)


# --- the research route is gated exactly like the preview route ------------

def test_research_route_gated_without_just_the_essentials_selected(client_db, client):
    company = _company(client_db)
    resp = client.post(
        f"/api/companies/{company.id}/just-the-essentials/"
        f"{PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP}/research"
    )
    assert resp.status_code == 400


def test_research_route_404s_for_unknown_action_type(client_db, client):
    company = _company(client_db)
    _case(client_db, company)
    client.post(f"/api/companies/{company.id}/privacy-case/recipe", data={"recipe": "JUST_THE_ESSENTIALS"})
    resp = client.post(f"/api/companies/{company.id}/just-the-essentials/NOT_A_REAL_TYPE/research")
    assert resp.status_code == 404


def test_research_route_never_sends_gmail(client_db, client):
    """Full Clean's own gate: nothing about researching a Just the
    Essentials mechanism should ever touch google_oauth.send_email."""
    company = _company(client_db)
    _case(client_db, company)
    client.post(f"/api/companies/{company.id}/privacy-case/recipe", data={"recipe": "JUST_THE_ESSENTIALS"})

    with patch("app.google_oauth.send_email") as mock_send:
        client.post(
            f"/api/companies/{company.id}/just-the-essentials/"
            f"{PrivacyActionType.SALE_SHARING_OPT_OUT}/research"
        )
    mock_send.assert_not_called()


# --- Full Clean unchanged ----------------------------------------------------

def test_full_clean_unaffected_by_privacy_action_research_wiring(client_db, client):
    company = _company(client_db)
    resp = client.post(f"/api/companies/{company.id}/privacy-case/recipe", data={"recipe": "FULL_CLEAN"})
    assert resp.status_code == 200
    preview = client.get(f"/api/companies/{company.id}/deletion/preview")
    assert preview.status_code == 200
    assert "capability" in preview.json()
    # Full Clean never gets PrivacyAction rows, research route or not.
    client_db.expire_all()
    case = client_db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
    assert client_db.query(PrivacyAction).filter(PrivacyAction.privacy_case_id == case.id).count() == 0


def test_build_default_provider_still_works_with_no_arguments():
    """The Full Clean provider factory's signature gained an optional
    search_backend kwarg (see main.py's shared-Brave-budget wiring) - every
    existing call site (build_default_provider() with no arguments) must
    behave exactly as before."""
    from app.deletion_research import NullResearchProvider, WebResearchProvider, build_default_provider

    provider = build_default_provider()
    assert isinstance(provider, (NullResearchProvider, WebResearchProvider))


# --- no internal vocabulary reaches user-facing copy ------------------------

_FORBIDDEN_JARGON = [
    "NEEDS_RESEARCH", "USER_ACTION_REQUIRED", "NEEDS_REVIEW", "PrivacyAction",
    "resolver", "Tier A", "Tier B",
]


def test_review_payload_display_fields_contain_no_internal_jargon(db):
    company = _company(db)
    case = _case(db, company)
    actions = ensure_just_the_essentials_actions(db, case)

    provider = _FakeProvider()
    tracking = next(a for a in actions if a.action_type == PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    provider.queue_result(
        company.domain, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP,
        _verified_result(company.domain, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP),
    )
    resolve_privacy_action(db, tracking, company, provider)
    db.refresh(tracking)

    opt_out = next(a for a in actions if a.action_type == PrivacyActionType.SALE_SHARING_OPT_OUT)
    provider.queue_result(company.domain, PrivacyActionType.SALE_SHARING_OPT_OUT, None)
    resolve_privacy_action(db, opt_out, company, provider)
    db.refresh(opt_out)

    review = just_the_essentials_review(company, [tracking, opt_out])
    for entry in review:
        for field in ("status_label", "explanation", "cta_label", "scope_note", "label", "summary"):
            value = entry.get(field)
            if not value:
                continue
            for jargon in _FORBIDDEN_JARGON:
                assert jargon not in value, f"{jargon!r} leaked into display field {field!r}: {value!r}"


def test_dashboard_html_never_shows_needs_research_developer_copy(client_db, client):
    """The old 'Needs research' developer-facing label must be gone from
    the rendered page - replaced by the 'Find cleanup method' CTA."""
    company = _company(client_db)
    client.post(f"/api/companies/{company.id}/privacy-case/recipe", data={"recipe": "JUST_THE_ESSENTIALS"})

    resp = client.get("/dashboard")
    assert "Needs research" not in resp.text
    for jargon in _FORBIDDEN_JARGON:
        assert jargon not in resp.text
