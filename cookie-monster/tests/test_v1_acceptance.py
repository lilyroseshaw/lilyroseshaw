"""V1 ACCEPTANCE PASS - Baker's Dozen.

End-to-end verification that the existing V1 workflow (discover -> choose
Cleanup Recipe -> explain -> confirm -> execute/hand off -> interpret
replies -> chase unresolved requests -> stop on evidence-backed outcome ->
project into Working/Needs You/Done/Pantry) is coherent across a small set
of representative company shapes.

This file is READ-ONLY VALIDATION over the existing engine - every
assertion here exercises production code that already exists
(check_company_response, chase_engine, derive_case_outcome,
derive_top_level_state, the recipe-selection route) rather than
reimplementing any of it. It does not replace the more exhaustive
per-module test suites (test_response_classify.py, test_case_outcome.py,
test_top_level_state.py, test_pantry.py, test_privacy_action_research.py,
...) - it exists to prove those pieces cohere into one truthful workflow
end to end, using a handful of representative shapes.

ACCEPTANCE MATRIX (see each test below for full detail):

  Scenario                     Recipe   Status/Outcome            Next actor   Top-level  Chase                          Truth claim
  ----------------------------  -------  ------------------------  -----------  ---------  -----------------------------  --------------------------------
  1a Full Clean success (Goop)  FULL_C.  ACCOUNT_RECORD_DELETED..  COMPANY      WORKING    active (follow-up still due)  "Request received; waiting on company"
  1b   ... after broad reply    FULL_C.  COMPLETED                 none         DONE       stopped, none due             "Deletion confirmed"
  2  Full Clean ack (MALK)      FULL_C.  IN_PROGRESS               COMPANY      WORKING    active                        "Request received; waiting on company"
  3  Full Clean verification    FULL_C.  VERIFICATION_NEEDED       USER         NEEDS_YOU  none while user owed action   "Needs your action"
  4  JTE mixed                  JTE      one USER_COMPLETED/       Baker's      WORKING    n/a (no chase for JTE) -       "Completed by you" + "needs review"
                                          one NEEDS_REVIEW          Dozen                   see test_4's own docstring
                                                                                             for why this is WORKING,
                                                                                             not the brief's NEEDS_YOU
  5  JTE user-resolved          JTE      USER_RESOLVED             none         DONE       n/a                           "Completed by you" (both)
  6  Pantry                     LEAVE_.  (whatever evidence says)  n/a          PANTRY     none, ever                    "Kept in Pantry"
  7  Rejection                  FULL_C.  REJECTED                  ESCALATION   NEEDS_YOU  no ordinary chase             "Needs your action" / unresolved
  8  Unknown                    FULL_C.  UNKNOWN_RESPONSE          varies       WORKING/   matches waiting_on             "Request received; waiting on company"
                                                                                NEEDS_YOU                                  or "Needs your action"

Fabricated companies for every generic test; MALK/Goop appear only as
regression fixtures reproducing their real historical/live shape, never
as production branches.
"""
import base64
import datetime
from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import chase_engine, config
from app.case_outcome import CaseOutcome, derive_case_outcome
from app.db import Base
from app.deletion_constants import (
    CaseState,
    DeletionMethod,
    DeletionStatus,
    PrivacyActionStatus,
    PrivacyActionType,
    RecipeChoice,
    WaitingOn,
)
from app.deletion_response_tracker import CHECK_RESULT_NEW_MESSAGE, check_company_response
from app.models import Company, PrivacyAction, PrivacyCase
from app.privacy_action import just_the_essentials_dashboard_summary
from app.response_classify import ResponseClassifier
from app.top_level_state import TopLevelState, derive_top_level_state

# --- fixtures --------------------------------------------------------------


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


def _select_recipe(client, company_id, recipe):
    return client.post(f"/api/companies/{company_id}/privacy-case/recipe", data={"recipe": recipe})


def _case(db, company, selected_recipe) -> PrivacyCase:
    case = PrivacyCase(company_id=company.id, selected_recipe=selected_recipe, recipe_selected_at=datetime.datetime(2026, 1, 1))
    db.add(case)
    db.commit()
    return case


