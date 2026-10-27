"""Shared pytest fixtures: throwaway Navidrome server and simulated iPod filesystem."""
from __future__ import annotations

import hashlib
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

# ---------------------------------------------------------------- helpers

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception:
            time.sleep(0.3)
    raise TimeoutError(f"Server never became ready at {url}")


def _navidrome_bin() -> Path:
    """Return the path to the Navidrome binary, downloading it if needed."""
    cache = Path(os.environ.get("NAVIDROME_CACHE", "~/.cache/wheelhouse-test")).expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    # Navidrome 0.64.2, macOS arm64 — update URL when upgrading
    version = "0.64.2"
    arch = "arm64" if platform.machine() == "arm64" else "amd64"
    bin_name = f"navidrome_{version}_Darwin_{arch}"
    dest = cache / bin_name
    if dest.exists():
        return dest
    url = (
        f"https://github.com/navidrome/navidrome/releases/download/"
        f"v{version}/navidrome_{version}_Darwin_{arch}.tar.gz"
    )
    tgz = cache / (bin_name + ".tar.gz")
    print(f"\nDownloading Navidrome {version} for tests...")
    urllib.request.urlretrieve(url, tgz)
    subprocess.run(["tar", "-xzf", str(tgz), "-C", str(cache)], check=True)
    extracted = cache / "navidrome"
    extracted.rename(dest)
    tgz.unlink(missing_ok=True)
    dest.chmod(0o755)
    return dest


# ---------------------------------------------------------------- Navidrome fixture

@pytest.fixture(scope="session")
def navidrome(tmp_path_factory):
    """A throwaway Navidrome 0.64.2 server running on a random local port."""
    work = tmp_path_factory.mktemp("navidrome")
    music = work / "music"
    music.mkdir()
    data = work / "data"
    data.mkdir()
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    bin_path = _navidrome_bin()
    env = {
        **os.environ,
        "ND_MUSICFOLDER": str(music),
        "ND_DATAFOLDER": str(data),
        "ND_PORT": str(port),
        "ND_LOGLEVEL": "warn",
        "ND_SCANSCHEDULE": "@disabled",
        "ND_ENABLEGRAVATAR": "false",
        "ND_ENABLETRANSCODINGCONFIG": "false",
        "ND_SESSIONTIMEOUT": "24h",
    }
    proc = subprocess.Popen([str(bin_path)], env=env, cwd=str(work),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_http(f"{base_url}/ping", timeout=30)
        # Create admin account
        import json
        payload = json.dumps({"username": "admin", "password": "testpass123"}).encode()
        req = urllib.request.Request(
            f"{base_url}/auth/createAdmin",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        yield {"url": base_url, "user": "admin", "password": "testpass123", "music_dir": music}
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# ---------------------------------------------------------------- simulated iPod fixture

IPOD_MODEL = "MB565"  # 6.5G 120GB black


def _make_ipod_dirs(root: Path) -> None:
    for d in [
        "iPod_Control/iTunes",
        "iPod_Control/Music",
        "iPod_Control/Artwork",
        "iPod_Control/Device",
    ]:
        (root / d).mkdir(parents=True, exist_ok=True)


FAKE_GUID = "000A27001A2B3C4D"


def _write_sysinfo(root: Path, serial: str, model: str = IPOD_MODEL) -> None:
    sysinfo = root / "iPod_Control/Device/SysInfo"
    sysinfo.write_text(
        f"ModelNumStr: {model}\npszSerialNumber: {serial}\n"
        f"VisibleBuildID: 0x00020002\nFirewireGuid: 0x{FAKE_GUID}\n"
    )


@pytest.fixture()
def ipod_root(tmp_path):
    """An empty simulated iPod filesystem (no iTunesDB yet)."""
    _make_ipod_dirs(tmp_path)
    _write_sysinfo(tmp_path, serial="TESTSYNC001")
    return tmp_path


@pytest.fixture()
def ipod_root_initialized(tmp_path):
    """A simulated iPod with an empty iTunesDB already written."""
    from device_registry import DEFAULT_DEVICE_REGISTRY
    from iPodDB.library import WriteTarget, WriteChecksum, create_empty_library
    from iOpenPod.app.artwork_policy import (
        application_artwork_root_value,
        application_cover_formats,
    )

    _make_ipod_dirs(tmp_path)
    _write_sysinfo(tmp_path, serial="TESTSYNC002")

    profile = DEFAULT_DEVICE_REGISTRY.profile_for_model_number(IPOD_MODEL)
    assert profile is not None
    caps = profile.capabilities
    guid = bytes.fromhex(FAKE_GUID)
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
    result = create_empty_library("Test iPod", target, supports_podcasts=True)
    assert result.prepared is not None, result.issues
    db_path = tmp_path / "iPod_Control/iTunes/iTunesDB"
    db_path.write_bytes(result.prepared.itunes)
    return tmp_path, target, profile
