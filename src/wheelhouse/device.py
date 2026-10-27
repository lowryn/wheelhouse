"""Find mounted iPods, identify them, read DB and play counts."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from device_registry import DEFAULT_DEVICE_REGISTRY, parse_sysinfo, parse_sysinfo_extended
from iPodDB.library import IPodLibrary, WriteTarget, WriteChecksum, create_empty_library
from iOpenPod.app.artwork_policy import (
    application_artwork_root_value,
    application_cover_formats,
)


@dataclass
class MountedIPod:
    root: Path
    serial: str
    model: str
    name: str
    guid: bytes
    firmware_version: str
    library: IPodLibrary
    write_target: WriteTarget


def find_ipods() -> list[Path]:
    """Return all /Volumes/* paths that look like an iPod mount."""
    volumes = Path("/Volumes")
    return [
        p for p in volumes.iterdir()
        if (p / "iPod_Control/iTunes/iTunesDB").exists()
        or (p / "iPod_Control/Device/SysInfo").exists()
    ]


def identify(root: Path) -> MountedIPod:
    raise NotImplementedError


def load_library(root: Path, write_target: WriteTarget) -> IPodLibrary:
    db_path = root / "iPod_Control/iTunes/iTunesDB"
    art_path = root / "iPod_Control/Artwork/ArtworkDB"
    if db_path.exists():
        lib = IPodLibrary.parse(db_path.read_bytes())
        if art_path.exists():
            lib = lib.with_artwork(art_path.read_bytes())
        return lib
    result = create_empty_library("iPod", write_target, supports_podcasts=True)
    assert result.prepared is not None, result.issues
    return IPodLibrary.parse(result.prepared.itunes)


def build_write_target(model: str, guid: bytes) -> tuple[WriteTarget, object]:
    profile = DEFAULT_DEVICE_REGISTRY.profile_for_model_number(model)
    if profile is None:
        raise ValueError(f"Unknown iPod model: {model}")
    caps = profile.capabilities
    target = WriteTarget(
        checksum=WriteChecksum(caps.database.checksum.value),
        database_version=caps.database.binary_version,
        firewire_guid=guid,
        max_database_bytes=caps.database.max_database_bytes,
        cover_formats=application_cover_formats(profile),
        artwork_root_value=application_artwork_root_value(profile),
        include_auxiliary_image_records=caps.artwork.include_auxiliary_image_records,
        supports_sparse_artwork=caps.artwork.supports_sparse_artwork,
    )
    return target, profile
