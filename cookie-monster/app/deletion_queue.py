"""Single-process background enrichment worker.

This is the POC-appropriate substitute for a real task queue
(Celery/RQ/cloud tasks) - see TODO.md for what a production deployment
would need instead. The property that actually matters here is preserved
without extra infrastructure: the *work itself* (which domains still need
research) lives in the DeletionRecipe table, not in memory - see
deletion_resolver.process_pending(). If the app restarts mid-tick, nothing
is lost; the same recipes are simply picked up again on the next tick.

The worker runs research calls in a thread (httpx/DB access here is
synchronous) so it never blocks the event loop serving dashboard requests.
"""
import asyncio
import logging

from app import config
from app.db import get_session
from app.deletion_research import DeletionResearchProvider, build_default_provider
from app.deletion_resolver import process_pending

logger = logging.getLogger("cookie_monster.deletion_queue")

_worker_task: asyncio.Task | None = None


def _process_one_batch(provider: DeletionResearchProvider) -> int:
    db = get_session()
    try:
        return process_pending(db, provider)
    finally:
        db.close()


async def _run_forever(provider: DeletionResearchProvider) -> None:
    while True:
        await asyncio.sleep(config.DELETION_QUEUE_INTERVAL_SECONDS)
        try:
            processed = await asyncio.to_thread(_process_one_batch, provider)
            if processed:
                logger.info("deletion enrichment tick processed %d recipe(s)", processed)
        except Exception:
            logger.exception("deletion enrichment tick failed")


def start_background_worker(provider: DeletionResearchProvider | None = None) -> asyncio.Task | None:
    """Called once at app startup. No-op if already running or if research
    is disabled entirely (DELETION_RESEARCH_ENABLED=false still runs the
    loop, but with NullResearchProvider, so ticks are instant no-ops - kept
    simple rather than special-casing the loop itself off)."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return _worker_task
    provider = provider or build_default_provider()
    _worker_task = asyncio.create_task(_run_forever(provider))
    return _worker_task


def stop_background_worker() -> None:
    global _worker_task
    if _worker_task is not None:
        _worker_task.cancel()
        _worker_task = None
