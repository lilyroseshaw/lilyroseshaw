"""Environment configuration. All secrets come from .env / real env vars only."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/callback")

# Minimum viable read-only scope: headers/labels only, never message bodies.
# See README "Gmail data processing" section for why this scope was chosen.
# This is the ONLY scope ever requested for connecting/scanning Gmail - it is
# never silently combined with GMAIL_SEND_SCOPE below.
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.metadata"]

# Send-only scope (cannot read, delete, or modify existing mail). Only ever
# requested via a separate, explicit, clearly-labeled consent step for the
# optional "send deletion request emails automatically" feature - see
# google_oauth.get_send_authorization_url() and the /auth/enable-sending
# route. Never bundled into GMAIL_SCOPES / the default connect flow.
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"

COOKIE_MONSTER_SECRET_KEY = os.environ.get("COOKIE_MONSTER_SECRET_KEY", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-only-insecure-secret")

DATABASE_PATH = os.environ.get("DATABASE_PATH", "./data/cookie_monster.db")

APP_HOST = os.environ.get("APP_HOST", "127.0.0.1")
APP_PORT = int(os.environ.get("APP_PORT", "8000"))

# --- Deletion-method research (see app/deletion_research.py) ---

# Master switch. False = use NullResearchProvider (no outbound requests at
# all, everything unresolved lands in NEEDS_RESEARCH). True = use
# WebResearchProvider - Tier A (same-domain crawl) is always active when
# this is true; Tier B (search) and Pass 2 (LLM extraction) below are each
# independently optional and only activate if their own key is set.
DELETION_RESEARCH_ENABLED = os.environ.get("DELETION_RESEARCH_ENABLED", "true").lower() == "true"

# Tier B: optional search-engine fallback for companies the same-domain
# crawl can't find anything for. Unset = Tier A only.
BRAVE_SEARCH_API_KEY = os.environ.get("BRAVE_SEARCH_API_KEY", "")
# Several distinct, narrowly-scoped query angles are tried per triggered
# Tier B attempt (deletion-page / data-rights-page / privacy-email), not
# one generic query - see research_search.brave_query_patterns().
BRAVE_SEARCH_QUERIES_PER_ATTEMPT = int(os.environ.get("BRAVE_SEARCH_QUERIES_PER_ATTEMPT", "3"))
# Hard daily cap on Brave queries (a paid API) - once exhausted, the
# worker stops making Brave requests for the rest of the day. Hitting
# this never counts as a failed research attempt for a company; that
# company's research is simply deferred until the budget is available
# again. Default is a conservative development budget - raise it once
# you have a sense of real usage.
BRAVE_SEARCH_DAILY_QUERY_BUDGET = int(os.environ.get("BRAVE_SEARCH_DAILY_QUERY_BUDGET", "100"))

# Pass 2: optional LLM-assisted extraction for privacy pages the regex/
# keyword pass can't confidently parse. Unset = Pass 1 (regex) only.
# Never used to invent a URL/email - see research_extract.py's verbatim
# containment check.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DELETION_RESEARCH_LLM_MODEL = os.environ.get("DELETION_RESEARCH_LLM_MODEL", "claude-haiku-4-5-20251001")

# How long a verified recipe is trusted before it's re-queued for research.
DELETION_RECIPE_FRESHNESS_DAYS = int(os.environ.get("DELETION_RECIPE_FRESHNESS_DAYS", "150"))
# How long to wait before retrying a company whose last research attempt
# came back NEEDS_RESEARCH, so a hard-to-verify company isn't re-hit every scan.
DELETION_RECIPE_RETRY_COOLDOWN_DAYS = int(os.environ.get("DELETION_RECIPE_RETRY_COOLDOWN_DAYS", "7"))
# After this many unsuccessful research attempts on a recipe that's never
# been verified, a company's status becomes NO_METHOD_FOUND instead of the
# generic "still retrying" UNKNOWN - a distinct, honest "this one may
# genuinely need a manual look" signal instead of the same message
# forever. Retries keep happening on the normal cooldown regardless - this
# only changes what's displayed, never stops future attempts (manual or
# automatic).
DELETION_RECIPE_FAILURE_THRESHOLD = int(os.environ.get("DELETION_RECIPE_FAILURE_THRESHOLD", "3"))

# Background enrichment queue (app/deletion_queue.py) - a single-process
# asyncio poller, not a real task queue. Good enough for one local user;
# see TODO.md for what a production version would need instead.
DELETION_QUEUE_INTERVAL_SECONDS = int(os.environ.get("DELETION_QUEUE_INTERVAL_SECONDS", "60"))
DELETION_QUEUE_BATCH_SIZE = int(os.environ.get("DELETION_QUEUE_BATCH_SIZE", "3"))
# How many companies' research can run concurrently within one batch/tick -
# bounded so one slow site can't stall the rest of the same batch, but
# never spawns more outbound requests at once than this. Deliberately
# small/conservative - this is a politeness limit, not a performance knob.
DELETION_RESEARCH_MAX_CONCURRENCY = int(os.environ.get("DELETION_RESEARCH_MAX_CONCURRENCY", "3"))

# Politeness limits for the crawler/fetcher - never hammer a company's site.
RESEARCH_HTTP_TIMEOUT_SECONDS = float(os.environ.get("RESEARCH_HTTP_TIMEOUT_SECONDS", "10"))
RESEARCH_MAX_PAGES_PER_COMPANY = int(os.environ.get("RESEARCH_MAX_PAGES_PER_COMPANY", "5"))

# --- Response tracking (see app/deletion_response_tracker.py) ---

# Read-only scope. Only ever requested via a separate, explicit consent step
# (/auth/enable-response-tracking), off by default, never bundled into
# GMAIL_SCOPES or GMAIL_SEND_SCOPE. The app only ever calls threads().get()
# on a thread_id it already stored itself - never lists/searches the inbox.
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

# Minimum time between checks of a healthy (no recent failures) thread.
RESPONSE_CHECK_MIN_INTERVAL_HOURS = float(os.environ.get("RESPONSE_CHECK_MIN_INTERVAL_HOURS", "6"))
# Exponential backoff for a thread whose checks keep hitting technical
# errors: base * 2^failures, capped at max. A transient failure never
# changes the underlying deletion status - see deletion_response_tracker.py.
RESPONSE_CHECK_BACKOFF_BASE_HOURS = float(os.environ.get("RESPONSE_CHECK_BACKOFF_BASE_HOURS", "1"))
RESPONSE_CHECK_BACKOFF_MAX_HOURS = float(os.environ.get("RESPONSE_CHECK_BACKOFF_MAX_HOURS", "48"))
# Max threads checked per background-worker tick.
RESPONSE_CHECK_BATCH_SIZE = int(os.environ.get("RESPONSE_CHECK_BATCH_SIZE", "5"))

# --- 24-hour chase (chase_engine.py) ---
# How often Baker's Dozen follows up on its own, while waiting_on=COMPANY.
FOLLOWUP_INTERVAL_HOURS = float(os.environ.get("FOLLOWUP_INTERVAL_HOURS", "24"))
# A follow-up lock older than this is treated as ambiguous (the process
# that set it likely crashed or hung) and triggers reconciliation against
# the tracked Gmail thread before the case can be touched again.
FOLLOWUP_LOCK_STALE_MINUTES = float(os.environ.get("FOLLOWUP_LOCK_STALE_MINUTES", "10"))
# If reconciliation finds NO evidence the send happened, retry this soon
# rather than waiting a full interval or hot-looping every tick.
FOLLOWUP_RECONCILE_RETRY_MINUTES = float(os.environ.get("FOLLOWUP_RECONCILE_RETRY_MINUTES", "15"))
# Minimum total time since a lock was first set - with the tracked thread
# checked and showing no evidence of the send THROUGHOUT that whole window,
# not just once - before a "not found" result is trusted enough to permit
# a fresh (potentially duplicate-risking) resend. Gmail's API gives no
# documented guarantee that a just-accepted send is IMMEDIATELY visible via
# threads().get() the instant it returns 200 - in the crash-after-Gmail-
# accepted-but-before-we-recorded-it window, a single early "not found"
# check could in principle be racing a brief propagation lag, not a
# genuine non-send. Requiring the thread to still show nothing after this
# much elapsed wall-clock time (checked on every worker tick in between -
# see reconcile_stale_followup_locks) makes that far less likely than
# trusting one single check. Must be large relative to any plausible
# Gmail indexing lag (expected to be sub-second to low seconds in normal
# operation) - this default is a deliberately generous margin, not a
# measured bound.
FOLLOWUP_RECONCILE_CONFIRM_MINUTES = float(os.environ.get("FOLLOWUP_RECONCILE_CONFIRM_MINUTES", "20"))
# If a lock stays unresolved (reconciliation itself keeps failing) past
# this age, stop retrying silently and surface it for manual review.
FOLLOWUP_RECONCILE_MAX_AGE_HOURS = float(os.environ.get("FOLLOWUP_RECONCILE_MAX_AGE_HOURS", "24"))
# Max chase follow-ups sent per background-worker tick.
FOLLOWUP_BATCH_SIZE = int(os.environ.get("FOLLOWUP_BATCH_SIZE", "5"))


def require_google_credentials() -> None:
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise RuntimeError(
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set. "
            "Copy .env.example to .env and fill in your Google Cloud OAuth client values."
        )


def require_secret_key() -> None:
    if not COOKIE_MONSTER_SECRET_KEY:
        raise RuntimeError(
            "COOKIE_MONSTER_SECRET_KEY is not set. Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
