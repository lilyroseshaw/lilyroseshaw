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
