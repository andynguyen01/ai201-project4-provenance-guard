"""Structured audit log backed by SQLite.

Every attribution decision and every appeal is recorded here. This is the
canonical record the README and the GET /log endpoint surface, and the store
the appeal workflow reads/updates in Milestone 5.

The schema already includes the columns later milestones need (stylometric
score, signals_used, appeal fields) so we never have to ALTER TABLE; Milestone 3
simply leaves the not-yet-computed ones NULL.
"""

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "provenance_guard.db"

# Columns written by record_entry, in order.
_COLUMNS = [
    "content_id",
    "creator_id",
    "text",
    "timestamp",
    "action",              # "classified" | "appeal"
    "content_type",        # "text" | "image_metadata"  (stretch: multi-modal)
    "attribution_result",  # likely_ai | uncertain | likely_human
    "confidence_score",
    "llm_score",
    "stylometric_score",
    "lexical_score",       # stretch: ensemble detection (3rd signal)
    "signals_used",        # JSON-encoded list, e.g. ["llm", "stylometric", "lexical"]
    "transparency_label",
    "status",              # classified | under_review
    "appeal_reasoning",
]

# Columns added after the original schema shipped — migrated in on existing DBs.
_MIGRATIONS = [
    ("content_type", "TEXT"),
    ("lexical_score", "REAL"),
]


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create the audit_log table if needed, and migrate in any new columns."""
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                content_id          TEXT NOT NULL,
                creator_id          TEXT,
                text                TEXT,
                timestamp           TEXT NOT NULL,
                action              TEXT NOT NULL DEFAULT 'classified',
                content_type        TEXT,
                attribution_result  TEXT,
                confidence_score    REAL,
                llm_score           REAL,
                stylometric_score   REAL,
                lexical_score       REAL,
                signals_used        TEXT,
                transparency_label  TEXT,
                status              TEXT,
                appeal_reasoning    TEXT
            )
            """
        )
        # Migrate older databases that predate the stretch-feature columns.
        existing = {row[1] for row in conn.execute("PRAGMA table_info(audit_log)")}
        for column, decl in _MIGRATIONS:
            if column not in existing:
                conn.execute(f"ALTER TABLE audit_log ADD COLUMN {column} {decl}")
        conn.commit()


def record_entry(entry):
    """Insert one structured entry. `signals_used` may be a Python list."""
    values = []
    for col in _COLUMNS:
        value = entry.get(col)
        if col == "signals_used" and value is not None:
            value = json.dumps(value)
        values.append(value)
    placeholders = ", ".join("?" for _ in _COLUMNS)
    columns = ", ".join(_COLUMNS)
    with get_connection() as conn:
        conn.execute(
            f"INSERT INTO audit_log ({columns}) VALUES ({placeholders})", values
        )
        conn.commit()


def _row_to_dict(row):
    record = dict(row)
    if record.get("signals_used"):
        try:
            record["signals_used"] = json.loads(record["signals_used"])
        except (ValueError, TypeError):
            pass
    return record


def get_log(limit=50):
    """Return the most recent entries (newest first) as a list of dicts."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_classification(content_id):
    """Return the original classification entry for a content_id, or None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM audit_log WHERE content_id = ? AND action = 'classified' "
            "ORDER BY id DESC LIMIT 1",
            (content_id,),
        ).fetchone()
    return _row_to_dict(row) if row is not None else None


def update_status(content_id, status):
    """Update the status of a content's classification entry (e.g. for appeals)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE audit_log SET status = ? WHERE content_id = ? AND action = 'classified'",
            (status, content_id),
        )
        conn.commit()