def _action(db, case, action_type, status) -> PrivacyAction:
    action = PrivacyAction(
        privacy_case_id=case.id, action_type=action_type, method=DeletionMethod.ACCOUNT_SETTING,
        status=status, evidence={},
    )
    db.add(action)
    db.commit()
    return action


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _msg(msg_id, body_text, from_addr, internal_date):
    return {
        "id": msg_id, "labelIds": ["INBOX"], "internalDate": str(internal_date),
        "payload": {
            "headers": [{"name": "From", "value": from_addr}], "mimeType": "text/plain",
            "body": {"data": _b64(body_text)},
        },
    }


GOOP_BROAD_DELETION_REPLY_TEXT = (
    "Hi Lily,\n\n"
    "Thank you for reaching out, and we sincerely apologize for any confusion regarding your data deletion request.\n\n"
    "We can confirm that all personal information associated with your account has been deleted from our system, "
    "including information maintained outside of the account record. There is no remaining personal information "
    "associated with your account in our system.\n\n"
    "We appreciate your patience and understanding, and please don't hesitate to reach out if you have any "
    "further questions."
)

MALK_ACKNOWLEDGMENT_TEXT = (
    "Hello Lily, Thanks for reaching out to MALK Organics! We have received "
    "your email and someone from our team will get back to you as soon as "
    "possible. Thank you!"
)


# ===========================================================================
# 1. FULL CLEAN - Goop shape: ambiguous account-record claim keeps chasing,
#    explicit broad confirmation stops it.
# ===========================================================================


