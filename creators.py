"""Creator status store for the provenance certificate (stretch feature).

A creator earns a "verified human" credential by passing a writing-challenge
check (see /verify in app.py). The credential is account-level reputation — it
says the account once demonstrated human writing, NOT that every later
submission is unassisted. Stored in the same SQLite database as the audit log.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "provenance_guard.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS creator_status (
                creator_id   TEXT PRIMARY KEY,
                verified     INTEGER NOT NULL DEFAULT 0,
                verified_at  TEXT
            )
            """
        )
        conn.commit()


def set_verified(creator_id, verified_at):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO creator_status (creator_id, verified, verified_at) "
            "VALUES (?, 1, ?) "
            "ON CONFLICT(creator_id) DO UPDATE SET verified = 1, verified_at = excluded.verified_at",
            (creator_id, verified_at),
        )
        conn.commit()


def get_status(creator_id):
    """Return {"verified": bool, "verified_at": str|None} for a creator."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT verified, verified_at FROM creator_status WHERE creator_id = ?",
            (creator_id,),
        ).fetchone()
    if row is None:
        return {"verified": False, "verified_at": None}
    return {"verified": bool(row["verified"]), "verified_at": row["verified_at"]}
