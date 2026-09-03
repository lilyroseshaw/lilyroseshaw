"""Regression coverage for the company-card restructuring: every company
is rendered as ONE self-contained <article class="company-card"> - all of
that company's controls (confirm/reject, research, check-response, delete)
must live inside its own container, never associated with a neighboring
card by mere DOM/visual proximity, and the single most useful next action
must be visually marked primary (.btn-primary) while everything else
recedes.
"""
import datetime

import pytest
from bs4 import BeautifulSoup
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config
from app.db import Base
from app.deletion_constants import DeletionMethod, DeletionStatus
from app.models import Company, OAuthToken


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


def _company(db, name, domain, **overrides) -> Company:
    defaults = dict(
        name=name, domain=domain, relationship_type="transactional", status="confirmed",
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
    )
    defaults.update(overrides)
    company = Company(**defaults)
    db.add(company)
    db.commit()
    return company


def _get_dashboard_soup(response_tracking=False):
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app, base_url="http://localhost:8000")
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    return BeautifulSoup(resp.text, "html.parser")


def _card_for(soup, company_id: int):
    card = soup.find(class_="company-card", attrs={"data-id": str(company_id)})
    assert card is not None, f"no company-card rendered for id={company_id}"
    return card


def test_pending_and_waiting_companies_dont_bleed_actions_across_cards(client_db):
    """The exact bug report scenario: an Airbnb-like company waiting for a
    response, sitting right next to another company - their actions must
    never cross into each other's card, regardless of adjacency."""
    airbnb = _company(
        client_db, "Airbnb", "airbnb.com", deletion_status=DeletionStatus.SUBMITTED,
        deletion_thread_id="thread-airbnb", deletion_method=DeletionMethod.EMAIL_REQUEST,
    )
    goop = _company(
        client_db, "Goop Kitchen", "goopkitchen.com", deletion_status=DeletionStatus.SUBMITTED,
        deletion_thread_id="thread-goop", deletion_method=DeletionMethod.EMAIL_REQUEST,
    )
    client_db.add(OAuthToken(
        gmail_address="me@gmail.com", encrypted_refresh_token="x",
        scopes_granted=" ".join(config.GMAIL_SCOPES + [config.GMAIL_READONLY_SCOPE]),
    ))
    client_db.commit()

    soup = _get_dashboard_soup()
    airbnb_card = _card_for(soup, airbnb.id)
    goop_card = _card_for(soup, goop.id)

    airbnb_check_form = airbnb_card.find("form", attrs={"action": f"/api/companies/{airbnb.id}/deletion/check-response"})
    goop_check_form = goop_card.find("form", attrs={"action": f"/api/companies/{goop.id}/deletion/check-response"})
    assert airbnb_check_form is not None
    assert goop_check_form is not None

    # Neither card contains the OTHER company's check-response form -
    # structurally impossible for one to be mistaken for belonging to the
    # neighboring company.
    assert airbnb_card.find("form", attrs={"action": f"/api/companies/{goop.id}/deletion/check-response"}) is None
    assert goop_card.find("form", attrs={"action": f"/api/companies/{airbnb.id}/deletion/check-response"}) is None

    # Each card is its own semantic, uniquely-identified container.
    assert airbnb_card.name == "article"
    assert airbnb_card["id"] == f"company-{airbnb.id}"
    assert goop_card["id"] == f"company-{goop.id}"


def test_delete_my_data_button_lives_inside_its_own_card(client_db):
    ready = _company(
        client_db, "YesStyle", "yesstyle.com", deletion_status=DeletionStatus.READY,
        deletion_method=DeletionMethod.EMAIL_REQUEST, deletion_email="privacy@yesstyle.com",
        deletion_verified=True,
    )
    other = _company(client_db, "Other Co", "otherco.com", deletion_status=DeletionStatus.NOT_STARTED)

    soup = _get_dashboard_soup()
    ready_card = _card_for(soup, ready.id)
    other_card = _card_for(soup, other.id)

    btn = ready_card.find("button", class_="delete-my-data-btn")
    assert btn is not None
    assert btn["data-id"] == str(ready.id)
    assert other_card.find("button", class_="delete-my-data-btn") is None


@pytest.mark.parametrize(
    "deletion_status,expect_primary_action_text",
    [
        (DeletionStatus.NOT_STARTED, "Find deletion method"),
        (DeletionStatus.UNKNOWN, "Search again"),
        (DeletionStatus.READY, "Delete my data"),
    ],
)
def test_primary_action_reflects_company_state(client_db, deletion_status, expect_primary_action_text):
    company = _company(
        client_db, "Widget Co", "widgetco.com", deletion_status=deletion_status,
        deletion_method=DeletionMethod.EMAIL_REQUEST, deletion_email="privacy@widgetco.com",
        deletion_verified=(deletion_status == DeletionStatus.READY),
    )
    soup = _get_dashboard_soup()
    card = _card_for(soup, company.id)
    current_state = card.find(class_="current-state")
    assert current_state is not None

    primary = current_state.find(class_="btn-primary")
    assert primary is not None, f"no primary action rendered for {deletion_status}"
    assert expect_primary_action_text in primary.get_text()


