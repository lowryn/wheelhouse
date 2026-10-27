"""Compute desired set vs state; space and track-cap checks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ChangeKind = Literal["add", "remove", "replace_media", "update_meta"]


@dataclass
class Change:
    kind: ChangeKind
    source: str         # 'navidrome' | 'drop'
    source_id: str
    detail: str = ""


@dataclass
class Plan:
    changes: list[Change]
    total_add_bytes: int
    total_remove_bytes: int
    track_count_after: int


def compute_plan(
    *,
    wanted: set[tuple[str, str]],      # (source, source_id)
    known: dict[tuple[str, str], dict], # (source, source_id) -> state row
    free_bytes: int,
    max_tracks: int | None,
) -> Plan:
    raise NotImplementedError
