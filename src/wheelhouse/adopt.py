"""First-run matching of tracks already on an iPod to Navidrome songs."""
from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from iPodDB.library import Track


@dataclass
class MatchResult:
    matched: list[tuple]         # (ipod_track_id, navidrome_id, ipod_path, method)
    ambiguous: list[tuple]       # (ipod_track_id, ipod_title, [candidate_nd_ids])
    unmatched_ipod: list[int]    # ipod_track_ids with no Navidrome match
    missing_from_ipod: list[str] # navidrome_ids in selection but absent from iPod


def _norm(s: str) -> str:
    """Lowercase, strip accents, collapse whitespace, keep alnum+space.

    & → 'and' before punctuation removal so 'Tom Petty & the Heartbreakers'
    and 'Tom Petty and the Heartbreakers' produce the same key.
    """
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"\s*&\s*", " and ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _duration_close(ipod_ms: int, nd_seconds: float, tolerance_s: int = 3) -> bool:
    # Navidrome reports integer seconds (truncated), so allow 3s to absorb that plus
    # minor encoder differences
    return abs(ipod_ms / 1000 - nd_seconds) <= tolerance_s


def match(ipod_tracks: list[Track], navidrome_songs: list[dict]) -> MatchResult:
    """
    Primary key:  (album_artist, album, disc, track_number, title) + duration ±3s
    Fallback key: (artist, title) + duration ±3s, only when unique
    Stripped key: if title is "X - Y", try (album, disc, track_number, Y) + duration ±3s, only when unique
                  handles tracks where the artist name was embedded in the title field
    """
    # Build Navidrome lookup indexes
    primary_index: dict[tuple, list[dict]] = {}
    fallback_index: dict[tuple, list[dict]] = {}
    stripped_index: dict[tuple, list[dict]] = {}

    for song in navidrome_songs:
        aa    = _norm(song.get("displayAlbumArtist") or song.get("artist", ""))
        alb   = _norm(song.get("album", ""))
        disc  = int(song.get("discNumber") or 1)
        trk   = int(song.get("track") or 0)
        title = _norm(song.get("title", ""))
        art   = _norm(song.get("artist", ""))

        primary_index.setdefault((aa, alb, disc, trk, title), []).append(song)
        fallback_index.setdefault((art, title), []).append(song)
        stripped_index.setdefault((alb, disc, trk, title), []).append(song)

    matched:         list[tuple] = []
    ambiguous:       list[tuple] = []
    unmatched_ipod:  list[int]   = []
    matched_nd_ids:  set[str]    = set()

    for t in ipod_tracks:
        aa    = _norm(t.album_artist or t.artist)
        alb   = _norm(t.album)
        disc  = int(t.metadata.disc_number or 1)
        trk   = int(t.track_number or 0)
        title = _norm(t.title)
        art   = _norm(t.artist)

        pk = (aa, alb, disc, trk, title)
        candidates = [
            s for s in primary_index.get(pk, [])
            if _duration_close(t.length_ms, s.get("duration", 0))
        ]
        method = "primary"

        if not candidates:
            fk = (art, title)
            candidates = [
                s for s in fallback_index.get(fk, [])
                if _duration_close(t.length_ms, s.get("duration", 0))
            ]
            method = "fallback"

        if not candidates:
            m = re.match(r"^.+? - (.+)$", t.title or "")
            if m:
                sk = (alb, disc, trk, _norm(m.group(1)))
                candidates = [
                    s for s in stripped_index.get(sk, [])
                    if _duration_close(t.length_ms, s.get("duration", 0))
                ]
                method = "stripped"

        if len(candidates) == 1:
            nd = candidates[0]
            matched.append((t.track_id, nd["id"], t.metadata.location, method))
            matched_nd_ids.add(nd["id"])
        elif len(candidates) > 1:
            ambiguous.append((t.track_id, t.title, [s["id"] for s in candidates]))
        else:
            unmatched_ipod.append(t.track_id)

    wanted_ids = {s["id"] for s in navidrome_songs}
    missing_from_ipod = sorted(wanted_ids - matched_nd_ids)

    return MatchResult(
        matched=matched,
        ambiguous=ambiguous,
        unmatched_ipod=unmatched_ipod,
        missing_from_ipod=missing_from_ipod,
    )


