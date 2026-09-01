import json
import sqlite3

import pytest
from sqlalchemy import create_engine

from app import migrations

# Mirrors the schema this app actually shipped with at commit 891415b:
# the original columns plus deletion_status/deletion_requested_at/deletion_evidence
# in their old, simple form - no registry/engine columns yet.
LEGACY_SCHEMA = """
CREATE TABLE companies (
    id INTEGER PRIMARY KEY,
    name VARCHAR(200),
    domain VARCHAR(255) UNIQUE,
    relationship_type VARCHAR(32),
    status VARCHAR(16),
    confidence VARCHAR(16),
    evidence_count INTEGER,
    evidence_types JSON,
    example_subjects JSON,
    detection_reasons JSON,
    first_seen DATETIME,
    last_seen DATETIME,
    user_corrected BOOLEAN,
    deletion_status VARCHAR(50),
    deletion_requested_at DATETIME,
    deletion_evidence VARCHAR(500),
    created_at DATETIME,
    updated_at DATETIME
);
"""


@pytest.fixture()
def legacy_db(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO companies (id, name, domain, relationship_type, status, confidence, evidence_count, "
        "evidence_types, example_subjects, detection_reasons, first_seen, last_seen, user_corrected, "
        "deletion_status, deletion_requested_at, deletion_evidence, created_at, updated_at) VALUES "
        "(1, 'Lyft', 'lyft.com', 'transactional', 'confirmed', 'high', 3, '[]', '[]', '[]', "
        "'2022-01-01 00:00:00', '2022-01-01 00:00:00', 0, "
        "'submitted', '2026-08-01 00:00:00', 'User confirmed deletion request was submitted', "
        "'2022-01-01 00:00:00', '2022-01-01 00:00:00')"
    )
    conn.execute(
        "INSERT INTO companies (id, name, domain, relationship_type, status, confidence, evidence_count, "
        "evidence_types, example_subjects, detection_reasons, first_seen, last_seen, user_corrected, "
        "deletion_status, deletion_requested_at, deletion_evidence, created_at, updated_at) VALUES "
        "(2, 'Random Co', 'randomco.com', 'account', 'pending', 'low', 1, '[]', '[]', '[]', "
        "'2023-01-01 00:00:00', '2023-01-01 00:00:00', 0, "
        "NULL, NULL, NULL, '2023-01-01 00:00:00', '2023-01-01 00:00:00')"
    )
    conn.commit()
    conn.close()
    return db_path


def test_migration_adds_new_columns(legacy_db):
    engine = create_engine(f"sqlite:///{legacy_db}")
    result = migrations.migrate(engine, str(legacy_db))
    with engine.connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(companies)")}
    for name, _, _ in migrations.NEW_DELETION_COLUMNS:
        assert name in cols
    assert set(result["added_columns"]) >= {
        "deletion_method", "deletion_action_capability", "deletion_url", "deletion_verified",
    }
    # Columns that already existed shouldn't be reported as "added".
    assert "deletion_status" not in result["added_columns"]


def test_migration_normalizes_legacy_submitted_without_overclaiming(legacy_db):
    """The old free-text "submitted" was a self-report with no system-level
    proof. It must never become DeletionStatus.COMPLETED (that would claim
    the whole deletion process finished) or an unlabeled SUBMITTED (that
    would look identical to a real Gmail-send-verified one). It's mapped to
    SUBMITTED but tagged unambiguously as a legacy self-report."""
    engine = create_engine(f"sqlite:///{legacy_db}")
    migrations.migrate(engine, str(legacy_db))
    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT deletion_status, deletion_evidence, deletion_completed_at FROM companies WHERE id = 1"
        ).fetchone()
    status, evidence_raw, completed_at = row
    assert status == "SUBMITTED"
    evidence = json.loads(evidence_raw)
    assert evidence["type"] == "user_reported"
    assert evidence["legacy"] is True
    assert "User confirmed" in evidence["note"]
    # Never claims completion - that would overclaim what the old data actually proves.
    assert completed_at is None
    from app.deletion_constants import is_system_verified
    assert is_system_verified(status, evidence) is False


def test_migration_normalizes_null_status_to_not_started(legacy_db):
    engine = create_engine(f"sqlite:///{legacy_db}")
    migrations.migrate(engine, str(legacy_db))
    with engine.connect() as conn:
        row = conn.exec_driver_sql("SELECT deletion_status FROM companies WHERE id = 2").fetchone()
    assert row[0] == "NOT_STARTED"


def test_migration_preserves_existing_data(legacy_db):
    engine = create_engine(f"sqlite:///{legacy_db}")
    migrations.migrate(engine, str(legacy_db))
    with engine.connect() as conn:
        row = conn.exec_driver_sql("SELECT name, domain, status, evidence_count FROM companies WHERE id = 1").fetchone()
    assert row == ("Lyft", "lyft.com", "confirmed", 3)


