"""Minimal, additive SQLite migration for the local prototype.

There is no migration framework in this project (the app previously relied
on `Base.metadata.create_all()`, which only creates *missing tables* - it
never alters an existing one). This module adds exactly the columns this
change introduces to an *existing* `companies` table, and normalizes the
values in three columns that already existed with an earlier, simpler
convention (deletion_status was free text like "submitted"/NULL,
deletion_evidence was a plain sentence) into the controlled vocabulary and
JSON evidence format the rest of the deletion feature now expects.

It never drops or renames a column, never discards existing data (legacy
values are reinterpreted, not deleted), and always backs up the database
file first if it's about to change anything in an existing table.

A brand-new database (no `companies` table yet) needs no migration at all -
`create_all()` already creates it with every current column.
"""
import datetime
import json
import shutil
from pathlib import Path

from sqlalchemy import Engine, text

from app.deletion_constants import ActionCapability, DeletionMethod, DeletionStatus

# (column_name, SQLite column type, SQL literal to backfill existing rows with, or None for NULL)
NEW_DELETION_COLUMNS: list[tuple[str, str, str | None]] = [
    ("deletion_method", "VARCHAR(32)", f"'{DeletionMethod.UNKNOWN}'"),
    ("deletion_action_capability", "VARCHAR(32)", f"'{ActionCapability.UNKNOWN}'"),
    ("deletion_status", "VARCHAR(32)", f"'{DeletionStatus.NOT_STARTED}'"),
    ("deletion_url", "VARCHAR(500)", None),
    ("deletion_email", "VARCHAR(255)", None),
    ("deletion_instructions", "VARCHAR(1000)", None),
    ("deletion_verified", "BOOLEAN", "0"),
    ("deletion_source_url", "VARCHAR(500)", None),
    ("deletion_last_checked", "DATETIME", None),
    ("deletion_requested_at", "DATETIME", None),
    ("deletion_completed_at", "DATETIME", None),
    ("deletion_evidence", "JSON", "'{}'"),
    ("deletion_error", "VARCHAR(500)", None),
]


def backup_database(db_path: str) -> str | None:
    """Copies the SQLite file aside before a schema/data change. Returns the
    backup path, or None if there was no existing file to back up."""
    path = Path(db_path)
    if not path.exists():
        return None
    timestamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    backup_path = path.with_name(f"{path.name}.bak-{timestamp}")
    shutil.copy2(path, backup_path)
    return str(backup_path)


def _existing_tables(conn) -> set[str]:
    return {row[0] for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}


def _existing_columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}


def _add_missing_columns(conn, existing_cols: set[str]) -> list[str]:
    missing = [c for c in NEW_DELETION_COLUMNS if c[0] not in existing_cols]
    added = []
    for name, sql_type, default_literal in missing:
        conn.exec_driver_sql(f"ALTER TABLE companies ADD COLUMN {name} {sql_type}")
        if default_literal is not None:
            conn.exec_driver_sql(f"UPDATE companies SET {name} = {default_literal} WHERE {name} IS NULL")
        added.append(name)
    return added


def _normalize_legacy_deletion_data(conn, has_deletion_columns: bool) -> int:
    """Upgrades rows written by the pre-registry version of this app, where
    deletion_status/deletion_requested_at/deletion_evidence already existed
    with a simpler meaning: deletion_status was NULL or the literal string
    "submitted" (set only when the user self-reported completing a request
    by hand), and deletion_evidence was a plain human-readable sentence.

    That old "submitted" maps to the new DeletionStatus.COMPLETED (a
    self-report), not SUBMITTED - SUBMITTED is now reserved for cases where
    Cookie Monster itself has system-level evidence (see deletion_engine.py).
    Idempotent: rows already in the new format are left untouched.
    """
    if not has_deletion_columns:
        return 0

    rows = conn.execute(
        text(
            "SELECT id, deletion_status, deletion_evidence, deletion_requested_at, deletion_completed_at "
            "FROM companies"
        )
    ).fetchall()

    changed = 0
    for company_id, status, evidence, requested_at, completed_at in rows:
        new_status = status
        new_completed_at = completed_at

        if status is None:
            new_status = DeletionStatus.NOT_STARTED
        elif status == "submitted":
            new_status = DeletionStatus.COMPLETED
            if not completed_at:
                new_completed_at = requested_at
        elif status not in DeletionStatus.ALL:
            # Unrecognized legacy value - fall back to a safe known state
            # rather than inventing a meaning for it.
            new_status = DeletionStatus.NOT_STARTED

        new_evidence = evidence
        if evidence and not evidence.strip().startswith("{"):
            new_evidence = json.dumps({"type": "user_reported", "note": evidence})
        elif evidence is None:
            new_evidence = json.dumps({})

        if new_status != status or new_evidence != evidence or new_completed_at != completed_at:
            conn.execute(
                text(
                    "UPDATE companies SET deletion_status = :status, deletion_evidence = :evidence, "
                    "deletion_completed_at = :completed_at WHERE id = :id"
                ),
                {"status": new_status, "evidence": new_evidence, "completed_at": new_completed_at, "id": company_id},
            )
            changed += 1
    return changed


def migrate(engine: Engine, db_path: str) -> dict[str, object]:
    """Adds any missing deletion-tracking columns to an existing `companies`
    table and normalizes legacy deletion_status/deletion_evidence values.
    Returns {"added_columns": [...], "normalized_rows": N}, both empty/zero
    if there was nothing to do (including a brand-new database - create_all()
    creates it fresh with every current column, so no migration applies)."""
    with engine.connect() as conn:
        if "companies" not in _existing_tables(conn):
            return {"added_columns": [], "normalized_rows": 0}

        existing_cols = _existing_columns(conn, "companies")
        needs_columns = any(c[0] not in existing_cols for c in NEW_DELETION_COLUMNS)
        # deletion_status/deletion_evidence predate this migration in some existing
        # databases (added by an earlier version of this app) - normalize legacy
        # values in them whenever the column is present, not only when we just added it.
        had_deletion_status_already = "deletion_status" in existing_cols

        if needs_columns or had_deletion_status_already:
            backup_database(db_path)

        added = _add_missing_columns(conn, existing_cols) if needs_columns else []
        # By this point deletion_status definitely exists (pre-existing or just added).
        normalized = _normalize_legacy_deletion_data(conn, has_deletion_columns=True)
        conn.commit()
        return {"added_columns": added, "normalized_rows": normalized}
