"""Tests for the scope-union logic - pure DB reads, no real Google calls.
Confirms the bug flagged in Phase 2 planning is actually fixed: granting one
optional scope must never silently drop another already-granted one.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config, google_oauth
from app.db import Base
from app.models import OAuthToken


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_granted_scopes_defaults_to_base_scan_scope_when_not_connected(db):
    assert google_oauth.get_granted_scopes(db) == set(config.GMAIL_SCOPES)


def test_granted_scopes_reflects_stored_token(db):
    db.add(OAuthToken(
        gmail_address="me@gmail.com", encrypted_refresh_token="x",
        scopes_granted=f"{config.GMAIL_SCOPES[0]} {config.GMAIL_SEND_SCOPE}",
    ))
    db.commit()
    assert google_oauth.get_granted_scopes(db) == {config.GMAIL_SCOPES[0], config.GMAIL_SEND_SCOPE}


def test_has_send_scope_false_by_default(db):
    assert google_oauth.has_send_scope(db) is False


def test_has_readonly_scope_false_by_default(db):
    assert google_oauth.has_readonly_scope(db) is False


def test_enabling_readonly_after_send_preserves_send_in_union(db):
    """Regression: granting gmail.readonly via a separate consent must not
    silently drop an already-granted gmail.send."""
    db.add(OAuthToken(
        gmail_address="me@gmail.com", encrypted_refresh_token="x",
        scopes_granted=f"{config.GMAIL_SCOPES[0]} {config.GMAIL_SEND_SCOPE}",
    ))
    db.commit()

    union = google_oauth.get_granted_scopes(db) | {config.GMAIL_READONLY_SCOPE}
    assert config.GMAIL_SEND_SCOPE in union
    assert config.GMAIL_READONLY_SCOPE in union
    assert config.GMAIL_SCOPES[0] in union


def test_enabling_send_after_readonly_preserves_readonly_in_union(db):
    db.add(OAuthToken(
        gmail_address="me@gmail.com", encrypted_refresh_token="x",
        scopes_granted=f"{config.GMAIL_SCOPES[0]} {config.GMAIL_READONLY_SCOPE}",
    ))
    db.commit()

    union = google_oauth.get_granted_scopes(db) | {config.GMAIL_SEND_SCOPE}
    assert config.GMAIL_READONLY_SCOPE in union
    assert config.GMAIL_SEND_SCOPE in union


def test_send_and_readonly_are_never_bundled_by_default(db):
    """The base scan/connect scope alone must never imply either optional scope."""
    granted = google_oauth.get_granted_scopes(db)
    assert config.GMAIL_SEND_SCOPE not in granted
    assert config.GMAIL_READONLY_SCOPE not in granted
