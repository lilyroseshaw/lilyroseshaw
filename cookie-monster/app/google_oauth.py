"""Google OAuth 2.0 (Authorization Code flow), scoped to gmail.metadata only.

Never requests, sees, or stores the user's Google password - that's Google's
job, not ours. Only a refresh token is persisted, and only encrypted
(see app/crypto.py).
"""
import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from sqlalchemy.orm import Session

from app import config, crypto
from app.models import OAuthToken


def _client_config() -> dict:
    return {
        "web": {
            "client_id": config.GOOGLE_CLIENT_ID,
            "client_secret": config.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [config.GOOGLE_REDIRECT_URI],
        }
    }


def build_flow() -> Flow:
    config.require_google_credentials()
    flow = Flow.from_client_config(_client_config(), scopes=config.GMAIL_SCOPES)
    flow.redirect_uri = config.GOOGLE_REDIRECT_URI
    return flow


def get_authorization_url() -> tuple[str, str]:
    flow = build_flow()
    # prompt=consent + access_type=offline: guarantees a refresh token is issued
    # even on a reconnect, and the consent screen always shows the scope grant.
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="false",
        prompt="consent",
    )
    return auth_url, state


def exchange_code_for_credentials(code: str) -> Credentials:
    flow = build_flow()
    flow.fetch_token(code=code)
    return flow.credentials


def get_gmail_address(creds: Credentials) -> str:
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    profile = service.users().getProfile(userId="me").execute()
    return profile["emailAddress"]


def save_credentials(db: Session, creds: Credentials, gmail_address: str) -> None:
    db.query(OAuthToken).delete()
    token_row = OAuthToken(
        gmail_address=gmail_address,
        encrypted_refresh_token=crypto.encrypt(creds.refresh_token),
        scopes_granted=" ".join(creds.scopes or config.GMAIL_SCOPES),
    )
    db.add(token_row)
    db.commit()


def load_credentials(db: Session) -> Credentials | None:
    row = db.query(OAuthToken).first()
    if row is None:
        return None
    config.require_google_credentials()
    creds = Credentials(
        token=None,
        refresh_token=crypto.decrypt(row.encrypted_refresh_token),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=config.GOOGLE_CLIENT_ID,
        client_secret=config.GOOGLE_CLIENT_SECRET,
        scopes=row.scopes_granted.split(),
    )
    creds.refresh(GoogleAuthRequest())
    return creds


def get_connected_address(db: Session) -> str | None:
    row = db.query(OAuthToken).first()
    return row.gmail_address if row else None


def revoke_and_forget(db: Session) -> None:
    """Revoke the token at Google (best-effort) and delete it locally either way."""
    row = db.query(OAuthToken).first()
    if row is not None:
        try:
            refresh_token = crypto.decrypt(row.encrypted_refresh_token)
            requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": refresh_token},
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
        except Exception:
            pass  # local deletion below still proceeds; user can also revoke via myaccount.google.com
    db.query(OAuthToken).delete()
    db.commit()