def test_migration_backs_up_database_before_changing_it(legacy_db):
    engine = create_engine(f"sqlite:///{legacy_db}")
    migrations.migrate(engine, str(legacy_db))
    backups = list(legacy_db.parent.glob(f"{legacy_db.name}.bak-*"))
    assert len(backups) == 1


def test_migration_is_idempotent(legacy_db):
    engine = create_engine(f"sqlite:///{legacy_db}")
    migrations.migrate(engine, str(legacy_db))
    second_result = migrations.migrate(engine, str(legacy_db))
    assert second_result["added_columns"] == []
    with engine.connect() as conn:
        row = conn.exec_driver_sql("SELECT deletion_status FROM companies WHERE id = 1").fetchone()
    assert row[0] == "SUBMITTED"  # unchanged by re-running


def test_migration_on_fresh_database_is_a_noop(tmp_path):
    db_path = tmp_path / "fresh.db"
    engine = create_engine(f"sqlite:///{db_path}")
    # No companies table exists yet - create_all() would make it fresh.
    result = migrations.migrate(engine, str(db_path))
    assert result == {"added_columns": [], "normalized_rows": 0, "backfilled_recipes": 0}


def test_migration_does_not_back_up_a_fresh_empty_database(tmp_path):
    """create_all() runs before migrate() in the real app (see db.py) and
    creates `companies` fresh with every current column, including
    deletion_status - that alone must not trigger a backup on every new
    install; only real pre-existing data should."""
    from app.db import Base

    db_path = tmp_path / "fresh.db"
    engine = create_engine(f"sqlite:///{db_path}")
    from app import models  # noqa: F401 - register models on Base.metadata

    Base.metadata.create_all(bind=engine)
    result = migrations.migrate(engine, str(db_path))
    assert result["added_columns"] == []
    assert not list(tmp_path.glob("fresh.db.bak-*"))


def test_migration_backfills_deletion_recipe_from_verified_company(tmp_path):
    """A Company row already resolved by the pre-DeletionRecipe-table version
    of this app (deletion_verified=1, a real method) gets a matching
    DeletionRecipe row created for its domain, so it isn't silently
    re-researched from scratch after upgrading."""
    from app.db import Base

    db_path = tmp_path / "upgrade.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE companies (
            id INTEGER PRIMARY KEY, name VARCHAR(200), domain VARCHAR(255) UNIQUE,
            relationship_type VARCHAR(32), status VARCHAR(16), confidence VARCHAR(16),
            evidence_count INTEGER, evidence_types JSON, example_subjects JSON, detection_reasons JSON,
            first_seen DATETIME, last_seen DATETIME, user_corrected BOOLEAN,
            deletion_method VARCHAR(32), deletion_action_capability VARCHAR(32), deletion_status VARCHAR(32),
            deletion_url VARCHAR(500), deletion_email VARCHAR(255), deletion_instructions VARCHAR(1000),
            deletion_verified BOOLEAN, deletion_source_url VARCHAR(500), deletion_last_checked DATETIME,
            deletion_requested_at DATETIME, deletion_completed_at DATETIME, deletion_evidence JSON,
            deletion_error VARCHAR(500), created_at DATETIME, updated_at DATETIME
        );
        """
    )
    conn.execute(
        "INSERT INTO companies (id,name,domain,relationship_type,status,confidence,evidence_count,"
        "evidence_types,example_subjects,detection_reasons,first_seen,last_seen,user_corrected,"
        "deletion_method,deletion_action_capability,deletion_status,deletion_url,deletion_email,"
        "deletion_instructions,deletion_verified,deletion_source_url,deletion_last_checked,"
        "deletion_requested_at,deletion_completed_at,deletion_evidence,deletion_error,created_at,updated_at) "
        "VALUES (1,'Lyft','lyft.com','account','confirmed','high',5,'[]','[]','[]','2022-01-01','2022-01-01',0,"
        "'ACCOUNT_SETTING','USER_ACTION_REQUIRED','READY','https://account.lyft.com/privacy/data/delete',NULL,"
        "'Sign in and submit the request.',1,'https://account.lyft.com/privacy/data/delete','2026-08-01',"
        "NULL,NULL,'{}',NULL,'2022-01-01','2022-01-01')"
    )
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{db_path}")
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    result = migrations.migrate(engine, str(db_path))
    assert result["backfilled_recipes"] == 1

    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT domain, method, status, origin FROM deletion_recipes WHERE domain = 'lyft.com'"
        ).fetchone()
    assert row == ("lyft.com", "ACCOUNT_SETTING", "VERIFIED", "migrated")

    # Idempotent - running again doesn't duplicate the recipe.
    second = migrations.migrate(engine, str(db_path))
    assert second["backfilled_recipes"] == 0
