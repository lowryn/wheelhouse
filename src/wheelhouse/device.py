"""Find mounted iPods, identify them, read DB and play counts."""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from device_registry import (
    DEFAULT_DEVICE_REGISTRY,
    DeviceEvidence,
    parse_sysinfo,
    parse_sysinfo_extended,
)
from iPodDB.library import (
    IPodLibrary,
    WriteChecksum,
    WriteTarget,
    create_empty_library,
)
from iOpenPod.app.artwork_policy import (
    application_artwork_root_value,
    application_cover_formats,
)


@dataclass
class MountedIPod:
    root: Path
    serial: str           # from pszSerialNumber or raw SerialNumber fallback
    model: str            # model number, e.g. "MB565"
    display_name: str     # e.g. "iPod Classic 6.5th Gen 120GB Black"
    guid: bytes           # 8 bytes; b"" if unavailable
    firmware: str         # raw firmware version string
    track_count: int
    free_bytes: int
    db_ok: bool
    hash58_valid: bool | None  # None = not a hash58 device or no DB


def find_ipods() -> list[Path]:
    """Return all /Volumes/* paths that look like an iPod mount."""
    volumes = Path("/Volumes")
    if not volumes.exists():
        return []
    return [
        p for p in volumes.iterdir()
        if (p / "iPod_Control/iTunes/iTunesDB").exists()
        or (p / "iPod_Control/Device/SysInfo").exists()
    ]


def _read_evidence(root: Path) -> DeviceEvidence:
    evidence = DeviceEvidence()
    sysinfo = root / "iPod_Control/Device/SysInfo"
    if sysinfo.exists():
        evidence = evidence.merged_with(parse_sysinfo(sysinfo.read_bytes()))
    extended = root / "iPod_Control/Device/SysInfoExtended"
    if extended.exists():
        evidence = evidence.merged_with(parse_sysinfo_extended(extended.read_bytes()))
    return evidence


def _best_value(identifiers: tuple) -> str:
    if not identifiers:
        return ""
    return str(max(identifiers, key=lambda i: i.authority).value)


def _serial_fallback(root: Path) -> str:
    """Raw-parse SysInfo for SerialNumber (some models use that key, not pszSerialNumber)."""
    sysinfo = root / "iPod_Control/Device/SysInfo"
    if not sysinfo.exists():
        return ""
    for line in sysinfo.read_text(errors="replace").splitlines():
        m = re.match(r"(?:pszSerialNumber|SerialNumber)\s*:\s*(\S+)", line, re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


def _guid_bytes(evidence: DeviceEvidence) -> bytes:
    if not evidence.transport_serials:
        return b""
    raw = str(max(evidence.transport_serials, key=lambda i: i.authority).value)
    raw = raw.strip().lower().removeprefix("0x")
    try:
        result = bytes.fromhex(raw)
    except ValueError:
        return b""
    return result if len(result) == 8 else b""


def identify(root: Path) -> MountedIPod:
    evidence = _read_evidence(root)
    result = DEFAULT_DEVICE_REGISTRY.identify(evidence)
    profile = result.profile

    model = _best_value(evidence.model_numbers)
    firmware = _best_value(evidence.firmware_versions)

    if profile is not None:
        display_name = (
            f"{profile.display_name} {profile.advertised_capacity} {profile.finish}"
        )
    elif model:
        display_name = f"Unknown iPod ({model})"
    else:
        display_name = "Unknown iPod"

    serial = _best_value(evidence.product_serials) or _serial_fallback(root)
    guid = _guid_bytes(evidence)

    db_path = root / "iPod_Control/iTunes/iTunesDB"
    db_ok = False
    hash58_valid: bool | None = None
    track_count = 0

    if db_path.exists():
        try:
            db_bytes = db_path.read_bytes()
            lib = IPodLibrary.parse(db_bytes)
            track_count = len(lib.snapshot.tracks)
            db_ok = True
            if (
                profile is not None
                and profile.capabilities.database.checksum.value == "hash58"
                and len(guid) == 8
            ):
                from iPodDB.iTunesDB.writer.signature import verify_hash58
                hash58_valid = verify_hash58(db_bytes, guid)
        except Exception:
            db_ok = False

    free_bytes = shutil.disk_usage(root).free

    return MountedIPod(
        root=root,
        serial=serial,
        model=model,
        display_name=display_name,
        guid=guid,
        firmware=firmware,
        track_count=track_count,
        free_bytes=free_bytes,
        db_ok=db_ok,
        hash58_valid=hash58_valid,
    )


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
