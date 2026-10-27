"""SQLite state: per-iPod managed tracks, play counts, scrobble ledger."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

DEFAULT_STATE_PATH = Path("~/Library/Application Support/wheelhouse/state.sqlite").expanduser()

DDL = """
CREATE TABLE IF NOT EXISTS ipod (
    serial        TEXT PRIMARY KEY,
    guid          BLOB,
    model         TEXT,
    name          TEXT,
    last_seen     INTEGER,
    last_db_sha256 TEXT,
    last_sync     INTEGER
);

CREATE TABLE IF NOT EXISTS item (
    ipod_serial   TEXT    NOT NULL,
    source        TEXT    NOT NULL,   -- 'navidrome' | 'drop'
    source_id     TEXT    NOT NULL,
    ipod_track_id INTEGER,
    ipod_path     TEXT,
    source_sig    TEXT,
    media_kind    TEXT,
    encoding      TEXT,
    play_count    INTEGER NOT NULL DEFAULT 0,
    match_method  TEXT    NOT NULL DEFAULT '',
    adopted_at    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ipod_serial, source, source_id)
);

CREATE TABLE IF NOT EXISTS scrobble_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ipod_serial TEXT    NOT NULL,
    source_id   TEXT    NOT NULL,
    scrobbled_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cache (
    source     TEXT NOT NULL,
    source_id  TEXT NOT NULL,
    source_sig TEXT NOT NULL,
    profile    TEXT NOT NULL,
    path       TEXT NOT NULL,
    size       INTEGER NOT NULL,
    PRIMARY KEY (source, source_id, source_sig, profile)
);
"""

_MIGRATE = """
ALTER TABLE item ADD COLUMN play_count  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE item ADD COLUMN match_method TEXT   NOT NULL DEFAULT '';
ALTER TABLE item ADD COLUMN adopted_at   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ipod ADD COLUMN last_sync   INTEGER;
"""


def open_state(path: Path = DEFAULT_STATE_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(DDL)
    # Best-effort schema migration for older databases
    for stmt in _MIGRATE.strip().splitlines():
        try:
            db.execute(stmt)
        except sqlite3.OperationalError:
            pass
    db.commit()
    return db


# ---------------------------------------------------------------------------
# Managed-item helpers
# ---------------------------------------------------------------------------

@dataclass
class ManagedItem:
    source_id: str       # Navidrome track ID
    ipod_track_id: int
    ipod_path: str
    play_count: int
    match_method: str
    adopted_at: int      # Unix seconds


def upsert_item(
    db: sqlite3.Connection,
    serial: str,
    nd_id: str,
    ipod_track_id: int,
    ipod_path: str,
    play_count: int,
    match_method: str,
    adopted_at: int,
) -> None:
    db.execute(
        """
        INSERT INTO item (ipod_serial, source, source_id, ipod_track_id, ipod_path,
                          play_count, match_method, adopted_at)
        VALUES (?, 'navidrome', ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ipod_serial, source, source_id) DO UPDATE SET
            ipod_track_id = excluded.ipod_track_id,
            ipod_path     = excluded.ipod_path,
            play_count    = excluded.play_count,
            match_method  = excluded.match_method,
            adopted_at    = excluded.adopted_at
        """,
        (serial, nd_id, ipod_track_id, ipod_path, play_count, match_method, adopted_at),
    )


def get_managed(db: sqlite3.Connection, serial: str) -> dict[str, ManagedItem]:
    rows = db.execute(
        "SELECT source_id, ipod_track_id, ipod_path, play_count, match_method, adopted_at "
        "FROM item WHERE ipod_serial = ? AND source = 'navidrome'",
        (serial,),
    ).fetchall()
    return {
        r["source_id"]: ManagedItem(
            source_id=r["source_id"],
            ipod_track_id=r["ipod_track_id"],
            ipod_path=r["ipod_path"] or "",
            play_count=r["play_count"],
            match_method=r["match_method"],
            adopted_at=r["adopted_at"],
        )
        for r in rows
    }


def update_play_count(
    db: sqlite3.Connection, serial: str, nd_id: str, play_count: int
) -> None:
    db.execute(
        "UPDATE item SET play_count = ? "
        "WHERE ipod_serial = ? AND source = 'navidrome' AND source_id = ?",
        (play_count, serial, nd_id),
    )


def remove_item(db: sqlite3.Connection, serial: str, nd_id: str) -> None:
    db.execute(
        "DELETE FROM item WHERE ipod_serial = ? AND source = 'navidrome' AND source_id = ?",
        (serial, nd_id),
    )


def record_scrobble(
    db: sqlite3.Connection, serial: str, nd_id: str, timestamp: int
) -> None:
    db.execute(
        "INSERT INTO scrobble_ledger (ipod_serial, source_id, scrobbled_at) VALUES (?, ?, ?)",
        (serial, nd_id, timestamp),
    )


def upsert_ipod(
    db: sqlite3.Connection,
    serial: str,
    name: str,
    model: str,
    guid: bytes,
    last_seen: int,
) -> None:
    db.execute(
        """
        INSERT INTO ipod (serial, name, model, guid, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(serial) DO UPDATE SET
            name      = excluded.name,
            model     = excluded.model,
            guid      = excluded.guid,
            last_seen = excluded.last_seen
        """,
        (serial, name, model, guid, last_seen),
    )


def set_last_sync(db: sqlite3.Connection, serial: str, timestamp: int) -> None:
    db.execute(
        "UPDATE ipod SET last_sync = ? WHERE serial = ?",
        (timestamp, serial),
    )


def get_last_sync(db: sqlite3.Connection, serial: str) -> int | None:
    row = db.execute(
        "SELECT last_sync FROM ipod WHERE serial = ?", (serial,)
    ).fetchone()
    return row["last_sync"] if row else None
