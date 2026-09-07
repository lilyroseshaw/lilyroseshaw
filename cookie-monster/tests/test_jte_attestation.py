"""Just the Essentials - USER COMPLETION / ATTESTATION milestone.

After Baker's Dozen hands the user to a verified privacy control
(PrivacyActionStatus.USER_ACTION_REQUIRED), the user can attest they
personally completed it - app.privacy_action.attest_user_completed. This
is USER-ATTESTED completion ONLY: real progress, but never presented as
company-confirmed, independently verified, or proof that historical data
was deleted/recalled. Fabricated companies only; no live network access
anywhere in this file.
"""
import datetime
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config
from app.case_outcome import derive_case_outcome
from app.db import Base
from app.deletion_constants import (
    CaseState,
    DeletionMethod,
    DeletionStatus,
    EventSource,
    EventType,
    NonessentialTrackingOutcome,
    OptOutOutcome,
    PrivacyActionStatus,
    PrivacyActionType,
    RecipeChoice,
)
from app.models import Company, DeletionEvent, PrivacyAction, PrivacyCase
from app.privacy_action import attest_user_completed, ensure_just_the_essentials_actions, just_the_essentials_review
from app.privacy_action_research import OPT_OUT_SCOPE_NOTE, TRACKING_CLEANUP_SCOPE_NOTE


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


def _make_user_action_required(db, action: PrivacyAction, scope_note: str, url: str = "https://fabricated-corp.com/privacy-choices") -> None:
    """Simulates a completed "Find cleanup method" lookup - direct row
    setup, since the research pipeline itself is out of scope for this
    milestone (see the task's own scope guard)."""
    action.status = PrivacyActionStatus.USER_ACTION_REQUIRED
    action.method = DeletionMethod.ACCOUNT_SETTING
    action.url = url
    action.evidence = {"source_url": url, "confidence": "high", "scope_note": scope_note}
    db.commit()


def _attest(client, company_id, action_type):
    return client.post(f"/api/companies/{company_id}/just-the-essentials/{action_type}/attest-completed")


# --- USER_ACTION_REQUIRED can be user-attested ------------------------------

