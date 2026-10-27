"""Smoke tests: project imports and iPod fixture sanity."""
from __future__ import annotations

from pathlib import Path


def test_wheelhouse_imports() -> None:
    import wheelhouse.cli
    import wheelhouse.config
    import wheelhouse.device
    import wheelhouse.navidrome
    import wheelhouse.state


def test_ipod_fixture_structure(ipod_root: Path) -> None:
    assert (ipod_root / "iPod_Control/Device/SysInfo").exists()
    assert (ipod_root / "iPod_Control/iTunes").is_dir()


def test_ipod_initialized_fixture(ipod_root_initialized: tuple) -> None:
    root, target, profile = ipod_root_initialized
    db = root / "iPod_Control/iTunes/iTunesDB"
    assert db.exists()
    assert db.stat().st_size > 0

    from iPodDB.library import IPodLibrary
    lib = IPodLibrary.parse(db.read_bytes())
    assert lib.snapshot.tracks == ()


def test_navidrome_fixture(navidrome: dict) -> None:
    import hashlib, json, secrets, urllib.parse, urllib.request
    info = navidrome
    salt = secrets.token_hex(6)
    token = hashlib.md5((info["password"] + salt).encode()).hexdigest()
    qs = urllib.parse.urlencode({
        "u": info["user"], "t": token, "s": salt,
        "v": "1.16.1", "c": "wheelhouse", "f": "json",
    })
    with urllib.request.urlopen(f"{info['url']}/rest/ping.view?{qs}", timeout=5) as r:
        body = json.load(r)["subsonic-response"]
    assert body["status"] == "ok"
