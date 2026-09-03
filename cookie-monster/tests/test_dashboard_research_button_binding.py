"""Regression coverage for a real dashboard bug report: a company's
"Research deletion method" button appearing to do nothing (no POST, no
visible response) while another company's identical button worked fine.

Static/dynamic inspection of dashboard.html and dashboard.js found every
Research form's action already correctly bound to its own company's ID in
every reachable deletion_status branch, and dashboard.js has no listener
that touches these forms at all (plain HTML form submits, no JS required -
see dashboard.js's own top-of-file comment). The one real, provable gap:
the METHOD_LOOKUP ("Researching...") state rendered NO control at all -
unlike every other unresolved state - so a company that transitions into
METHOD_LOOKUP between page loads (background research or a just-fired
manual click) would silently lose its button, which reads exactly like "I
clicked the button and nothing happened" to someone who still sees where
it used to be.

These tests structurally scope every assertion to ONE company's own
`.company-card` subtree (via BeautifulSoup), so a bug that makes one
card's action point at another company's ID - or drop the control
entirely without an explicit inert placeholder - fails loudly, across
several companies sharing the page and even sharing the same status.
"""
import datetime

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config
from app.db import Base
from app.deletion_constants import DeletionStatus
from app.models import Company


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


def _company(db, name, domain, deletion_status, **overrides) -> Company:
    defaults = dict(
        name=name, domain=domain, relationship_type="transactional", status="confirmed",
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        deletion_status=deletion_status,
    )
    defaults.update(overrides)
    company = Company(**defaults)
    db.add(company)
    db.commit()
    return company


def _get_dashboard_soup(client_db):
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app, base_url="http://localhost:8000")
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    return BeautifulSoup(resp.text, "html.parser")


def _card_for(soup, company_id: int):
    # Not tag-specific (the card shell is a semantic <article>) - matched
    # purely by its class/data-id, same as the browser/CSS would.
    card = soup.find(class_="company-card", attrs={"data-id": str(company_id)})
    assert card is not None, f"no company-card rendered for id={company_id}"
    return card


def test_each_research_form_submits_its_own_company_id(client_db):
    """Several companies, several different unresolved statuses, all on
    the same page - every Research/Try-again form must post to its OWN
    company's endpoint, never a sibling's."""
    a = _company(client_db, "Quest", "questdiagnostics.com", DeletionStatus.UNKNOWN)
    b = _company(client_db, "REBAG-TEST", "rebag.com", DeletionStatus.NO_METHOD_FOUND)
    c = _company(client_db, "Fresh Co", "fresh.com", DeletionStatus.NOT_STARTED)

    soup = _get_dashboard_soup(client_db)

    for company in (a, b, c):
        card = _card_for(soup, company.id)
        form = card.find("form", attrs={"action": f"/api/companies/{company.id}/deletion/research"})
        assert form is not None, f"{company.name}'s card has no research form pointed at its own id"
        assert form.get("id") == f"research-form-{company.id}"
        assert form.get("data-company-id") == str(company.id)

        button = form.find("button", attrs={"type": "submit"})
        assert button is not None
        assert not button.has_attr("disabled")

        # No OTHER company's action string should appear inside this card.
        for other in (a, b, c):
            if other.id == company.id:
                continue
            assert card.find("form", attrs={"action": f"/api/companies/{other.id}/deletion/research"}) is None


def test_multiple_companies_sharing_the_same_status_stay_distinct(client_db):
    """The exact scenario a single-company-per-status test can't catch:
    three DIFFERENT companies all in the SAME deletion_status at once."""
    companies = [
        _company(client_db, f"Company {i}", f"company{i}.com", DeletionStatus.NOT_STARTED)
        for i in range(5)
    ]

    soup = _get_dashboard_soup(client_db)

    seen_form_ids = set()
    for company in companies:
        card = _card_for(soup, company.id)
        form = card.find("form", attrs={"action": f"/api/companies/{company.id}/deletion/research"})
        assert form is not None
        assert form["id"] not in seen_form_ids  # every form id is unique across the whole page
        seen_form_ids.add(form["id"])

    # No two companies' forms collapsed onto the same HTML id (which
    # would make the browser only ever "see" one of them).
    assert len(seen_form_ids) == len(companies)


def test_method_lookup_shows_an_explicit_inert_placeholder_not_a_silent_gap(client_db):
    """A company mid-research (METHOD_LOOKUP) must never render a bare
    text line where every other unresolved state renders a button - that
    gap is exactly what makes "I clicked it and nothing happened" true
    without anything actually being broken. It must show a clearly
    disabled control instead, and never a live form that could submit."""
    company = _company(client_db, "Looking Co", "looking.com", DeletionStatus.METHOD_LOOKUP)

    soup = _get_dashboard_soup(client_db)
    card = _card_for(soup, company.id)

    assert card.find("form", attrs={"action": f"/api/companies/{company.id}/deletion/research"}) is None

    placeholder = card.find("button", string=lambda s: s and "Researching" in s)
    assert placeholder is not None
    assert placeholder.has_attr("disabled")
    assert placeholder.get("type") == "button"  # never "submit" - must be structurally unable to post


def test_no_method_found_try_again_form_is_scoped_and_enabled(client_db):
    company = _company(client_db, "REBAG-TEST", "rebag.com", DeletionStatus.NO_METHOD_FOUND)

    soup = _get_dashboard_soup(client_db)
    card = _card_for(soup, company.id)

    form = card.find("form", attrs={"id": f"research-form-{company.id}"})
    assert form is not None
    assert form["action"] == f"/api/companies/{company.id}/deletion/research"
    button = form.find("button", attrs={"type": "submit"})
    assert button is not None
    assert not button.has_attr("disabled")
    assert "Try again" in button.get_text()
