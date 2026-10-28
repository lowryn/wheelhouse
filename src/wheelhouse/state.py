"""SQLite state: per-iPod managed tracks, play counts, scrobble ledger."""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path


def nd_sig(song: dict) -> str:
    """Stable short hash of the Navidrome fields that drive iPod display.

    Used to detect when Navidrome metadata or file content has changed since
    the last sync and the on-iPod copy needs to be refreshed.
    """
    fields = (
        song.get("size") or 0,
        song.get("suffix") or "",
        song.get("title") or "",
        song.get("artist") or "",
        song.get("albumArtist") or song.get("displayAlbumArtist") or "",
        song.get("album") or "",
        song.get("year") or song.get("date") or "",
        song.get("trackNumber") or song.get("track") or "",
        song.get("discNumber") or song.get("disc") or "",
        song.get("genre") or "",
    )
    return hashlib.sha1("|".join(str(f) for f in fields).encode()).hexdigest()[:16]

DEFAULT_STATE_PATH = Path("~/Library/Application Support/wheelhouse/state.sqlite").expanduser()

DDL = """
CREATE TABLE IF NOT EXISTS ipod (
    serial        TEXT PRIMARY KEY,
    guid          BLOB,
    model         TEXT,
    name          TEXT,
    last_seen     INTEGER,
    last_db_sha256 TEXT,
    last_sync     INTEGER,
    last_playlists TEXT
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

# Schema additions to apply on databases created before the column existed.
# SQLite has no `ALTER TABLE ADD COLUMN IF NOT EXISTS`; we catch the
# "duplicate column name" OperationalError that signals the column is already
# present. Any other OperationalError (e.g. missing table) is re-raised so a
# genuine schema problem surfaces instead of being swallowed.
_MIGRATE = (
    "ALTER TABLE item ADD COLUMN play_count   INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE item ADD COLUMN match_method TEXT    NOT NULL DEFAULT ''",
    "ALTER TABLE item ADD COLUMN adopted_at   INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE item ADD COLUMN source_sig   TEXT    NOT NULL DEFAULT ''",
    "ALTER TABLE ipod ADD COLUMN last_sync      INTEGER",
    "ALTER TABLE ipod ADD COLUMN last_playlists TEXT",
)


def open_state(path: Path = DEFAULT_STATE_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(DDL)
    for stmt in _MIGRATE:
        try:
            db.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise
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
    source_sig: str = ""  # hash of Navidrome metadata; empty = not yet recorded


def upsert_item(
    db: sqlite3.Connection,
    serial: str,
    nd_id: str,
    ipod_track_id: int,
    ipod_path: str,
    play_count: int,
    match_method: str,
    adopted_at: int,
    source_sig: str = "",
) -> None:
    db.execute(
        """
        INSERT INTO item (ipod_serial, source, source_id, ipod_track_id, ipod_path,
                          play_count, match_method, adopted_at, source_sig)
        VALUES (?, 'navidrome', ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ipod_serial, source, source_id) DO UPDATE SET
            ipod_track_id = excluded.ipod_track_id,
            ipod_path     = excluded.ipod_path,
            play_count    = excluded.play_count,
            match_method  = excluded.match_method,
            adopted_at    = excluded.adopted_at,
            source_sig    = excluded.source_sig
        """,
        (serial, nd_id, ipod_track_id, ipod_path, play_count, match_method, adopted_at, source_sig),
    )


def get_managed(db: sqlite3.Connection, serial: str) -> dict[str, ManagedItem]:
    rows = db.execute(
        "SELECT source_id, ipod_track_id, ipod_path, play_count, match_method, adopted_at, source_sig "
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
            source_sig=r["source_sig"] or "",
        )
        for r in rows
    }


def bulk_set_source_sig(
    db: sqlite3.Connection, serial: str, sigs: dict[str, str]
) -> None:
    """Set source_sig for multiple navidrome items in one shot (backfill)."""
    db.executemany(
        "UPDATE item SET source_sig = ? "
        "WHERE ipod_serial = ? AND source = 'navidrome' AND source_id = ? AND (source_sig IS NULL OR source_sig = '')",
        [(sig, serial, nd_id) for nd_id, sig in sigs.items()],
    )


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


def is_adopted(db: sqlite3.Connection, serial: str) -> bool:
    return db.execute(
        "SELECT 1 FROM ipod WHERE serial = ?", (serial,)
    ).fetchone() is not None


def set_last_sync(db: sqlite3.Connection, serial: str, timestamp: int) -> None:
    db.execute(
        "UPDATE ipod SET last_sync = ? WHERE serial = ?",
        (timestamp, serial),
    )


@dataclass
class DropStateItem:
    rel_path: str
    ipod_track_id: int
    ipod_path: str
    source_sig: str
    adopted_at: int


def get_drop_managed(db: sqlite3.Connection, serial: str) -> dict[str, DropStateItem]:
    rows = db.execute(
        "SELECT source_id, ipod_track_id, ipod_path, source_sig, adopted_at "
        "FROM item WHERE ipod_serial = ? AND source = 'drop'",
        (serial,),
    ).fetchall()
    return {
        r["source_id"]: DropStateItem(
            rel_path=r["source_id"],
            ipod_track_id=r["ipod_track_id"] or 0,
            ipod_path=r["ipod_path"] or "",
            source_sig=r["source_sig"] or "",
            adopted_at=r["adopted_at"] or 0,
        )
        for r in rows
    }


def upsert_drop_item(
    db: sqlite3.Connection,
    serial: str,
    rel_path: str,
    source_sig: str,
    ipod_track_id: int,
    ipod_path: str,
    adopted_at: int,
) -> None:
    db.execute(
        """
        INSERT INTO item (ipod_serial, source, source_id, source_sig, ipod_track_id, ipod_path, adopted_at)
        VALUES (?, 'drop', ?, ?, ?, ?, ?)
        ON CONFLICT(ipod_serial, source, source_id) DO UPDATE SET
            source_sig    = excluded.source_sig,
            ipod_track_id = excluded.ipod_track_id,
            ipod_path     = excluded.ipod_path,
            adopted_at    = excluded.adopted_at
        """,
        (serial, rel_path, source_sig, ipod_track_id, ipod_path, adopted_at),
    )


def remove_drop_item(db: sqlite3.Connection, serial: str, rel_path: str) -> None:
    db.execute(
        "DELETE FROM item WHERE ipod_serial = ? AND source = 'drop' AND source_id = ?",
        (serial, rel_path),
    )


def get_last_sync(db: sqlite3.Connection, serial: str) -> int | None:
    row = db.execute(
        "SELECT last_sync FROM ipod WHERE serial = ?", (serial,)
    ).fetchone()
    return row["last_sync"] if row else None


def set_last_playlists(db: sqlite3.Connection, serial: str, playlists: list[str]) -> None:
    import json
    db.execute(
        "UPDATE ipod SET last_playlists = ? WHERE serial = ?",
        (json.dumps(playlists), serial),
    )


def get_last_playlists(db: sqlite3.Connection, serial: str) -> list[str] | None:
    """Returns None if never synced, [] if last sync was 'all music', or a list of names."""
    import json
    row = db.execute(
        "SELECT last_playlists FROM ipod WHERE serial = ?", (serial,)
    ).fetchone()
    if row is None or row["last_playlists"] is None:
        return None
    return json.loads(row["last_playlists"])
