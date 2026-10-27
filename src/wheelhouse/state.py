"""SQLite state: per-iPod items, scrobble ledger, conversion cache."""
from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_STATE_PATH = Path("~/Library/Application Support/wheelhouse/state.sqlite").expanduser()

DDL = """
CREATE TABLE IF NOT EXISTS ipod (
    serial        TEXT PRIMARY KEY,
    guid          BLOB,
    model         TEXT,
    name          TEXT,
    last_seen     INTEGER,
    last_db_sha256 TEXT
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
    PRIMARY KEY (ipod_serial, source, source_id)
);

CREATE TABLE IF NOT EXISTS scrobble_ledger (
    ipod_serial       TEXT    NOT NULL,
    play_counts_sha256 TEXT   NOT NULL,
    row               INTEGER NOT NULL,
    track_id          INTEGER NOT NULL,
    sent_at           INTEGER NOT NULL,
    PRIMARY KEY (ipod_serial, play_counts_sha256, row)
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


def open_state(path: Path = DEFAULT_STATE_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(DDL)
    db.commit()
    return db
