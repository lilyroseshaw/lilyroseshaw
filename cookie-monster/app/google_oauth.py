"""Google OAuth 2.0 (Authorization Code flow).

The default connect/scan flow requests ONLY gmail.metadata - see
config.GMAIL_SCOPES. Two further scopes exist purely for their own optional
features and are only ever requested through their own clearly-labeled UI
action, never bundled silently into the scan connection:

- gmail.send - "send deletion-request emails automatically"
  (get_send_authorization_url)
- gmail.readonly - "track company responses" (get_response_tracking_authorization_url)

Only one OAuthToken row exists (single local user), and re-consenting
replaces it entirely - so every "enable X" flow requests the UNION of
whatever scopes are already granted plus the one new scope (see
get_granted_scopes), not a hardcoded pair. Otherwise enabling response
tracking after already enabling sending would silently drop the send grant.

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


def get_granted_scopes(db: Session) -> set[str]:
    """Scopes on the current token, or the base scan scope if not connected
    yet. Used to compute the union for an additional-consent flow, so
    granting one optional scope never silently drops another already-granted
    one (see module docstring)."""
    row = db.query(OAuthToken).first()
    if row is None:
        return set(config.GMAIL_SCOPES)
    return set(row.scopes_granted.split())


def get_send_authorization_url(db: Session) -> tuple[str, str]:
    """Separate, explicit consent step for the optional auto-send feature.
    Requests (whatever's already granted) + gmail.send - a fresh full
    consent, not an invisible scope bump - so Google's own consent screen
    shows every permission being granted, and no previously-granted scope
    (e.g. gmail.readonly, if enabled first) gets silently dropped."""
    scopes = get_granted_scopes(db) | {config.GMAIL_SEND_SCOPE}
    return get_authorization_url(scopes=sorted(scopes))


def get_response_tracking_authorization_url(db: Session) -> tuple[str, str]:
    """Separate, explicit consent step for the optional response-tracking
    feature. Same union approach as get_send_authorization_url - never drops
    an already-granted scope."""
    scopes = get_granted_scopes(db) | {config.GMAIL_READONLY_SCOPE}
    return get_authorization_url(scopes=sorted(scopes))


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
    return config.GMAIL_SEND_SCOPE in get_granted_scopes(db)


def has_readonly_scope(db: Session) -> bool:
    """Whether the user has completed the SEPARATE gmail.readonly consent
    step. False for everyone by default - required before any response
    tracking can run."""
    return config.GMAIL_READONLY_SCOPE in get_granted_scopes(db)


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


def fetch_thread_messages(creds: Credentials, thread_id: str) -> list[dict]:
    """Fetches every message in ONE specific thread - never a search, never
    a list of the inbox. This is the only Gmail read call response tracking
    ever makes, and thread_id always comes from Company.deletion_thread_id
    (a thread Cookie Monster itself started by sending a deletion request),
    never from searching/listing. Requires gmail.readonly (see
    has_readonly_scope) - raises googleapiclient.errors.HttpError on
    failure, which the caller (deletion_response_tracker.py) classifies as
    transient or permanent."""
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    return thread.get("messages", [])


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