@dataclass
class AdoptPlan:
    target: object           # MountedIPod (post config overrides)
    ipod_tracks: list        # list[Track]
    all_songs: list[dict]
    desired_songs: list[dict]
    result: MatchResult


def plan(target, ipod_cfg, nd) -> AdoptPlan:
    """Build an adopt plan: load library, fetch Navidrome, run the matcher.

    Does not touch state or write to the iPod.
    """
    from wheelhouse.device import apply_config_overrides, build_write_target, load_library

    target = apply_config_overrides(target, ipod_cfg)
    wt, _profile = build_write_target(target.model, target.guid)
    lib = load_library(target.root, wt)
    ipod_tracks = list(lib.snapshot.tracks)

    all_songs = nd.all_songs()
    if ipod_cfg.music.mode == "all":
        desired_songs = all_songs
    else:
        desired_ids: set[str] = set()
        for pl in nd.playlists():
            if ipod_cfg.music.matches_playlist(pl["name"]):
                for entry in nd.playlist_songs(pl["id"]):
                    desired_ids.add(entry["id"])
        by_id = {s["id"]: s for s in all_songs}
        desired_songs = [by_id[i] for i in desired_ids if i in by_id]

    result = match(ipod_tracks, desired_songs)
    return AdoptPlan(target, ipod_tracks, all_songs, desired_songs, result)


def save(plan: AdoptPlan, ipod_cfg) -> int:
    """Persist a matched plan to state. Returns the count saved."""
    from wheelhouse import state as state_mod

    db = state_mod.open_state()
    state_mod.upsert_ipod(
        db, plan.target.serial, ipod_cfg.name, plan.target.model, plan.target.guid,
        int(time.time()),
    )
    now = int(time.time())
    ipod_by_id = {t.track_id: t for t in plan.ipod_tracks}
    for (tid, nd_id, path, method) in plan.result.matched:
        t = ipod_by_id[tid]
        state_mod.upsert_item(
            db, plan.target.serial, nd_id,
            ipod_track_id=tid,
            ipod_path=path or "",
            play_count=t.play_count,
            match_method=method,
            adopted_at=now,
        )
    db.commit()
    db.close()
    return len(plan.result.matched)


def write_csv(plan: AdoptPlan, out_path: Path) -> None:
    """Write the adopt match report as CSV for human review."""
    import csv

    by_id = {s["id"]: s for s in plan.all_songs}
    ipod_by_id = {t.track_id: t for t in plan.ipod_tracks}
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "status", "method", "ipod_track_id", "ipod_title", "ipod_artist",
            "ipod_album", "nd_id", "nd_title", "nd_artist", "nd_album", "ipod_path",
        ])
        for (tid, nd_id, path, method) in plan.result.matched:
            t = ipod_by_id[tid]
            s = by_id.get(nd_id, {})
            w.writerow(["matched", method, tid, t.title, t.artist, t.album,
                        nd_id, s.get("title", ""), s.get("artist", ""),
                        s.get("album", ""), path])
        for (tid, title, cands) in plan.result.ambiguous:
            t = ipod_by_id[tid]
            w.writerow(["ambiguous", "", tid, t.title, t.artist, t.album,
                        "|".join(cands), "", "", "", t.metadata.location])
        for tid in plan.result.unmatched_ipod:
            t = ipod_by_id[tid]
            w.writerow(["unmatched_ipod", "", tid, t.title, t.artist, t.album,
                        "", "", "", "", t.metadata.location])
        for nd_id in plan.result.missing_from_ipod:
            s = by_id.get(nd_id, {})
            w.writerow(["missing_from_ipod", "", "", "", "", "",
                        nd_id, s.get("title", ""), s.get("artist", ""),
                        s.get("album", ""), ""])
