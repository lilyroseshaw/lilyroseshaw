"""Google OAuth 2.0 (Authorization Code flow).

The default connect/scan flow requests ONLY gmail.metadata - see
config.GMAIL_SCOPES. A second, separate scope (gmail.send) exists purely for
the optional "send deletion-request emails automatically" feature and is
only ever requested through get_send_authorization_url(), which is only
reachable from its own clearly-labeled UI action, never bundled silently
into the scan connection.

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


def build_flow(scopes: list[str] | None = None) -> Flow:
    config.require_google_credentials()
    flow = Flow.from_client_config(_client_config(), scopes=scopes or config.GMAIL_SCOPES)
    flow.redirect_uri = config.GOOGLE_REDIRECT_URI
    return flow


def get_authorization_url(scopes: list[str] | None = None) -> tuple[str, str]:
    flow = build_flow(scopes)
    # prompt=consent + access_type=offline: guarantees a refresh token is issued
    # even on a reconnect, and the consent screen always shows the scope grant.
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="false",
        prompt="consent",
    )
    return auth_url, state


def get_send_authorization_url() -> tuple[str, str]:
    """Separate, explicit consent step for the optional auto-send feature.
    Requests gmail.metadata + gmail.send together (a fresh full consent,
    not an invisible scope bump) so Google's own consent screen shows both
    permissions being granted."""
    return get_authorization_url(scopes=[*config.GMAIL_SCOPES, config.GMAIL_SEND_SCOPE])


def exchange_code_for_credentials(code: str, scopes: list[str] | None = None) -> Credentials:
    flow = build_flow(scopes)
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


def has_send_scope(db: Session) -> bool:
    """Whether the user has completed the SEPARATE gmail.send consent step.
    False for everyone by default - the scan/connect flow never grants this."""
    row = db.query(OAuthToken).first()
    if row is None:
        return False
    return config.GMAIL_SEND_SCOPE in row.scopes_granted.split()


def send_email(creds: Credentials, to_email: str, subject: str, body_text: str) -> dict:
    """Sends exactly one email as the connected user. Only ever called from
    deletion_engine.py, only for EMAIL_REQUEST deletions, only after the user
    has both completed the separate gmail.send consent AND clicked a
    per-company confirmation. Returns Gmail's send response (contains the
    message id used as SUBMITTED evidence)."""
    import base64
    from email.mime.text import MIMEText

    message = MIMEText(body_text)
    message["to"] = to_email
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return service.users().messages().send(userId="me", body={"raw": raw}).execute()


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
