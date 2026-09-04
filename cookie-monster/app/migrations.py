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

from app import config
from app.deletion_constants import ActionCapability, DeletionMethod, DeletionStatus, RecipeOrigin, RecipeStatus

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
    # Added in Phase 1 (models.py) but missed here at the time - fixed now.
    ("deletion_thread_id", "VARCHAR(100)", None),
    # Phase 2 - response tracking.
    ("deletion_last_response_message_id", "VARCHAR(100)", None),
    ("deletion_response_checked_at", "DATETIME", None),
    ("deletion_response_check_failures", "INTEGER", "0"),
    # 24-hour chase (chase_engine.py).
    ("waiting_on", "VARCHAR(20)", None),
    ("next_followup_at", "DATETIME", None),
    ("followup_attempt", "INTEGER", "0"),
    ("last_followup_at", "DATETIME", None),
    ("followup_locked_at", "DATETIME", None),
    ("followups_paused", "BOOLEAN", "0"),
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
    "submitted" (set only when the user self-reported completing the
    company's own process by hand), and deletion_evidence was a plain
    human-readable sentence.

    IMPORTANT: that old "submitted" is a self-report with no system-level
    proof attached to it. It must NOT be reinterpreted as
    DeletionStatus.COMPLETED or as a SYSTEM-VERIFIED SUBMITTED - either
    would claim more certainty than the old data actually has. It's mapped
    to DeletionStatus.SUBMITTED (the user told us they sent/submitted
    something) with deletion_evidence explicitly tagged
    {"type": "user_reported", "legacy": true, ...} so it's permanently and
    unambiguously distinguishable from a real Gmail-send-verified SUBMITTED
    (which is tagged {"type": "gmail_send", ...}) - see deletion_engine.py
    and the dashboard template, which renders these two cases differently.

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
        new_evidence = evidence

        if status is None:
            new_status = DeletionStatus.NOT_STARTED
        elif status == "submitted":
            new_status = DeletionStatus.SUBMITTED
            new_evidence = json.dumps({
                "type": "user_reported",
                "legacy": True,
                "note": evidence or "User marked this submitted before Cookie Monster tracked evidence.",
            })
        elif status not in DeletionStatus.ALL:
            # Unrecognized legacy value - fall back to a safe known state
            # rather than inventing a meaning for it.
            new_status = DeletionStatus.NOT_STARTED

        if new_evidence == evidence and evidence and not evidence.strip().startswith("{"):
            new_evidence = json.dumps({"type": "user_reported", "note": evidence})
        elif new_evidence is None:
            new_evidence = json.dumps({})

        if new_status != status or new_evidence != evidence:
            conn.execute(
                text(
                    "UPDATE companies SET deletion_status = :status, deletion_evidence = :evidence "
                    "WHERE id = :id"
                ),
                {"status": new_status, "evidence": new_evidence, "id": company_id},
            )
            changed += 1
    return changed


def _backfill_deletion_recipes(conn) -> int:
    """Any Company row that already has a verified deletion method (from
    the pre-DeletionRecipe-table version of this app) gets a matching
    DeletionRecipe row created for its domain, if one doesn't already exist -
    so nothing already resolved is lost or silently re-researched from
    scratch. origin='migrated', confidence='medium' (it wasn't run through
    verify_recipe's official-source check, so it isn't claimed as 'high').
    Idempotent: skips any domain that already has a recipe.
    """
    if "deletion_recipes" not in _existing_tables(conn):
        return 0  # table doesn't exist yet on this connection - create_all() runs before migrate(), but be defensive

    rows = conn.execute(
        text(
            "SELECT domain, deletion_method, deletion_action_capability, deletion_url, deletion_email, "
            "deletion_instructions, deletion_source_url FROM companies "
            "WHERE deletion_verified = 1 AND deletion_method IS NOT NULL AND deletion_method != :unknown"
        ),
        {"unknown": DeletionMethod.UNKNOWN},
    ).fetchall()
    if not rows:
        return 0

    now = datetime.datetime.utcnow()
    now_str = now.isoformat(sep=" ")
    expires_str = (now + datetime.timedelta(days=config.DELETION_RECIPE_FRESHNESS_DAYS)).isoformat(sep=" ")

    inserted = 0
    for domain, method, capability, url, email, instructions, source_url in rows:
        existing = conn.execute(
            text("SELECT id FROM deletion_recipes WHERE domain = :domain"), {"domain": domain}
        ).fetchone()
        if existing is not None:
            continue
        conn.execute(
            text(
                "INSERT INTO deletion_recipes "
                "(domain, method, action_capability, url, email, instructions, required_request_fields, "
                "source_url, confidence, status, origin, recipe_version, verified_at, expires_at, "
                "research_attempts, created_at, updated_at) "
                "VALUES (:domain, :method, :capability, :url, :email, :instructions, '[]', "
                ":source_url, 'medium', :status, :origin, 1, :verified_at, :expires_at, 0, :now, :now)"
            ),
            {
                "domain": domain, "method": method, "capability": capability or ActionCapability.UNKNOWN,
                "url": url, "email": email, "instructions": instructions, "source_url": source_url,
                "status": RecipeStatus.VERIFIED, "origin": RecipeOrigin.MIGRATED,
                "verified_at": now_str, "expires_at": expires_str, "now": now_str,
            },
        )
        inserted += 1
    return inserted


def migrate(engine: Engine, db_path: str) -> dict[str, object]:
    """Adds any missing deletion-tracking columns to an existing `companies`
    table and normalizes legacy deletion_status/deletion_evidence values.
    Returns {"added_columns": [...], "normalized_rows": N}, both empty/zero
    if there was nothing to do (including a brand-new database - create_all()
    creates it fresh with every current column, so no migration applies)."""
    with engine.connect() as conn:
        if "companies" not in _existing_tables(conn):
            return {"added_columns": [], "normalized_rows": 0, "backfilled_recipes": 0}

        existing_cols = _existing_columns(conn, "companies")
        needs_columns = any(c[0] not in existing_cols for c in NEW_DELETION_COLUMNS)
        # deletion_status/deletion_evidence predate this migration in some existing
        # databases (added by an earlier version of this app) - normalize legacy
        # values in them whenever the column is present, not only when we just added it.
        had_deletion_status_already = "deletion_status" in existing_cols

        # Only back up if there's actual data at risk. On a brand-new database,
        # create_all() runs before migrate() (see db.py) and already creates
        # `companies` with every current column, including deletion_status -
        # that alone doesn't mean there's a pre-existing database to protect,
        # so check for rows too, or this would back up an empty file on every
        # fresh install.
        (row_count,) = conn.exec_driver_sql("SELECT COUNT(*) FROM companies").fetchone()
        if needs_columns or (had_deletion_status_already and row_count > 0):
            backup_database(db_path)

        added = _add_missing_columns(conn, existing_cols) if needs_columns else []
        # By this point deletion_status definitely exists (pre-existing or just added).
        normalized = _normalize_legacy_deletion_data(conn, has_deletion_columns=True)
        backfilled = _backfill_deletion_recipes(conn)
        conn.commit()
        return {"added_columns": added, "normalized_rows": normalized, "backfilled_recipes": backfilled}
