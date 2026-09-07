"""Regression tests for the "Delete my data" confirmation modal's actual
on-screen behavior.

Bug this file exists for: the modal (#deletion-modal) is server-rendered
with the `hidden` attribute and is only meant to become visible after a
user clicks a company's "Delete my data" button (dashboard.js). But
`.deletion-modal` in style.css sets `display: flex`, and nothing overrode
that for the `[hidden]` state - the browser's own `[hidden] { display:
none }` rule and the author rule `.deletion-modal { display: flex }` have
EQUAL CSS specificity, and author rules win ties against the UA
stylesheet. So the modal was visible on every dashboard load regardless of
the `hidden` attribute, and clicking Cancel - which correctly re-sets
`hidden` in the DOM - had no visible effect, because the CSS was never
looking at that attribute in the first place.

A pure server-rendered-HTML test (e.g. asserting the string "hidden"
appears in the response) would NOT have caught this - the attribute was
always present and correct; only the browser's actual visual rendering
was wrong. This uses a real headless browser (Playwright) against a real
running instance of the app to check what a user actually sees, not just
what the server sent.

Requires the Playwright Python package AND its Chromium browser to be
installed (`pip install playwright && playwright install chromium`) - if
either is missing, these tests are skipped rather than failing the suite,
since this is one extra, optional layer of UI coverage on top of the
(mandatory) pytest suite covering everything else.
"""
import datetime
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.db as dbmod
from app import config
from app.db import Base
from app.deletion_constants import DeletionMethod, DeletionStatus
from app.main import app
from app.models import Company

playwright_sync_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
sync_playwright = playwright_sync_api.sync_playwright

# Only set for this specific sandbox, where the pip-installed playwright's
# expected browser build doesn't match what's pre-cached on disk. On a
# normal machine (including a real dev's Mac after `playwright install
# chromium`), this is unset and Playwright auto-detects its own browser.
_CHROMIUM_OVERRIDE = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")


