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


def test_migration_normalizes_legacy_submitted_to_completed(legacy_db):
    engine = create_engine(f"sqlite:///{legacy_db}")
    migrations.migrate(engine, str(legacy_db))
    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT deletion_status, deletion_evidence, deletion_completed_at FROM companies WHERE id = 1"
        ).fetchone()
    status, evidence_raw, completed_at = row
    assert status == "COMPLETED"  # self-report, not system-verified SUBMITTED
    evidence = json.loads(evidence_raw)
    assert evidence["type"] == "user_reported"
    assert "User confirmed" in evidence["note"]
    assert completed_at is not None


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
    assert row[0] == "COMPLETED"  # unchanged by re-running


def test_migration_on_fresh_database_is_a_noop(tmp_path):
    db_path = tmp_path / "fresh.db"
    engine = create_engine(f"sqlite:///{db_path}")
    # No companies table exists yet - create_all() would make it fresh.
    result = migrations.migrate(engine, str(db_path))
    assert result == {"added_columns": [], "normalized_rows": 0}
