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

# Background enrichment queue (app/deletion_queue.py) - a single-process
# asyncio poller, not a real task queue. Good enough for one local user;
# see TODO.md for what a production version would need instead.
DELETION_QUEUE_INTERVAL_SECONDS = int(os.environ.get("DELETION_QUEUE_INTERVAL_SECONDS", "60"))
DELETION_QUEUE_BATCH_SIZE = int(os.environ.get("DELETION_QUEUE_BATCH_SIZE", "3"))

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
