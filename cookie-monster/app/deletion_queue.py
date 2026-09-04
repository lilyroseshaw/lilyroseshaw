"""Single-process background worker: deletion-method research (Phase 1) and
company response tracking (Phase 2), one tick, two jobs.

This is the POC-appropriate substitute for a real task queue
(Celery/RQ/cloud tasks) - see TODO.md for what a production deployment
would need instead. The property that actually matters here is preserved
without extra infrastructure: the *work itself* lives in the database, not
in memory - which domains still need research (DeletionRecipe rows) and
which companies are due for a response check (Company.deletion_response_checked_at
+ backoff). If the app restarts mid-tick, nothing is lost; both are simply
picked up again on the next tick.

The worker runs everything in a thread (httpx/Gmail API/DB access here are
all synchronous) so it never blocks the event loop serving dashboard requests.
"""
import asyncio
import logging

from app import chase_engine, config, google_oauth
from app.db import get_session
from app.deletion_research import DeletionResearchProvider, build_default_provider
from app.deletion_resolver import process_pending
from app.deletion_response_tracker import process_response_checks
from app.response_classify import ResponseClassifier, build_default_classifier

# Explicit handler/level so per-tick activity (recipes researched, threads
# checked) is actually visible in the terminal running uvicorn, instead of
# being silently swallowed by uvicorn's default root log level - "is there
# enough logging to tell what research is doing" was an explicit gap
# raised in the recipe-verification investigation.
logger = logging.getLogger("cookie_monster.deletion_queue")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _queue_handler = logging.StreamHandler()
    _queue_handler.setFormatter(logging.Formatter("%(asctime)s [cookie-monster-queue] %(message)s"))
    logger.addHandler(_queue_handler)
    logger.propagate = False

_worker_task: asyncio.Task | None = None


def _process_recipe_research(provider: DeletionResearchProvider) -> int:
    db = get_session()
    try:
        return process_pending(db, provider)
    finally:
        db.close()


def _process_response_checks(classifier: ResponseClassifier) -> int:
    """No-op (returns 0) unless the user has completed the separate
    gmail.readonly consent - response tracking is opt-in, not assumed."""
    db = get_session()
    try:
        if not google_oauth.has_readonly_scope(db):
            return 0
        creds = google_oauth.load_credentials(db)
        gmail_address = google_oauth.get_connected_address(db)
        if creds is None or gmail_address is None:
            return 0
        return process_response_checks(db, creds, gmail_address, classifier)
    finally:
        db.close()


def _process_followups() -> int:
    """No-op unless BOTH gmail.readonly (to read the thread) and
    gmail.send (to actually reply) have been granted - the 24-hour chase
    is strictly a superset of what response-checking already requires."""
    db = get_session()
    try:
        if not (google_oauth.has_readonly_scope(db) and google_oauth.has_send_scope(db)):
            return 0
        creds = google_oauth.load_credentials(db)
        gmail_address = google_oauth.get_connected_address(db)
        if creds is None or gmail_address is None:
            return 0
        return chase_engine.process_followups(db, creds, gmail_address)
    finally:
        db.close()


def _run_one_tick(provider: DeletionResearchProvider, classifier: ResponseClassifier) -> tuple[int, int, int]:
    recipes_processed = _process_recipe_research(provider)
    responses_checked = _process_response_checks(classifier)
    followups_sent = _process_followups()
    return recipes_processed, responses_checked, followups_sent


async def _run_forever(provider: DeletionResearchProvider, classifier: ResponseClassifier) -> None:
    while True:
        await asyncio.sleep(config.DELETION_QUEUE_INTERVAL_SECONDS)
        try:
            recipes_processed, responses_checked, followups_sent = await asyncio.to_thread(
                _run_one_tick, provider, classifier
            )
            if recipes_processed:
                logger.info("deletion enrichment tick processed %d recipe(s)", recipes_processed)
            if responses_checked:
                logger.info("response-tracking tick checked %d thread(s)", responses_checked)
            if followups_sent:
                logger.info("chase tick sent %d follow-up(s)", followups_sent)
        except Exception:
            logger.exception("background worker tick failed")


def start_background_worker(
    provider: DeletionResearchProvider | None = None, classifier: ResponseClassifier | None = None
) -> asyncio.Task | None:
    """Called once at app startup. No-op if already running. Research runs
    with DELETION_RESEARCH_ENABLED honored (NullResearchProvider if false);
    response-checking runs only if gmail.readonly has been separately
    granted - both are checked fresh on every tick, not just at startup."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return _worker_task
    provider = provider or build_default_provider()
    classifier = classifier or build_default_classifier()
    _worker_task = asyncio.create_task(_run_forever(provider, classifier))
    return _worker_task


def stop_background_worker() -> None:
    global _worker_task
    if _worker_task is not None:
        _worker_task.cancel()
        _worker_task = None
