from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    sql = resources.files("photoindex").joinpath("schema.sql").read_text()
    conn.executescript(sql)
    conn.commit()


def upsert_disk(conn: sqlite3.Connection, label: str, volume_uuid: str | None = None) -> int:
    now = now_iso()
    cur = conn.execute(
        """
        INSERT INTO disks (label, volume_uuid, first_seen, last_seen)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(label) DO UPDATE SET
            last_seen = excluded.last_seen,
            volume_uuid = COALESCE(excluded.volume_uuid, disks.volume_uuid)
        """,
        (label, volume_uuid, now, now),
    )
    if cur.lastrowid:
        return cur.lastrowid
    row = conn.execute("SELECT id FROM disks WHERE label = ?", (label,)).fetchone()
    return row["id"]
