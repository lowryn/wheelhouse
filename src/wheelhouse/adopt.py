"""First-run matching of tracks already on an iPod to Navidrome songs."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

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
