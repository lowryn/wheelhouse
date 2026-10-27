"""Load and validate ~/.config/wheelhouse/config.yaml."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

DEFAULT_CONFIG_PATH = Path("~/.config/wheelhouse/config.yaml").expanduser()


@dataclass
class NavidromeConfig:
    url: str
    user: str


@dataclass
class MusicSelection:
    mode: Literal["all"] | list[str]  # "all" or list of playlist globs


@dataclass
class IPodConfig:
    name: str
    serial: str
    music: MusicSelection
    max_tracks: int | None = None


@dataclass
class Config:
    navidrome: NavidromeConfig
    drop_folder: Path
    cache_dir: Path
    ipods: list[IPodConfig] = field(default_factory=list)


def load(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    raise NotImplementedError
