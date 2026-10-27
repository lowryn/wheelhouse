"""Scan the shared drop folder into items."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MediaKind = Literal["music", "audiobook", "movie", "tv_show", "music_video", "note"]

SUBFOLDER_KIND: dict[str, MediaKind] = {
    "Audiobooks": "audiobook",
    "Movies": "movie",
    "TV": "tv_show",
    "Music Videos": "music_video",
    "Notes": "note",
}

AUDIO_SUFFIXES = frozenset({"mp3", "m4a", "m4b", "aac", "wav", "aiff", "aif"})


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
    """Walk drop_folder subfolders and return all audio files as DropItems."""
    items = []
    for folder_name, kind in SUBFOLDER_KIND.items():
        folder = drop_folder / folder_name
        if not folder.exists():
            continue
        for f in sorted(folder.rglob("*")):
            if f.is_file() and f.suffix.lstrip(".").lower() in AUDIO_SUFFIXES:
                st = f.stat()
                items.append(DropItem(
                    rel_path=str(f.relative_to(drop_folder)),
                    kind=kind,
                    size=st.st_size,
                    mtime=st.st_mtime,
                ))
    return items
