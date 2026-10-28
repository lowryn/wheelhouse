"""Scrobble new plays from iPod to Navidrome.

Partial-failure safe: play_count in state only advances by the number of
scrobbles that actually succeeded. Any remaining plays are retried on the
next sync, since the iPod's play_count hasn't changed and the state value
still lags.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from wheelhouse import state as state_mod


@dataclass
class ScrobbleReport:
    scrobbled: int
    failed: int


def push_plays(
    db: sqlite3.Connection,
    nd,
    serial: str,
    managed: dict,
    ipod_by_tid: dict,
    *,
    dry_run: bool = False,
) -> ScrobbleReport:
    """Submit newly-played tracks to Navidrome; advance state only on success."""
    scrobbled = 0
    failed = 0
    now_ms = int(time.time() * 1000)

    for nd_id, item in managed.items():
        t = ipod_by_tid.get(item.ipod_track_id)
        if t is None or t.play_count <= item.play_count:
            continue
        delta = t.play_count - item.play_count

        if dry_run:
            scrobbled += delta
            continue

        ok = 0
        for _ in range(delta):
            try:
                nd.scrobble(nd_id, now_ms)
                state_mod.record_scrobble(db, serial, nd_id, int(time.time()))
                ok += 1
            except Exception:
                # Stop hammering a failing server; remaining plays will retry
                # next sync (iPod play_count unchanged, state still lags).
                break
        if ok > 0:
            new_count = item.play_count + ok
            state_mod.update_play_count(db, serial, nd_id, new_count)
            managed[nd_id].play_count = new_count
        scrobbled += ok
        failed += (delta - ok)

    return ScrobbleReport(scrobbled=scrobbled, failed=failed)
