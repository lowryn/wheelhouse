"""First-run matching of tracks already on an iPod to Navidrome songs."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class MatchResult:
    matched: list[tuple]        # (ipod_track_id, navidrome_id, ipod_path)
    ambiguous: list[tuple]
    unmatched_ipod: list[int]   # ipod_track_ids with no Navidrome match
    missing_from_ipod: list[str] # navidrome_ids in selection but absent from iPod


def match(ipod_tracks: list, navidrome_songs: list[dict]) -> MatchResult:
    raise NotImplementedError