@pytest.fixture()
def live_server(monkeypatch):
    """Playwright drives a real browser making real HTTP requests, so it
    needs an actual running server - not an in-process ASGI TestClient."""
    path = tempfile.mktemp(suffix=".db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(dbmod, "engine", engine)
    monkeypatch.setattr(dbmod, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(config, "DELETION_QUEUE_INTERVAL_SECONDS", 9999)
    monkeypatch.setattr(config, "GOOGLE_REDIRECT_URI", "http://127.0.0.1:8000/auth/callback")

    db = dbmod.SessionLocal()
    company = Company(
        name="Widget Co", domain="widgetco.com", relationship_type="transactional", status="confirmed",
        confidence="high", evidence_count=1, evidence_types=[], example_subjects=[], detection_reasons=[],
        first_seen=datetime.datetime(2022, 1, 1), last_seen=datetime.datetime(2022, 1, 1),
        deletion_status=DeletionStatus.READY, deletion_method=DeletionMethod.WEB_FORM,
        deletion_url="https://widgetco.com/privacy/delete", deletion_verified=True,
    )
    db.add(company)
    db.commit()
    company_id = company.id
    db.close()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server_config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(server_config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "test server did not start in time"

    yield f"http://127.0.0.1:{port}", company_id

    server.should_exit = True
    thread.join(timeout=5)
    Path(path).unlink(missing_ok=True)


@pytest.fixture()
def page():
    with sync_playwright() as p:
        launch_kwargs = {"args": ["--no-sandbox"]}
        if _CHROMIUM_OVERRIDE:
            launch_kwargs["executable_path"] = _CHROMIUM_OVERRIDE
        try:
            browser = p.chromium.launch(**launch_kwargs)
        except Exception as exc:  # noqa: BLE001 - environment-dependent, not a code defect
            pytest.skip(f"Chromium not available for Playwright: {exc}")
        pg = browser.new_page()
        yield pg
        browser.close()


def test_dashboard_loads_with_modal_closed(live_server, page):
    base_url, _ = live_server
    page.goto(f"{base_url}/dashboard")
    modal = page.locator("#deletion-modal")
    assert modal.is_hidden(), "the deletion modal must not be visible just from opening the dashboard"


def test_clicking_delete_my_data_opens_the_modal(live_server, page):
    base_url, _ = live_server
    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    modal = page.locator("#deletion-modal")
    assert modal.is_visible(), "the modal should open after an explicit 'Delete my data' click"


def test_clicking_cancel_closes_the_modal(live_server, page):
    base_url, _ = live_server
    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    assert page.locator("#deletion-modal").is_visible()

    page.click("#deletion-modal-cancel")
    assert page.locator("#deletion-modal").is_hidden(), "Cancel must actually hide the modal, not just no-op"


def test_cancel_never_submits_a_deletion_request(live_server, page):
    base_url, company_id = live_server
    execute_requests = []
    page.on(
        "request",
        lambda req: execute_requests.append(req.url)
        if req.method == "POST" and "/deletion/execute" in req.url
        else None,
    )

    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    page.click("#deletion-modal-cancel")
    page.wait_for_timeout(200)  # give any errant request a moment to fire

    assert execute_requests == [], "Cancel must never POST to the deletion/execute endpoint"

    # And the company's actual status must be untouched - a request-level
    # check and a state-level check, so neither can silently mask a bug.
    page.goto(f"{base_url}/dashboard")
    assert "Deletion method ready" in page.content()
    assert page.locator("#deletion-modal").is_hidden()


# --- Cleanup Recipes milestone, Full Clean UX fix: seamless two-stage
# consent flow (no close-then-reopen, no ambiguous/stale controls) - see
# app/static/dashboard.js's openModal()/recipe-submit handler and
# style.css's ".deletion-modal-actions .btn[hidden]" rule, which fixes the
# actual root cause (an equal-CSS-specificity author-vs-UA-stylesheet tie,
# same class of bug as the .deletion-modal[hidden] fix above) that let
# BOTH the recipe-choice and confirm/execute buttons render visible at
# once regardless of which one JS had set `hidden` on.

def test_first_stage_has_no_ambiguous_continue_button(live_server, page):
    """Before a recipe is selected, the modal must show exactly Cancel and
    the three available Cleanup Recipe choices - never the execute-flow's
    Continue/Send/Open button alongside them."""
    base_url, _ = live_server
    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    visible = page.locator("#deletion-modal-form button:visible").all_inner_texts()
    assert visible == ["Cancel", "Choose Full Clean", "Choose Just the Essentials", "Choose Leave It Be"]


def test_choosing_full_clean_transitions_seamlessly_without_closing_modal(live_server, page):
    """Choosing Full Clean must never close the modal and dump the user
    back on the dashboard - it transitions the SAME modal directly into
    the real preview, so only one click on "Delete my data" is ever
    needed."""
    base_url, _ = live_server
    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    page.click("#deletion-modal-recipe-submit")
    page.wait_for_timeout(500)

    assert page.locator("#deletion-modal").is_visible(), "the modal must stay open through the transition"
    assert page.locator("#deletion-modal-confirm").is_visible(), "the real preview must now be showing"
    assert page.locator("#deletion-modal-choose-recipe").is_hidden()


def test_second_stage_has_no_stale_choosing_control(live_server, page):
    """Once the real preview is showing, there must be exactly one
    capability-specific primary CTA plus Cancel - never a leftover/stale
    "Choosing…" recipe-selection control."""
    base_url, _ = live_server
    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    page.click("#deletion-modal-recipe-submit")
    page.wait_for_timeout(500)

    visible = page.locator("#deletion-modal-form button:visible").all_inner_texts()
    assert visible == ["Cancel", "Open the verified page"]
    assert "Choosing" not in " ".join(visible)


def test_full_clean_flow_uses_bakers_dozen_branding(live_server, page):
    """No 'Cookie Monster' should remain anywhere in the Full Clean modal's
    user-facing copy, in either stage."""
    base_url, _ = live_server
    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    assert "Cookie Monster" not in page.locator("#deletion-modal").inner_text()

    page.click("#deletion-modal-recipe-submit")
    page.wait_for_timeout(500)
    assert "Cookie Monster" not in page.locator("#deletion-modal").inner_text()
    assert "Baker's Dozen" in page.locator("#deletion-modal").inner_text()


# --- Just the Essentials milestone: a real browser must show the second
# recipe choice, transition seamlessly into its own review stage (not the
# Full Clean preview/execute stage), never expose Leave It Be, and never
# fire a single execute/send request just from choosing or viewing it.

def test_choosing_just_the_essentials_transitions_seamlessly_to_its_own_review(live_server, page):
    base_url, _ = live_server
    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    page.click("#deletion-modal-jte-submit")
    page.wait_for_timeout(500)

    assert page.locator("#deletion-modal").is_visible(), "the modal must stay open through the transition"
    assert page.locator("#deletion-modal-jte-review").is_visible(), "the Just the Essentials review must now be showing"
    assert page.locator("#deletion-modal-choose-recipe").is_hidden()
    assert page.locator("#deletion-modal-confirm").is_hidden(), "Full Clean's preview/execute stage must never show for this recipe"


def test_just_the_essentials_review_offers_find_cleanup_method_for_both_actions(live_server, page):
    """Before any research has run, both actions must offer the real
    "Find cleanup method" action - never the old developer-facing "Needs
    research" label, and never a fabricated URL or a false claim of
    progress."""
    base_url, _ = live_server
    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    page.click("#deletion-modal-jte-submit")
    page.wait_for_timeout(500)

    actions_el = page.locator("#deletion-modal-jte-actions")
    text = actions_el.inner_text()
    assert "Nonessential tracking" in text
    assert "Sale/sharing" in text or "opt-out" in text.lower()
    assert "Needs research" not in text
    assert actions_el.locator("button:has-text('Find cleanup method')").count() == 2


def test_just_the_essentials_stage_has_no_execute_button(live_server, page):
    """Reviewing Just the Essentials is purely informational - there must
    be no consequential submit button, only Cancel."""
    base_url, _ = live_server
    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    page.click("#deletion-modal-jte-submit")
    page.wait_for_timeout(500)

    visible = page.locator("#deletion-modal-form button:visible").all_inner_texts()
    assert visible == ["Cancel"]


def test_leave_it_be_is_offered_but_only_in_the_recipe_picker_stage(live_server, page):
    """Leave It Be is now a real, offered recipe (see The Pantry milestone)
    - but only in the picker stage itself, never leaking into a DIFFERENT
    recipe's own review/confirm stage once one has been chosen."""
    base_url, _ = live_server
    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    assert "Leave It Be" in page.locator("#deletion-modal").inner_text()

    page.click("#deletion-modal-jte-submit")
    page.wait_for_timeout(500)
    assert "Leave It Be" not in page.locator("#deletion-modal").inner_text()


def test_selecting_or_viewing_just_the_essentials_never_fires_an_execute_request(live_server, page):
    base_url, _ = live_server
    execute_requests = []
    page.on(
        "request",
        lambda req: execute_requests.append(req.url)
        if req.method == "POST" and ("/deletion/execute" in req.url or "/deletion/mark-completed" in req.url)
        else None,
    )

    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    page.click("#deletion-modal-jte-submit")
    page.wait_for_timeout(500)
    page.click("#deletion-modal-cancel")
    page.wait_for_timeout(200)

    assert execute_requests == [], "choosing/viewing Just the Essentials must never POST to an execution route"


def test_just_the_essentials_flow_uses_bakers_dozen_branding(live_server, page):
    base_url, _ = live_server
    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    page.click("#deletion-modal-jte-submit")
    page.wait_for_timeout(500)
    assert "Cookie Monster" not in page.locator("#deletion-modal").inner_text()


# --- Just the Essentials research pipeline: "Find cleanup method" live click ---
# The provider is monkeypatched to a fake (no real network access, no real
# Amazon/company request) so these exercise the REAL click -> AJAX ->
# server -> resolve_privacy_action -> DOM-update path in a genuine browser,
# without ever touching the public internet during a test run.

class _FakeVerifiedProvider:
    def research(self, domain, action_type):
        from app.deletion_constants import DeletionMethod, PrivacyActionType
        from app.research_types import PrivacyActionResult

        if action_type != PrivacyActionType.NONESSENTIAL_TRACKING_CLEANUP:
            return None
        url = f"https://{domain}/privacy/ad-preferences"
        return PrivacyActionResult(
            domain=domain, action_type=action_type, method=DeletionMethod.ACCOUNT_SETTING,
            url=url, source_url=url, confidence="high",
            scope_note="This can stop some future nonessential tracking, but does not confirm past data was deleted.",
            verified=True, reasons=["fixture"],
        )


class _FakeNothingFoundProvider:
    def research(self, domain, action_type):
        return None


def test_find_cleanup_method_click_reveals_a_verified_mechanism(live_server, page, monkeypatch):
    monkeypatch.setattr("app.main._privacy_action_provider", _FakeVerifiedProvider())
    base_url, _ = live_server
    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    page.click("#deletion-modal-jte-submit")
    page.wait_for_timeout(500)

    tracking_item = page.locator("#deletion-modal-jte-actions li", has_text="Nonessential tracking")
    opt_out_item = page.locator("#deletion-modal-jte-actions li", has_text="Sale/sharing")
    tracking_item.locator("button:has-text('Find cleanup method')").click()
    page.wait_for_timeout(500)

    assert tracking_item.locator("a:has-text('Open privacy settings')").count() == 1
    assert "Ready for you" in tracking_item.inner_text()
    assert "does not confirm" in tracking_item.inner_text().lower()
    # The OTHER action must be completely untouched by resolving this one.
    assert opt_out_item.locator("button:has-text('Find cleanup method')").count() == 1
    assert "Ready for you" not in opt_out_item.inner_text()


def test_find_cleanup_method_click_shows_needs_review_when_nothing_verified(live_server, page, monkeypatch):
    monkeypatch.setattr("app.main._privacy_action_provider", _FakeNothingFoundProvider())
    base_url, _ = live_server
    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    page.click("#deletion-modal-jte-submit")
    page.wait_for_timeout(500)

    opt_out_item = page.locator("#deletion-modal-jte-actions li", has_text="Sale/sharing")
    opt_out_item.locator("button:has-text('Find cleanup method')").click()
    page.wait_for_timeout(500)

    text = opt_out_item.inner_text()
    assert "Needs review" in text
    assert "couldn't verify" in text.lower()
    # A "Look again" retry must be offered - NEEDS_REVIEW is never terminal.
    assert opt_out_item.locator("button:has-text('Look again')").count() == 1
    # No internal jargon anywhere in what's shown.
    for jargon in ("NEEDS_RESEARCH", "NEEDS_REVIEW", "PrivacyAction", "resolver"):
        assert jargon not in text


# --- Leave It Be / The Pantry: a real browser must show it as a third
# recipe, moving a company between the active list and the Pantry is a
# real (if full-reload) transition, never a duplicate, and choosing it
# never fires any consequential request.

def test_leave_it_be_shown_as_third_recipe_choice_live(live_server, page):
    base_url, _ = live_server
    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")

    visible = page.locator("#deletion-modal-form button:visible").all_inner_texts()
    assert visible == ["Cancel", "Choose Full Clean", "Choose Just the Essentials", "Choose Leave It Be"]


def test_choosing_leave_it_be_never_fires_an_execute_request(live_server, page):
    base_url, _ = live_server
    execute_requests = []
    page.on(
        "request",
        lambda req: execute_requests.append(req.url)
        if req.method == "POST" and "/deletion/execute" in req.url
        else None,
    )

    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    page.click("#deletion-modal-leave-it-be-submit")
    page.wait_for_load_state("load")
    page.wait_for_timeout(300)

    assert execute_requests == []


def test_choosing_leave_it_be_moves_company_into_pantry_live(live_server, page):
    """Choosing Leave It Be relocates the company between two different
    sections of the page (active list -> Pantry) - a full reload, not an
    in-place swap (see dashboard.js's swapCard pantry-boundary check).
    After it, the company must appear in the Pantry and NOT in the active
    company list - never both."""
    base_url, company_id = live_server
    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    page.click("#deletion-modal-leave-it-be-submit")
    page.wait_for_load_state("load")
    page.wait_for_timeout(300)

    active_card = page.locator(f"#merge-select-scope #company-{company_id}")
    assert active_card.count() == 0, "the company must no longer show in the active area"

    # The Pantry section is collapsed by default (visually secondary) -
    # expand it to see/interact with its contents, same as any user would.
    page.click(".pantry-section summary")
    pantry_section = page.locator(".pantry-section")
    pantry_card = pantry_section.locator(f"#company-{company_id}")
    assert pantry_card.count() == 1, "the company must appear exactly once, in the Pantry"
    assert "Leave It Be" in pantry_card.inner_text()
    assert "Change recipe" in pantry_card.inner_text()

    # Never duplicated anywhere else on the page.
    assert page.locator(f"#company-{company_id}").count() == 1


def test_pantry_change_recipe_reopens_the_cleanup_recipe_picker(live_server, page):
    """The Pantry card's ONE action must return the user to the same
    three-choice Cleanup Recipe picker - never straight into a specific
    recipe's own confirm/review stage."""
    base_url, company_id = live_server
    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    page.click("#deletion-modal-leave-it-be-submit")
    page.wait_for_load_state("load")
    page.wait_for_timeout(300)

    page.click(".pantry-section summary")
    page.locator(f".pantry-section #company-{company_id} .delete-my-data-btn").click()
    page.wait_for_timeout(300)

    assert page.locator("#deletion-modal").is_visible()
    assert page.locator("#deletion-modal-choose-recipe").is_visible()
    assert page.locator("#deletion-modal-confirm").is_hidden()
    assert page.locator("#deletion-modal-jte-review").is_hidden()
    visible = page.locator("#deletion-modal-form button:visible").all_inner_texts()
    assert visible == ["Cancel", "Choose Full Clean", "Choose Just the Essentials", "Choose Leave It Be"]


def test_pantry_change_recipe_back_to_full_clean_removes_from_pantry_live(live_server, page):
    base_url, company_id = live_server
    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    page.click("#deletion-modal-leave-it-be-submit")
    page.wait_for_load_state("load")
    page.wait_for_timeout(300)

    page.click(".pantry-section summary")
    page.locator(f".pantry-section #company-{company_id} .delete-my-data-btn").click()
    page.wait_for_timeout(300)
    page.click("#deletion-modal-recipe-submit")
    page.wait_for_load_state("load")
    page.wait_for_timeout(300)

    page.click(".pantry-section summary")
    pantry_section = page.locator(".pantry-section")
    assert pantry_section.locator(f"#company-{company_id}").count() == 0
    active_card = page.locator(f"#merge-select-scope #company-{company_id}")
    assert active_card.count() == 1


def test_leave_it_be_flow_uses_bakers_dozen_branding(live_server, page):
    base_url, _ = live_server
    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    assert "Cookie Monster" not in page.locator("#deletion-modal").inner_text()


def test_user_step_required_flow_does_not_claim_submission(live_server, page):
    """A MANUAL_HANDOFF company's preview copy must never imply Baker's
    Dozen already sent/submitted anything, and must state plainly that
    login/identity verification/CAPTCHA/MFA are on the user."""
    base_url, _ = live_server
    page.goto(f"{base_url}/dashboard")
    page.click(".delete-my-data-btn")
    page.click("#deletion-modal-recipe-submit")
    page.wait_for_timeout(500)

    text = page.locator("#deletion-modal-confirm").inner_text()
    lowered = text.lower()
    assert "sent" not in lowered and "submitted" not in lowered
    assert "login" in lowered or "identity verification" in lowered
    assert "captcha" in lowered or "mfa" in lowered