def test_completed_company_has_no_large_primary_cta(client_db):
    """A finished company shouldn't be nagging the user with another big
    button - just a satisfying completed state."""
    company = _company(
        client_db, "Done Co", "doneco.com", deletion_status=DeletionStatus.COMPLETED,
        deletion_completed_at=datetime.datetime(2024, 1, 1),
    )
    soup = _get_dashboard_soup()
    card = _card_for(soup, company.id)
    current_state = card.find(class_="current-state")
    assert current_state.find(class_="btn-primary") is None
    assert "done" in current_state.get_text().lower()


def test_status_badge_present_with_text_not_color_only(client_db):
    company = _company(client_db, "Widget Co", "widgetco.com", deletion_status=DeletionStatus.READY, deletion_verified=True)
    soup = _get_dashboard_soup()
    card = _card_for(soup, company.id)
    badge = card.find(class_="status-badge")
    assert badge is not None
    assert badge.get_text().strip()  # never color-only - always has real text
    tone_classes = [c for c in badge.get("class", []) if c.startswith("tone-")]
    assert len(tone_classes) == 1


def test_progress_stepper_marks_correct_stage(client_db):
    company = _company(client_db, "Widget Co", "widgetco.com", deletion_status=DeletionStatus.READY, deletion_verified=True)
    soup = _get_dashboard_soup()
    card = _card_for(soup, company.id)
    track = card.find(class_="progress-track")
    assert track is not None
    labels = card.find(class_="track-labels")
    assert labels is not None
    assert [s.get_text(strip=True) for s in labels.find_all("span")] == ["Found", "Confirmed", "Ready", "Requested", "Done"]
    current = labels.find(class_="is-current")
    assert current is not None
    assert "Ready" in current.get_text()


def test_pending_company_shows_stepper_at_found_stage():
    """A not-yet-reviewed company IS already at the first real stage
    ("Found") - the stepper shows that truthfully rather than implying
    nothing has happened yet."""
    from fastapi.testclient import TestClient
    from app.main import app
    import app.db as dbmod
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import tempfile

    path = tempfile.mktemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    dbmod.engine = engine
    dbmod.SessionLocal = Session
    db = Session()
    company = _company(db, "New Co", "newco.com", status="pending")
    company_id = company.id
    db.close()

    client = TestClient(app, base_url="http://localhost:8000")
    resp = client.get("/dashboard")
    soup = BeautifulSoup(resp.text, "html.parser")
    card = _card_for(soup, company_id)
    track = card.find(class_="progress-track")
    assert track is not None
    labels = card.find(class_="track-labels")
    assert labels is not None
    current = labels.find(class_="is-current")
    assert current is not None
    assert "Found" in current.get_text(strip=True)


def test_rejected_company_has_no_progress_stepper():
    """A company the USER rejected (not a company that declined the
    request - see DeletionStatus.REJECTED) is off the deletion pipeline
    entirely - the stepper would be meaningless for it."""
    from fastapi.testclient import TestClient
    from app.main import app
    import app.db as dbmod
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import tempfile

    path = tempfile.mktemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    dbmod.engine = engine
    dbmod.SessionLocal = Session
    db = Session()
    company = _company(db, "Rejected Co", "rejectedco.com", status="rejected")
    company_id = company.id
    db.close()

    client = TestClient(app, base_url="http://localhost:8000")
    resp = client.get("/dashboard")
    soup = BeautifulSoup(resp.text, "html.parser")
    card = _card_for(soup, company_id)
    assert card.find(class_="progress-track") is None


def test_dashboard_progress_summary_uses_real_counts_only(client_db):
    _company(client_db, "Ready Co", "readyco.com", deletion_status=DeletionStatus.READY, deletion_verified=True)
    _company(client_db, "Done Co", "doneco.com", deletion_status=DeletionStatus.COMPLETED)
    soup = _get_dashboard_soup()
    summary = soup.find(class_="progress-summary")
    assert summary is not None
    nums = [el.get_text() for el in summary.find_all(class_="stat-num")]
    assert "2" in nums  # total found
    assert "1" in nums  # ready + deleted, both 1 here


def test_check_response_result_uses_status_role_for_screen_readers(client_db):
    """The 'Response found...' feedback (see main.py's check-response
    route) must be announced to assistive tech, not just visually shown."""
    company = _company(
        client_db, "Goop Kitchen", "goopkitchen.com", deletion_status=DeletionStatus.SUBMITTED,
        deletion_thread_id="thread-1",
    )
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app, base_url="http://localhost:8000")
    resp = client.get(f"/dashboard?checked={company.id}&check_result=no_new_response")
    soup = BeautifulSoup(resp.text, "html.parser")
    card = _card_for(soup, company.id)
    result_el = card.find(class_="check-response-result")
    assert result_el is not None
    assert result_el.get("role") == "status"