def test_user_action_required_can_be_attested(db):
    company = _company(db)
    case = _case(db, company)
    actions = ensure_just_the_essentials_actions(db, case)
    tracking = next(a for a in actions if a.action_type == PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    _make_user_action_required(db, tracking, TRACKING_CLEANUP_SCOPE_NOTE)

    result = attest_user_completed(db, tracking, company)
    assert result is True

    db.refresh(tracking)
    assert tracking.status == PrivacyActionStatus.USER_COMPLETED


def test_cannot_attest_from_needs_research_or_needs_review(db):
    company = _company(db)
    case = _case(db, company)
    actions = ensure_just_the_essentials_actions(db, case)
    tracking = next(a for a in actions if a.action_type == PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    assert tracking.status == PrivacyActionStatus.NEEDS_RESEARCH

    assert attest_user_completed(db, tracking, company) is False
    db.refresh(tracking)
    assert tracking.status == PrivacyActionStatus.NEEDS_RESEARCH  # untouched

    tracking.status = PrivacyActionStatus.NEEDS_REVIEW
    db.commit()
    assert attest_user_completed(db, tracking, company) is False
    db.refresh(tracking)
    assert tracking.status == PrivacyActionStatus.NEEDS_REVIEW  # untouched


# --- attestation records USER provenance ------------------------------------

def test_attestation_records_user_provenance(db):
    company = _company(db)
    case = _case(db, company)
    actions = ensure_just_the_essentials_actions(db, case)
    opt_out = next(a for a in actions if a.action_type == PrivacyActionType.SALE_SHARING_OPT_OUT)
    _make_user_action_required(db, opt_out, OPT_OUT_SCOPE_NOTE)

    attest_user_completed(db, opt_out, company)

    db.refresh(opt_out)
    assert opt_out.evidence["attested_by"] == EventSource.USER
    assert "attested_at" in opt_out.evidence
    # The scope_note recorded when the mechanism was found survives.
    assert opt_out.evidence["scope_note"] == OPT_OUT_SCOPE_NOTE

    event = (
        db.query(DeletionEvent)
        .filter(DeletionEvent.event_type == EventType.PRIVACY_ACTION_USER_COMPLETED)
        .one()
    )
    assert event.source == EventSource.USER
    assert event.privacy_action_id == opt_out.id
    assert event.evidence["action_type"] == PrivacyActionType.SALE_SHARING_OPT_OUT


# --- duplicate attestation is idempotent ------------------------------------

def test_duplicate_attestation_is_idempotent(db):
    company = _company(db)
    case = _case(db, company)
    actions = ensure_just_the_essentials_actions(db, case)
    tracking = next(a for a in actions if a.action_type == PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    _make_user_action_required(db, tracking, TRACKING_CLEANUP_SCOPE_NOTE)

    assert attest_user_completed(db, tracking, company) is True
    db.refresh(tracking)
    first_attested_at = tracking.evidence["attested_at"]

    assert attest_user_completed(db, tracking, company) is True  # second call, same row
    db.refresh(tracking)
    assert tracking.evidence["attested_at"] == first_attested_at  # not re-written

    assert db.query(DeletionEvent).filter(
        DeletionEvent.event_type == EventType.PRIVACY_ACTION_USER_COMPLETED
    ).count() == 1  # never a duplicate audit entry


def test_duplicate_attestation_via_api_is_idempotent(client_db, client):
    company = _company(client_db)
    _case(client_db, company)
    client.post(f"/api/companies/{company.id}/privacy-case/recipe", data={"recipe": "JUST_THE_ESSENTIALS"})
    client_db.expire_all()
    case = client_db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
    actions = client_db.query(PrivacyAction).filter(PrivacyCase.company_id == company.id).all()
    tracking = client_db.query(PrivacyAction).filter(
        PrivacyAction.privacy_case_id == case.id,
        PrivacyAction.action_type == PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP,
    ).one()
    _make_user_action_required(client_db, tracking, TRACKING_CLEANUP_SCOPE_NOTE)

    resp1 = _attest(client, company.id, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    resp2 = _attest(client, company.id, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert client_db.query(DeletionEvent).filter(
        DeletionEvent.event_type == EventType.PRIVACY_ACTION_USER_COMPLETED
    ).count() == 1


# --- user attestation never becomes company/system CONFIRMED ---------------

def test_user_attestation_never_becomes_confirmed(db):
    company = _company(db)
    case = _case(db, company)
    actions = ensure_just_the_essentials_actions(db, case)
    tracking = next(a for a in actions if a.action_type == PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    _make_user_action_required(db, tracking, TRACKING_CLEANUP_SCOPE_NOTE)

    attest_user_completed(db, tracking, company)
    db.refresh(tracking)
    assert tracking.status != PrivacyActionStatus.CONFIRMED
    assert tracking.status == PrivacyActionStatus.USER_COMPLETED

    review = just_the_essentials_review(company, [tracking])
    assert review[0]["status_label"] == "Completed by you"
    assert review[0]["status_label"] != "Confirmed"


# --- no Gmail/send/research/deletion/follow-up side effect ------------------

def test_attestation_triggers_no_external_side_effects(client_db, client):
    company = _company(client_db, deletion_status=DeletionStatus.READY, deletion_method=DeletionMethod.WEB_FORM)
    _case(client_db, company)
    client.post(f"/api/companies/{company.id}/privacy-case/recipe", data={"recipe": "JUST_THE_ESSENTIALS"})
    client_db.expire_all()
    case = client_db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
    tracking = client_db.query(PrivacyAction).filter(
        PrivacyAction.privacy_case_id == case.id,
        PrivacyAction.action_type == PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP,
    ).one()
    _make_user_action_required(client_db, tracking, TRACKING_CLEANUP_SCOPE_NOTE)

    with patch("app.google_oauth.send_email") as mock_send:
        resp = _attest(client, company.id, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    assert resp.status_code == 200
    mock_send.assert_not_called()

    client_db.expire_all()
    fetched = client_db.query(Company).filter(Company.id == company.id).one()
    assert fetched.deletion_status == DeletionStatus.READY  # Full Clean's own fields untouched
    assert fetched.deletion_requested_at is None
    assert fetched.deletion_thread_id is None
    assert fetched.waiting_on is None
    assert client_db.query(DeletionEvent).filter(DeletionEvent.event_type == EventType.EMAIL_SENT).count() == 0
    assert client_db.query(DeletionEvent).filter(DeletionEvent.event_type == EventType.EXECUTION_STARTED).count() == 0
    assert client_db.query(DeletionEvent).filter(
        DeletionEvent.event_type == EventType.PRIVACY_ACTION_METHOD_FOUND
    ).count() == 0  # attesting never triggers a research lookup either

    # The sibling action is completely untouched by attesting the other.
    opt_out = client_db.query(PrivacyAction).filter(
        PrivacyAction.privacy_case_id == case.id,
        PrivacyAction.action_type == PrivacyActionType.SALE_SHARING_OPT_OUT,
    ).one()
    assert opt_out.status == PrivacyActionStatus.NEEDS_RESEARCH


# --- truthful limitations survive attestation -------------------------------

def test_tracking_action_retains_historical_deletion_limitation_after_attestation(db):
    company = _company(db)
    case = _case(db, company)
    actions = ensure_just_the_essentials_actions(db, case)
    tracking = next(a for a in actions if a.action_type == PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    _make_user_action_required(db, tracking, TRACKING_CLEANUP_SCOPE_NOTE)
    attest_user_completed(db, tracking, company)
    db.refresh(tracking)

    entry = just_the_essentials_review(company, [tracking])[0]
    assert entry["scope_note"] == TRACKING_CLEANUP_SCOPE_NOTE
    assert "does not" in entry["scope_note"].lower()
    assert "deleted" in entry["scope_note"].lower()
    assert "Verified" not in entry["status_label"]
    assert "Company confirmed" not in (entry["status_label"] or "")


def test_sale_share_action_retains_recall_deletion_limitation_after_attestation(db):
    company = _company(db)
    case = _case(db, company)
    actions = ensure_just_the_essentials_actions(db, case)
    opt_out = next(a for a in actions if a.action_type == PrivacyActionType.SALE_SHARING_OPT_OUT)
    _make_user_action_required(db, opt_out, OPT_OUT_SCOPE_NOTE)
    attest_user_completed(db, opt_out, company)
    db.refresh(opt_out)

    entry = just_the_essentials_review(company, [opt_out])[0]
    assert entry["scope_note"] == OPT_OUT_SCOPE_NOTE
    assert "recalled" in entry["scope_note"].lower() or "deleted" in entry["scope_note"].lower()


# --- mixed JTE states render correctly --------------------------------------

def test_mixed_state_one_completed_one_still_actionable(db):
    company = _company(db)
    case = _case(db, company)
    actions = ensure_just_the_essentials_actions(db, case)
    tracking = next(a for a in actions if a.action_type == PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    opt_out = next(a for a in actions if a.action_type == PrivacyActionType.SALE_SHARING_OPT_OUT)
    _make_user_action_required(db, tracking, TRACKING_CLEANUP_SCOPE_NOTE)
    _make_user_action_required(db, opt_out, OPT_OUT_SCOPE_NOTE)
    attest_user_completed(db, tracking, company)
    db.refresh(tracking)

    review = just_the_essentials_review(company, [tracking, opt_out])
    tracking_entry = next(e for e in review if e["action_type"] == PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    opt_out_entry = next(e for e in review if e["action_type"] == PrivacyActionType.SALE_SHARING_OPT_OUT)
    assert tracking_entry["status_label"] == "Completed by you"
    assert tracking_entry["can_attest"] is False
    assert opt_out_entry["status_label"] == "Ready for you"
    assert opt_out_entry["can_attest"] is True

    outcome = derive_case_outcome(company, case, actions=[tracking, opt_out])
    assert outcome.overall == CaseState.NEEDS_USER  # opt_out still needs the user
    assert outcome.nonessential_tracking == NonessentialTrackingOutcome.USER_COMPLETED
    assert outcome.opt_out == OptOutOutcome.UNKNOWN


def test_mixed_state_one_completed_one_needs_review(db):
    company = _company(db)
    case = _case(db, company)
    actions = ensure_just_the_essentials_actions(db, case)
    tracking = next(a for a in actions if a.action_type == PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    opt_out = next(a for a in actions if a.action_type == PrivacyActionType.SALE_SHARING_OPT_OUT)
    _make_user_action_required(db, tracking, TRACKING_CLEANUP_SCOPE_NOTE)
    attest_user_completed(db, tracking, company)
    db.refresh(tracking)
    opt_out.status = PrivacyActionStatus.NEEDS_REVIEW
    db.commit()

    outcome = derive_case_outcome(company, case, actions=[tracking, opt_out])
    assert outcome.overall != CaseState.RESOLVED
    assert outcome.overall != CaseState.USER_RESOLVED  # opt_out never reached a positive outcome
    assert outcome.nonessential_tracking == NonessentialTrackingOutcome.USER_COMPLETED
    assert outcome.opt_out == OptOutOutcome.UNKNOWN


# --- both user-completed can resolve the user-action portion, without
# claiming Full Clean/data-deletion confirmation -----------------------------

def test_both_actions_user_completed_resolves_user_action_perspective_only(db):
    company = _company(db, deletion_status=DeletionStatus.NOT_STARTED)
    case = _case(db, company)
    actions = ensure_just_the_essentials_actions(db, case)
    tracking = next(a for a in actions if a.action_type == PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    opt_out = next(a for a in actions if a.action_type == PrivacyActionType.SALE_SHARING_OPT_OUT)
    _make_user_action_required(db, tracking, TRACKING_CLEANUP_SCOPE_NOTE)
    _make_user_action_required(db, opt_out, OPT_OUT_SCOPE_NOTE)
    attest_user_completed(db, tracking, company)
    attest_user_completed(db, opt_out, company)
    db.refresh(tracking)
    db.refresh(opt_out)

    outcome = derive_case_outcome(company, case, actions=[tracking, opt_out])
    assert outcome.overall == CaseState.USER_RESOLVED
    assert outcome.overall != CaseState.RESOLVED  # never the company/system-evidence RESOLVED
    assert outcome.nonessential_tracking == NonessentialTrackingOutcome.USER_COMPLETED
    assert outcome.opt_out == OptOutOutcome.USER_COMPLETED
    # Never promoted into a Full Clean / broad personal-data deletion claim.
    assert outcome.personal_data != "DELETION_CONFIRMED"
    assert outcome.account != "CLOSED"
    assert outcome.is_pantry is False


# --- cannot attest an action for a non-JTE recipe ---------------------------

@pytest.mark.parametrize("other_recipe", [None, RecipeChoice.FULL_CLEAN, RecipeChoice.LEAVE_IT_BE])
def test_cannot_attest_for_non_jte_recipe(client_db, client, other_recipe):
    company = _company(client_db)
    if other_recipe is not None:
        _case(client_db, company, other_recipe)
    resp = _attest(client, company.id, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    assert resp.status_code == 400


def test_cannot_attest_unknown_action_type(client_db, client):
    company = _company(client_db)
    _case(client_db, company)
    resp = _attest(client, company.id, "NOT_A_REAL_ACTION_TYPE")
    assert resp.status_code == 404


def test_cannot_attest_before_a_verified_mechanism_exists(client_db, client):
    """The action row exists (JTE materializes both on selection) but is
    still NEEDS_RESEARCH - attestation must fail closed, never silently
    succeed on a non-existent mechanism."""
    company = _company(client_db)
    _case(client_db, company)
    client.post(f"/api/companies/{company.id}/privacy-case/recipe", data={"recipe": "JUST_THE_ESSENTIALS"})
    resp = _attest(client, company.id, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    assert resp.status_code == 400


# --- Pantry unchanged --------------------------------------------------------

def test_pantry_company_cannot_attest(client_db, client):
    company = _company(client_db)
    client.post(f"/api/companies/{company.id}/privacy-case/recipe", data={"recipe": "LEAVE_IT_BE"})
    resp = _attest(client, company.id, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    assert resp.status_code == 400

    dash = client.get("/dashboard")
    soup = BeautifulSoup(dash.text, "html.parser")
    pantry_section = soup.find(class_="pantry-section")
    assert pantry_section is not None
    assert pantry_section.find(id=f"company-{company.id}") is not None
    assert "I did this" not in pantry_section.get_text()


# --- Full Clean unchanged ----------------------------------------------------

def test_full_clean_unaffected_by_attestation_feature(client_db, client):
    company = _company(client_db)
    resp = client.post(f"/api/companies/{company.id}/privacy-case/recipe", data={"recipe": "FULL_CLEAN"})
    assert resp.status_code == 200

    preview = client.get(f"/api/companies/{company.id}/deletion/preview")
    assert preview.status_code == 200
    assert "capability" in preview.json()

    attest_resp = _attest(client, company.id, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP)
    assert attest_resp.status_code == 400

    client_db.expire_all()
    case = client_db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
    assert client_db.query(PrivacyAction).filter(PrivacyAction.privacy_case_id == case.id).count() == 0
