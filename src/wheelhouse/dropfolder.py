"""Scan the shared drop folder into items."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MediaKind = Literal["music", "audiobook", "movie", "tv_show", "music_video", "note"]

SUBFOLDER_KIND: dict[str, MediaKind] = {
    "Music": "music",
    "Audiobooks": "audiobook",
    "Movies": "movie",
    "TV": "tv_show",
    "Music Videos": "music_video",
    "Notes": "note",
}


@dataclass
class DropItem:
    rel_path: str       # relative to drop_folder root
    kind: MediaKind
    size: int
    mtime: float

    @property
    def source_sig(self) -> str:
        return f"{self.size}:{self.mtime}"


def scan(drop_folder: Path) -> list[DropItem]:
    raise NotImplementedError
