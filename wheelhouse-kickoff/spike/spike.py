"""Spike: Navidrome (Subsonic API) -> iPod Classic iTunesDB using iOpenPod's iPodDB.

Not production code. Proves:
  1. iPodDB can be driven headless from a non-folder source.
  2. Persistent per-iPod state (Navidrome id <-> iPod track id) gives incremental syncs.
  3. Play Counts on the iPod can be mapped back and scrobbled to Navidrome.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import secrets
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path

from PIL import Image

from device_registry import DEFAULT_DEVICE_REGISTRY
from iOpenPod.app.artwork_policy import (
    application_artwork_root_value,
    application_cover_formats,
)
from iPodDB.iTunesDB.writer.signature import verify_hash58
from iPodDB.library import (
    ArtworkAsset,
    ArtworkPixels,
    AudioEncoding,
    FileDependency,
    IPodLibrary,
    Playlist,
    PlaylistKind,
    SourceFile,
    Track,
    TrackMetadata,
    WriteChecksum,
    WriteResources,
    WriteTarget,
    content_sha256,
    create_empty_library,
    playlist_entries,
    prepared_audio,
)
from iPodDB.sidecars.codec import parse_play_counts

MAC_EPOCH_OFFSET = 2082844800  # spike only: real tool must use the device timezone
FAKE_GUID = bytes.fromhex("000A27001A2B3C4D")


# ---------------------------------------------------------------- Subsonic
class Subsonic:
    def __init__(self, url: str, user: str, password: str, client: str = "ipodsync"):
        self.url, self.user, self.password, self.client = url.rstrip("/"), user, password, client

    def _q(self, endpoint: str, **params: object) -> str:
        salt = secrets.token_hex(6)
        token = hashlib.md5((self.password + salt).encode()).hexdigest()
        base = {"u": self.user, "t": token, "s": salt, "v": "1.16.1", "c": self.client, "f": "json"}
        base.update({k: str(v) for k, v in params.items() if v is not None})
        return f"{self.url}/rest/{endpoint}.view?{urllib.parse.urlencode(base)}"

    def get(self, endpoint: str, **params: object) -> dict:
        with urllib.request.urlopen(self._q(endpoint, **params), timeout=30) as r:
            body = json.load(r)["subsonic-response"]
        if body.get("status") != "ok":
            raise RuntimeError(f"{endpoint}: {body.get('error')}")
        return body

    def raw(self, endpoint: str, **params: object) -> bytes:
        with urllib.request.urlopen(self._q(endpoint, **params), timeout=120) as r:
            return r.read()

    def all_songs(self, page: int = 500) -> list[dict]:
        songs, offset = [], 0
        while True:
            res = self.get("search3", query="", songCount=page, songOffset=offset,
                           artistCount=0, albumCount=0)["searchResult3"].get("song", [])
            songs += res
            if len(res) < page:
                return songs
            offset += page

    def playlist_by_name(self, name: str) -> list[dict]:
        for pl in self.get("getPlaylists")["playlists"].get("playlist", []):
            if pl["name"] == name:
                return self.get("getPlaylist", id=pl["id"])["playlist"].get("entry", [])
        raise KeyError(name)


# ---------------------------------------------------------------- state
def open_state(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("""create table if not exists track(
        nd_id text primary key, ipod_track_id integer, ipod_path text,
        source_sig text, encoding text)""")
    return db


# ---------------------------------------------------------------- media
def fetch_source(sub: Subsonic, song: dict, path_map: tuple[str, str] | None, work: Path) -> tuple[Path, str]:
    """Prefer the real file via a mounted share; fall back to Subsonic download."""
    if path_map and song.get("path", "").startswith(path_map[0]):
        local = Path(path_map[1] + song["path"][len(path_map[0]):])
        if local.is_file():
            return local, "share"
    dest = work / f"{song['id']}.{song['suffix']}"
    dest.write_bytes(sub.raw("download", id=song["id"]))
    return dest, "download"


def prepare_audio(src: Path, suffix: str, work: Path) -> tuple[Path, AudioEncoding, str]:
    if suffix == "mp3":
        return src, AudioEncoding.MP3, ".mp3"
    if suffix in ("m4a", "aac"):
        codec = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                                "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(src)],
                               capture_output=True, text=True, check=True).stdout.strip()
        if codec in ("aac", "alac"):
            return src, AudioEncoding.ALAC if codec == "alac" else AudioEncoding.AAC, ".m4a"
    out = work / (src.stem + ".alac.m4a")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-map", "0:a",
                    "-c:a", "alac", "-movflags", "+faststart", str(out)], check=True)
    return out, AudioEncoding.ALAC, ".m4a"


def cover_pixels(sub: Subsonic, cover_id: str | None) -> ArtworkPixels | None:
    if not cover_id:
        return None
    img = Image.open(io.BytesIO(sub.raw("getCoverArt", id=cover_id, size=600))).convert("RGB")
    return ArtworkPixels(img.width, img.height, img.tobytes())


# ---------------------------------------------------------------- iPod
def device_target(model: str, guid: bytes) -> WriteTarget:
    profile = DEFAULT_DEVICE_REGISTRY.profile_for_model_number(model)
    assert profile is not None, model
    caps = profile.capabilities
    return WriteTarget(
        checksum=WriteChecksum(caps.database.checksum.value),
        database_version=caps.database.binary_version,
        firewire_guid=guid,
        max_database_bytes=caps.database.max_database_bytes,
        cover_formats=application_cover_formats(profile),
        artwork_root_value=application_artwork_root_value(profile),
        include_auxiliary_image_records=caps.artwork.include_auxiliary_image_records,
        supports_sparse_artwork=caps.artwork.supports_sparse_artwork,
    ), profile


def load_library(root: Path, target: WriteTarget) -> IPodLibrary:
    db = root / "iPod_Control/iTunes/iTunesDB"
    if db.exists():
        lib = IPodLibrary.parse(db.read_bytes())
        art = root / "iPod_Control/Artwork/ArtworkDB"
        return lib.with_artwork(art.read_bytes()) if art.exists() else lib
    created = create_empty_library("Spike iPod", target, supports_podcasts=True)
    assert created.prepared is not None, created.issues
    return IPodLibrary.parse(created.prepared.itunes)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------- commands
def cmd_sync(a: argparse.Namespace) -> None:
    t0 = time.perf_counter()
    sub = Subsonic(a.url, a.user, a.password)
    root, work = Path(a.ipod), Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    state = open_state(Path(a.state))
    target, profile = device_target(a.model, FAKE_GUID)
    print(f"device profile: {profile.family} {profile.generation} -> checksum={target.checksum}, db=0x{target.database_version:x}, "
          f"covers={[c.format_id for c in target.cover_formats]}")

    library = sub.all_songs()
    t_index = time.perf_counter()
    playlist = sub.playlist_by_name(a.playlist)
    wanted_ids = {s["id"] for s in playlist} if a.only_playlist else {s["id"] for s in library}
    wanted_ids -= set(a.exclude or ())
    by_id = {s["id"]: s for s in library}
    album_cache: dict[str, dict] = {}

    source = load_library(root, target)
    snapshot = source.snapshot
    known = {r[0]: r for r in state.execute("select * from track")}
    by_ipod_id = {r[1]: r[0] for r in known.values()}

    keep: list[Track] = []
    removed_paths: list[str] = []
    for t in snapshot.tracks:
        nd = by_ipod_id.get(t.track_id)
        if nd is None or nd in wanted_ids:
            keep.append(t)  # unmanaged tracks are left alone
        else:
            removed_paths.append(t.metadata.location)

    new_tracks, media, artwork, copies = [], [], [], []
    art_ids: dict[str, int] = {}
    draft_to_nd: dict[int, str] = {}
    next_id = -1
    for nd_id in sorted(wanted_ids - set(known)):
        song = by_id[nd_id]
        if song["albumId"] not in album_cache:
            album_cache[song["albumId"]] = sub.get("getAlbum", id=song["albumId"])["album"]
        album = album_cache[song["albumId"]]
        src, how = fetch_source(sub, song, tuple(a.path_map.split("=", 1)) if a.path_map else None, work)
        prepared, enc, ext = prepare_audio(src, song["suffix"], work)
        folder = f"F{int(hashlib.sha1(nd_id.encode()).hexdigest(), 16) % 50:02d}"
        rel = f"iPod_Control/Music/{folder}/{secrets.token_hex(2).upper()}{ext}"
        data = prepared.read_bytes()
        copies.append((rel, prepared, how))
        cover_key = album.get("coverArt") or ""
        if cover_key and cover_key not in art_ids:
            px = cover_pixels(sub, cover_key)
            if px:
                art_ids[cover_key] = -(len(art_ids) + 1)
                artwork.append(ArtworkAsset(art_ids[cover_key], px))
        track = Track(
            next_id, song["title"], song.get("artist", ""), song.get("album", ""),
            int(song.get("duration", 0) * 1000),
            genre=song.get("genre", ""), year=song.get("year", 0), track_number=song.get("track", 0),
            size_bytes=len(data), bitrate_kbps=song.get("bitRate", 0),
            album_artist=song.get("displayAlbumArtist", ""),
            artwork_id=art_ids.get(cover_key, 0),
            metadata=TrackMetadata(
                location=rel,
                file_format="Apple Lossless audio file" if enc is AudioEncoding.ALAC else
                            ("MPEG audio file" if enc is AudioEncoding.MP3 else "AAC audio file"),
                sample_rate_hz=song.get("samplingRate", 44100),
                disc_number=song.get("discNumber", 0),
                compilation=bool(album.get("isCompilation")),
                date_added=int(time.time()),
            ),
        )
        new_tracks.append(track)
        media.append(prepared_audio(next_id, FileDependency(rel, len(data), content_sha256(data)), enc))
        draft_to_nd[next_id] = nd_id
        next_id -= 1

    # playlist: map Navidrome order to iPod/draft ids
    nd_to_track = {**{v: k for k, v in by_ipod_id.items()}, **{v: k for k, v in draft_to_nd.items()}}
    pl_ids = [nd_to_track[e["id"]] for e in playlist if e["id"] in nd_to_track and e["id"] in wanted_ids]
    existing = next((p for p in snapshot.playlists if p.name == a.playlist), None)
    playlists = [p for p in snapshot.playlists if p.name != a.playlist]
    playlists.append(Playlist(existing.playlist_id if existing else -100, a.playlist, PlaylistKind.PLAYLIST,
                              entries=playlist_entries(pl_ids, existing.entries if existing else ())))

    desired = replace(snapshot, tracks=(*keep, *new_tracks), playlists=tuple(playlists))
    analysis = source.analyze(source.begin_draft(desired, delete_omissions=True), target)
    blocking = [i for i in analysis.issues if i.severity.value == "error"]
    if blocking:
        raise SystemExit(f"plan blocked: {blocking}")

    # copy media first (the DB must never reference a file that is not there)
    for rel, path, _ in copies:
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    # existing .ithmb artwork files: inventory + content so retained images can be rewritten
    inventory, art_files = [], []
    art_dir = root / "iPod_Control/Artwork"
    for f in sorted(art_dir.glob("*.ithmb")) if art_dir.exists() else ():
        blob = f.read_bytes()
        dep = FileDependency(f"iPod_Control/Artwork/{f.name}", len(blob), content_sha256(blob))
        inventory.append(dep)
        art_files.append(SourceFile(dep, blob))
    result = source.prepare(analysis, WriteResources(media=tuple(media), artwork=tuple(artwork),
                                                     files=tuple(art_files), file_inventory=tuple(inventory),
                                                     pending_playback_sidecars=False))
    if result.prepared is None:
        for rel, _, _ in copies:
            (root / rel).unlink(missing_ok=True)
        raise SystemExit(f"prepare failed: {result.issues}")
    p = result.prepared
    atomic_write(root / "iPod_Control/iTunes/iTunesDB", p.itunes)
    if p.artwork is not None:
        atomic_write(root / "iPod_Control/Artwork/ArtworkDB", p.artwork)
    for f in p.artwork_files:
        atomic_write(root / f.relative_path, bytes(f.data))
    for rel in removed_paths:
        (root / rel).unlink(missing_ok=True)

    # persist mapping from draft ids to the ids the database actually got
    out_ids = {m.draft_id: m.output_id for m in p.identities if m.subject == "track"}
    for draft_id, nd_id in draft_to_nd.items():
        song = by_id[nd_id]
        t = next(t for t in p.snapshot.tracks if t.track_id == out_ids.get(draft_id, draft_id))
        state.execute("insert or replace into track values (?,?,?,?,?)",
                      (nd_id, t.track_id, t.metadata.location, f"{song['size']}", t.metadata.file_format))
    removed_nd = [by_ipod_id[t.track_id] for t in snapshot.tracks
                  if t.track_id in by_ipod_id and by_ipod_id[t.track_id] not in wanted_ids]
    state.executemany("delete from track where nd_id=?", [(n,) for n in removed_nd])
    state.commit()

    print(f"index: {len(library)} songs in {t_index - t0:.2f}s | added {len(new_tracks)} "
          f"({', '.join(sorted({h for *_, h in copies})) or '-'}) | removed {len(removed_paths)} | "
          f"artwork assets {len(artwork)} | total {time.perf_counter() - t0:.2f}s")
    if result.issues:
        print("warnings:", [f"{i.severity.value}: {i.message}" for i in result.issues][:5])


def cmd_show(a: argparse.Namespace) -> None:
    root = Path(a.ipod)
    data = (root / "iPod_Control/iTunes/iTunesDB").read_bytes()
    lib = IPodLibrary.parse(data)
    print(f"HASH58 valid: {verify_hash58(data, FAKE_GUID)} | db bytes {len(data)}")
    for t in lib.snapshot.tracks:
        exists = (root / t.metadata.location).is_file()
        print(f"  [{t.track_id}] {t.album_artist or t.artist} / {t.album} / {t.track_number:02d} {t.title} "
              f"| {t.metadata.file_format} | comp={t.metadata.compilation} | art={t.artwork_id} "
              f"| plays={t.play_count} | file_ok={exists}")
    for pl in lib.snapshot.playlists:
        names = {t.track_id: t.title for t in lib.snapshot.tracks}
        print(f"  playlist '{pl.name}': {[names.get(e.track_id) for e in pl.entries]}")


def cmd_fake_plays(a: argparse.Namespace) -> None:
    """Write a Play Counts file as the firmware would (positional rows, mhdp header)."""
    root = Path(a.ipod)
    lib = IPodLibrary.parse((root / "iPod_Control/iTunes/iTunesDB").read_bytes())
    plays = dict(x.split(":") for x in a.plays)  # title:count
    now = int(time.time())
    rows = b""
    for t in lib.snapshot.tracks:
        n = int(plays.get(t.title, 0))
        rows += struct.pack("<IIII12x", n, (now - 600 + MAC_EPOCH_OFFSET) if n else 0, 0, 0)
    header = b"mhdp" + struct.pack("<III", 96, 28, len(lib.snapshot.tracks)) + bytes(80)
    (root / "iPod_Control/iTunes/Play Counts").write_bytes(header + rows)
    print(f"wrote Play Counts for {len(lib.snapshot.tracks)} rows")


def cmd_scrobble(a: argparse.Namespace) -> None:
    root = Path(a.ipod)
    sub = Subsonic(a.url, a.user, a.password)
    state = open_state(Path(a.state))
    by_ipod_id = {r[1]: r[0] for r in state.execute("select nd_id, ipod_track_id from track")}
    lib = IPodLibrary.parse((root / "iPod_Control/iTunes/iTunesDB").read_bytes())
    doc = parse_play_counts((root / "iPod_Control/iTunes/Play Counts").read_bytes())
    sent = 0
    for track, entry in zip(lib.snapshot.tracks, doc.entries, strict=False):
        if not entry.play_count:
            continue
        nd = by_ipod_id.get(track.track_id)
        if nd is None:
            continue
        last = entry.last_played - MAC_EPOCH_OFFSET
        for i in range(entry.play_count):  # earlier plays get synthesised timestamps
            ts = last - (entry.play_count - 1 - i) * max(track.length_ms // 1000, 1)
            sub.get("scrobble", id=nd, time=ts * 1000, submission="true")
            sent += 1
        song = sub.get("getSong", id=nd)["song"]
        print(f"  {track.title}: iPod plays={entry.play_count} -> Navidrome playCount={song.get('playCount')} played={song.get('played')}")
    print(f"scrobbled {sent} plays")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ipod", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--url", default="http://127.0.0.1:4533")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="spikepass")
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("sync")
    s.add_argument("--work", required=True)
    s.add_argument("--model", default="MB565")  # 6.5G 120GB black
    s.add_argument("--playlist", default="Spike Mix")
    s.add_argument("--only-playlist", action="store_true")
    s.add_argument("--exclude", nargs="*")
    s.add_argument("--path-map", help="NAVIDROME_PREFIX=LOCAL_PREFIX")
    sp.add_parser("show")
    f = sp.add_parser("fake-plays")
    f.add_argument("plays", nargs="+")
    sp.add_parser("scrobble")
    a = ap.parse_args()
    {"sync": cmd_sync, "show": cmd_show, "fake-plays": cmd_fake_plays, "scrobble": cmd_scrobble}[a.cmd](a)


if __name__ == "__main__":
    main()
