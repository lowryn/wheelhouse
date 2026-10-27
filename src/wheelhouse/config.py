"""Load and validate ~/.config/wheelhouse/config.yaml."""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

import keyring
import yaml

DEFAULT_CONFIG_PATH = Path("~/.config/wheelhouse/config.yaml").expanduser()
KEYRING_SERVICE = "wheelhouse"


@dataclass
class NavidromeConfig:
    url: str
    user: str

    def password(self) -> str:
        pw = keyring.get_password(KEYRING_SERVICE, self.user)
        if pw is None:
            raise RuntimeError(
                f"No password in Keychain for {self.user!r}. "
                f"Run: python -c \"import keyring; keyring.set_password"
                f"('{KEYRING_SERVICE}', '{self.user}', input('Password: '))\""
            )
        return pw


@dataclass
class MusicSelection:
    """Either 'all' tracks or a list of playlist name globs."""
    mode: str  # "all" or "playlists"
    playlists: list[str] = field(default_factory=list)

    def matches_playlist(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name, pat) for pat in self.playlists)


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

    def ipod_by_serial(self, serial: str) -> IPodConfig | None:
        return next((i for i in self.ipods if i.serial == serial), None)


def _parse_music(raw) -> MusicSelection:
    if raw == "all" or raw is None:
        return MusicSelection(mode="all")
    if isinstance(raw, dict):
        pls = raw.get("playlists", [])
        if isinstance(pls, str):
            pls = [pls]
        return MusicSelection(mode="playlists", playlists=list(pls))
    raise ValueError(f"Invalid music selection: {raw!r}")


def append_ipod_to_config(
    name: str, serial: str, music_all: bool, path: Path = DEFAULT_CONFIG_PATH
) -> None:
    """Append a new iPod entry to config.yaml."""
    text = path.read_text()
    prefix = "" if text.endswith("\n") else "\n"
    lines = [f"{prefix}  - name: {name}\n", f"    serial: {serial}\n"]
    if music_all:
        lines.append("    music: all\n")
    else:
        lines.append("    music:\n      playlists: []\n")
    with open(path, "a") as f:
        f.writelines(lines)


def load(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    with open(path) as f:
        data = yaml.safe_load(f)

    nd = data["navidrome"]
    ipods = []
    for raw in data.get("ipods", []):
        ipods.append(IPodConfig(
            name=raw["name"],
            serial=raw["serial"],
            music=_parse_music(raw.get("music", "all")),
            max_tracks=raw.get("max_tracks"),
        ))

    return Config(
        navidrome=NavidromeConfig(url=nd["url"].rstrip("/"), user=nd["user"]),
        drop_folder=Path(data.get("drop_folder", "~/Wheelhouse")).expanduser(),
        cache_dir=Path(data.get("cache_dir", "~/Library/Caches/wheelhouse")).expanduser(),
        ipods=ipods,
    )