def test_1a_goop_account_record_ambiguous_claim_does_not_complete_and_chase_stays_active(client_db, client):
    company = _company(
        client_db, name="Goop", domain="goop.com", deletion_status=DeletionStatus.SUBMITTED,
        deletion_thread_id="goop-thread", waiting_on=WaitingOn.COMPANY,
        next_followup_at=datetime.datetime(2020, 1, 1),
    )
    reply = _msg("goop-m1", "The account associated with your email has been deleted.", "privacy@goop.com", 1_600_000_000_000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        outcome = check_company_response(client_db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    assert outcome == CHECK_RESULT_NEW_MESSAGE
    assert company.deletion_status == DeletionStatus.ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED
    assert company.deletion_status != DeletionStatus.COMPLETED  # ambiguous account-record claim never completes
    assert company.waiting_on == WaitingOn.COMPANY  # chase remains active
    assert company.next_followup_at is not None
    assert derive_top_level_state(company, None) == TopLevelState.WORKING
    assert company in chase_engine.get_companies_due_for_followup(client_db)  # a follow-up IS still due


def test_1b_goop_broad_confirmation_completes_and_stops_chase(client_db, client):
    company = _company(
        client_db, name="Goop", domain="goop.com", deletion_status=DeletionStatus.ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED,
        deletion_thread_id="goop-thread", waiting_on=WaitingOn.COMPANY,
        next_followup_at=datetime.datetime(2020, 1, 1), deletion_last_response_message_id="goop-m1",
    )
    reply = _msg("goop-m2", GOOP_BROAD_DELETION_REPLY_TEXT, "privacy@goop.com", 1_700_000_000_000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(client_db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    assert company.deletion_status == DeletionStatus.COMPLETED
    assert company.waiting_on is None
    assert company.next_followup_at is None
    assert company.deletion_completed_at is not None
    assert derive_top_level_state(company, None) == TopLevelState.DONE

    with patch("app.chase_engine._send_followup_email") as mock_send:
        sent = chase_engine.process_followups(client_db, creds=MagicMock(), gmail_address="me@gmail.com")
    assert sent == 0
    mock_send.assert_not_called()

    # Truth claim rendered on the dashboard card:
    resp = client.get("/dashboard")
    soup = BeautifulSoup(resp.text, "html.parser")
    card = soup.find("article", {"data-id": str(company.id)})
    assert "Done" in card.get_text()
    assert "confirmed by Goop" in card.get_text()
    assert card.get("data-top-level-state") == TopLevelState.DONE


# ===========================================================================
# 2. FULL CLEAN - MALK shape: generic acknowledgment stays IN_PROGRESS,
#    chase active, never a false completion.
# ===========================================================================


def test_2_malk_acknowledgment_is_in_progress_working_never_completed(client_db, client):
    company = _company(
        client_db, name="MALK Organics", domain="malkorganics.com", deletion_status=DeletionStatus.SUBMITTED,
        deletion_thread_id="malk-thread", waiting_on=WaitingOn.COMPANY, next_followup_at=datetime.datetime(2020, 1, 1),
    )
    reply = _msg("malk-m1", MALK_ACKNOWLEDGMENT_TEXT, "hello@malkorganics.com", 1_600_000_000_000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(client_db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    assert company.deletion_status == DeletionStatus.IN_PROGRESS
    assert company.deletion_status != DeletionStatus.COMPLETED
    assert company.waiting_on == WaitingOn.COMPANY
    assert company.next_followup_at is not None
    assert derive_top_level_state(company, None) == TopLevelState.WORKING
    assert company in chase_engine.get_companies_due_for_followup(client_db)

    resp = client.get("/dashboard")
    soup = BeautifulSoup(resp.text, "html.parser")
    card = soup.find("article", {"data-id": str(company.id)})
    assert "waiting on MALK Organics" in card.get_text()  # "Request received; waiting on company"
    assert card.get("data-top-level-state") == TopLevelState.WORKING


# ===========================================================================
# 3. FULL CLEAN - user action required (verification-needed shape).
# ===========================================================================


def test_3_verification_needed_is_needs_you_no_automatic_followup(client_db, client):
    company = _company(
        client_db, deletion_status=DeletionStatus.SUBMITTED, deletion_thread_id="thread-verify",
        waiting_on=WaitingOn.COMPANY, next_followup_at=datetime.datetime(2020, 1, 1),
    )
    reply = _msg("verify-m1", "Please verify your identity to continue with this request.", "privacy@fabricated-corp.com", 1_600_000_000_000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(client_db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    assert company.deletion_status == DeletionStatus.VERIFICATION_NEEDED
    assert company.waiting_on == WaitingOn.USER
    assert derive_top_level_state(company, None) == TopLevelState.NEEDS_YOU

    # No automatic follow-up fires while the user owes the next step, no
    # matter how overdue next_followup_at looks (it's cleared to None by
    # the WaitingOn.USER transition, but assert the actual due-query too -
    # this is the real gate the background worker relies on).
    assert company.next_followup_at is None
    assert company not in chase_engine.get_companies_due_for_followup(client_db)

    resp = client.get("/dashboard")
    soup = BeautifulSoup(resp.text, "html.parser")
    card = soup.find("article", {"data-id": str(company.id)})
    assert "verify your identity" in card.get_text()
    assert card.get("data-top-level-state") == TopLevelState.NEEDS_YOU


# ===========================================================================
# 4/5. JUST THE ESSENTIALS - mixed state and user-resolved.
# ===========================================================================


def test_4_jte_mixed_user_completed_and_needs_review(client_db, client):
    """NOTE ON THE TOP-LEVEL STATE HERE: the acceptance brief for this pass
    named NEEDS_YOU as the expected destination for a USER_COMPLETED +
    NEEDS_REVIEW mix. Validated against EXISTING semantics (as the brief's
    own precedence section requires) this does not hold: NEEDS_REVIEW
    means Baker's Dozen's own research attempt already ran and couldn't
    verify anything safe - the exact same shape as Full Clean's own
    NO_METHOD_FOUND (also CaseState.WORKING despite showing a "Search
    again" button), and unlike USER_ACTION_REQUIRED, NEEDS_REVIEW's "Look
    again" button is optional/non-committal, not something the user is
    actually being asked to decide. _just_the_essentials_overall
    (app/case_outcome.py) already reflects this consistently - only
    USER_ACTION_REQUIRED (a real, verified mechanism actually waiting on
    the user) trips NEEDS_USER for JTE. Changing that to also treat
    NEEDS_REVIEW as NEEDS_USER would make it inconsistent with Full
    Clean's own NOT_STARTED/NO_METHOD_FOUND/UNKNOWN precedent (all
    WORKING despite a retry button), so this acceptance pass did NOT
    change it - see the final report for this judgment call in full."""
    company = _company(client_db, deletion_status=DeletionStatus.READY)
    case = _case(client_db, company, RecipeChoice.JUST_THE_ESSENTIALS)
    tracking = _action(client_db, case, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP, PrivacyActionStatus.USER_COMPLETED)
    opt_out = _action(client_db, case, PrivacyActionType.SALE_SHARING_OPT_OUT, PrivacyActionStatus.NEEDS_REVIEW)
    actions = [tracking, opt_out]

    outcome = derive_case_outcome(company, case, actions)
    assert outcome.overall != CaseState.RESOLVED
    assert outcome.overall != CaseState.USER_RESOLVED
    assert derive_top_level_state(company, case, actions) == TopLevelState.WORKING

    summary = just_the_essentials_dashboard_summary(actions)
    assert "completed by you" in summary  # provenance-aware, never bare "confirmed"
    assert "needs review" in summary
    assert "company confirmed" not in summary.lower()
    assert "verified" not in summary.lower()

    # No Full Clean behavior invoked for a JTE company at all:
    assert company.deletion_status == DeletionStatus.READY
    assert company.waiting_on is None
    assert company.deletion_evidence in (None, {})


def test_5_jte_both_user_completed_is_user_resolved_and_done(client_db, client):
    company = _company(client_db, deletion_status=DeletionStatus.READY)
    case = _case(client_db, company, RecipeChoice.JUST_THE_ESSENTIALS)
    actions = [
        _action(client_db, case, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP, PrivacyActionStatus.USER_COMPLETED),
        _action(client_db, case, PrivacyActionType.SALE_SHARING_OPT_OUT, PrivacyActionStatus.USER_COMPLETED),
    ]

    outcome = derive_case_outcome(company, case, actions)
    assert outcome.overall == CaseState.USER_RESOLVED
    assert derive_top_level_state(company, case, actions) == TopLevelState.DONE

    # Still distinguishable internally from a real company/system-confirmed
    # resolution (same top-level DONE bucket, different CaseState/summary):
    confirmed_actions = [
        _action(client_db, case, PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP, PrivacyActionStatus.CONFIRMED),
        _action(client_db, case, PrivacyActionType.SALE_SHARING_OPT_OUT, PrivacyActionStatus.CONFIRMED),
    ]
    confirmed_outcome = derive_case_outcome(company, case, confirmed_actions)
    assert confirmed_outcome.overall == CaseState.RESOLVED
    assert confirmed_outcome.overall != outcome.overall
    assert derive_top_level_state(company, case, confirmed_actions) == TopLevelState.DONE  # same top-level bucket

    summary = just_the_essentials_dashboard_summary(actions)
    assert summary == "2 of 2 privacy controls completed by you"
    confirmed_summary = just_the_essentials_dashboard_summary(confirmed_actions)
    assert confirmed_summary == "2 of 2 privacy controls confirmed"
    assert summary != confirmed_summary


# ===========================================================================
# 6. PANTRY - no chase/research/reward, historical evidence preserved,
#    no active-work count contamination.
# ===========================================================================


def test_6_pantry_projects_pantry_no_chase_evidence_preserved(client_db, client):
    company = _company(
        client_db, deletion_status=DeletionStatus.COMPLETED,
        deletion_evidence={"type": "gmail_reply", "quote": "your personal data has been deleted", "confidence": "high"},
    )
    resp = _select_recipe(client, company.id, RecipeChoice.LEAVE_IT_BE)
    assert resp.status_code in (200, 303)
    client_db.refresh(company)

    case = client_db.query(PrivacyCase).filter(PrivacyCase.company_id == company.id).one()
    assert derive_top_level_state(company, case) == TopLevelState.PANTRY

    # Historical evidence untouched:
    outcome = derive_case_outcome(company, case)
    assert outcome.is_pantry is True
    assert outcome.overall == CaseState.RESOLVED
    assert company.deletion_status == DeletionStatus.COMPLETED

    resp = client.get("/dashboard")
    soup = BeautifulSoup(resp.text, "html.parser")
    pantry_card = soup.find("article", {"data-id": str(company.id), "data-pantry": "true"})
    assert pantry_card is not None
    assert pantry_card.get("data-top-level-state") == TopLevelState.PANTRY

    # No active-work count contamination (see main.py's _status_counts).
    from app.main import _status_counts
    counts = _status_counts(client_db)
    assert counts["methods_ready"] == 0
    assert counts["requests_sent"] == 0


def test_6b_pantry_selection_stops_an_already_active_chase(client_db, client):
    """BLOCKER FOUND DURING THIS ACCEPTANCE PASS: the recipe-selection API
    route had no server-side guard against selecting LEAVE_IT_BE for a
    company with an already-active EMAIL_REQUEST chase (waiting_on=
    COMPANY, next_followup_at due) - select_recipe() itself is documented
    to never touch Company fields (by design, so recipe intent stays
    separate from execution evidence), so nothing was stopping a due
    follow-up email from firing after the user explicitly chose to leave
    the company alone. Today's dashboard UI only ever offers a recipe
    choice from READY/FAILED (never from a state with an active chase),
    so this was unreachable through the app's own buttons - but the API
    route itself is the actual authority, and a privacy tool's core
    Pantry promise ("we stop pursuing this") must hold there too, not by
    accident of which HTML happens to render today.

    Fix: main.py's select_company_recipe route now calls the existing,
    already-tested chase_engine.pause_followups() when the selected
    recipe is LEAVE_IT_BE - the same idempotent mechanism the dashboard's
    manual "Pause follow-ups" button already uses. It touches only
    Company.followups_paused (get_companies_due_for_followup's own
    existing gate) - never waiting_on/next_followup_at/deletion_status,
    so no historical evidence is altered and chase cadence itself is
    unchanged."""
    company = _company(
        client_db, deletion_status=DeletionStatus.SUBMITTED, deletion_thread_id="thread-active-chase",
        waiting_on=WaitingOn.COMPANY, next_followup_at=datetime.datetime(2020, 1, 1),
    )
    assert company in chase_engine.get_companies_due_for_followup(client_db)  # chase genuinely active beforehand

    resp = _select_recipe(client, company.id, RecipeChoice.LEAVE_IT_BE)
    assert resp.status_code in (200, 303)
    client_db.refresh(company)

    # Historical/evidence fields untouched - Pantry never erases history:
    assert company.deletion_status == DeletionStatus.SUBMITTED
    assert company.waiting_on == WaitingOn.COMPANY
    assert company.next_followup_at == datetime.datetime(2020, 1, 1)

    # But the chase itself has genuinely stopped:
    assert company.followups_paused is True
    assert company not in chase_engine.get_companies_due_for_followup(client_db)

    with patch("app.chase_engine._send_followup_email") as mock_send:
        sent = chase_engine.process_followups(client_db, creds=MagicMock(), gmail_address="me@gmail.com")
    assert sent == 0
    mock_send.assert_not_called()


# ===========================================================================
# 7. FAILURE / REJECTION - needs a human decision, no false success, no
#    ordinary automatic chase.
# ===========================================================================


def test_7_rejection_is_needs_you_never_false_success(client_db, client):
    company = _company(
        client_db, deletion_status=DeletionStatus.SUBMITTED, deletion_thread_id="thread-reject",
        waiting_on=WaitingOn.COMPANY, next_followup_at=datetime.datetime(2020, 1, 1),
    )
    reply = _msg("reject-m1", "We are unable to fulfill this request; it is denied.", "privacy@fabricated-corp.com", 1_600_000_000_000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(client_db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    assert company.deletion_status == DeletionStatus.REJECTED
    assert company.deletion_status != DeletionStatus.COMPLETED
    assert company.waiting_on == WaitingOn.ESCALATION_NEEDED
    assert derive_top_level_state(company, None) == TopLevelState.NEEDS_YOU

    # No ordinary automatic chase - ESCALATION_NEEDED is never picked up by
    # the plain "waiting_on == COMPANY" follow-up query.
    assert company not in chase_engine.get_companies_due_for_followup(client_db)

    resp = client.get("/dashboard")
    soup = BeautifulSoup(resp.text, "html.parser")
    card = soup.find("article", {"data-id": str(company.id)})
    assert "Needs your attention" in card.get_text()
    assert "declined" in card.get_text()
    assert card.get("data-top-level-state") == TopLevelState.NEEDS_YOU


# ===========================================================================
# 8. UNKNOWN - stays conservative, WORKING or NEEDS_YOU by next-actor,
#    never DONE.
# ===========================================================================


def test_8a_unknown_response_stays_working_when_next_actor_is_company(client_db, client):
    company = _company(
        client_db, deletion_status=DeletionStatus.SUBMITTED, deletion_thread_id="thread-unknown",
        waiting_on=WaitingOn.COMPANY, next_followup_at=datetime.datetime(2020, 1, 1),
    )
    reply = _msg("unk-m1", "Thanks for your email!", "privacy@fabricated-corp.com", 1_600_000_000_000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(client_db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())

    assert company.deletion_status == DeletionStatus.UNKNOWN_RESPONSE
    assert company.deletion_status != DeletionStatus.COMPLETED
    top_level = derive_top_level_state(company, None)
    assert top_level in (TopLevelState.WORKING, TopLevelState.NEEDS_YOU)
    assert top_level == (TopLevelState.NEEDS_YOU if company.waiting_on == WaitingOn.USER else TopLevelState.WORKING)
    assert top_level != TopLevelState.DONE


def test_8b_unknown_response_never_resolves_to_done_regardless_of_waiting_on(client_db):
    for waiting_on in (WaitingOn.COMPANY, WaitingOn.USER, None):
        company = _company(
            client_db, name=f"Co {waiting_on}", domain=f"unknown-{waiting_on}.example",
            deletion_status=DeletionStatus.UNKNOWN_RESPONSE, waiting_on=waiting_on,
        )
        assert derive_top_level_state(company, None) != TopLevelState.DONE


# ===========================================================================
# LIVE/REAL REGRESSIONS explicitly re-verified as part of this acceptance
# pass (each already has dedicated, more exhaustive coverage elsewhere -
# these are the compact end-to-end confirmations for this pass specifically).
# ===========================================================================


def test_regression_quoted_outgoing_text_cannot_trigger_completion(client_db, client):
    company = _company(
        client_db, deletion_status=DeletionStatus.SUBMITTED, deletion_thread_id="thread-quote",
        waiting_on=WaitingOn.COMPANY, next_followup_at=datetime.datetime(2020, 1, 1),
    )
    raw = (
        "Please let us know soon.\n\n"
        "On Mon, Jan 1, 2024, Baker's Dozen wrote:\n"
        "> We are asking whether all personal information associated with your account has been deleted,\n"
        "> including information maintained outside of the account record.\n"
    )
    reply = _msg("quote-m1", raw, "privacy@fabricated-corp.com", 1_600_000_000_000)
    with patch("app.google_oauth.fetch_thread_messages", return_value=[reply]):
        check_company_response(client_db, company, creds=MagicMock(), gmail_address="me@gmail.com", classifier=ResponseClassifier())
    assert company.deletion_status != DeletionStatus.COMPLETED


def test_regression_stale_goop_account_record_self_corrects(client_db, client):
    """The reconciliation path (f91b30e) - a company already stuck on
    ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED for its last-processed reply
    self-corrects to COMPLETED using only already-stored evidence."""
    from app import mail
    from app.deletion_response_tracker import reclassify_stale_unknown_response
    from app.response_classify import ResponseClassification

    company = _company(
        client_db, deletion_status=DeletionStatus.ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED,
        deletion_thread_id="thread-stale", waiting_on=WaitingOn.COMPANY, next_followup_at=datetime.datetime(2020, 1, 1),
    )
    message = _msg("stale-m1", GOOP_BROAD_DELETION_REPLY_TEXT, "privacy@fabricated-corp.com", 1_600_000_000_000)
    mail.record_inbound_mail_message(
        client_db, company, message, GOOP_BROAD_DELETION_REPLY_TEXT,
        ResponseClassification(status=DeletionStatus.ACCOUNT_RECORD_DELETED_DATA_UNVERIFIED, confidence="high", quote="x"),
    )
    company.deletion_last_response_message_id = "stale-m1"
    client_db.commit()

    with patch("app.google_oauth.fetch_thread_messages") as mock_fetch:
        changed = reclassify_stale_unknown_response(client_db, company, ResponseClassifier())
    assert changed is True
    mock_fetch.assert_not_called()
    assert company.deletion_status == DeletionStatus.COMPLETED


def test_regression_jte_amazon_style_corporate_family_route_fails_closed():
    """JTE research must never verify a mechanism merely because it shares
    an official corporate domain family (e.g. aws.amazon.com for an
    amazon.com case) without also applying to the target service - see
    app/privacy_action_research.py's _is_exact_official_host."""
    from app.privacy_action_research import _is_exact_official_host

    assert _is_exact_official_host("amazon-fixture.com", "aws.amazon-fixture.com") is False
    assert _is_exact_official_host("amazon-fixture.com", "amazon-fixture.com") is True


def test_regression_pantry_to_jte_shows_active_jte_presentation_not_full_clean(client_db, client):
    company = _company(client_db, deletion_status=DeletionStatus.READY)
    _case(client_db, company, RecipeChoice.LEAVE_IT_BE)

    resp = _select_recipe(client, company.id, RecipeChoice.JUST_THE_ESSENTIALS)
    assert resp.status_code in (200, 303)

    resp = client.get("/dashboard")
    soup = BeautifulSoup(resp.text, "html.parser")
    card = soup.find("article", {"data-id": str(company.id)})
    assert card is not None
    assert "Delete my data" not in card.get_text()
    assert "Review cleanup" in card.get_text()
