"""Download Navidrome songs and commit them to an iPod iTunesDB."""
from __future__ import annotations

import hashlib
import io
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

import mutagen
from mutagen.mp3 import BitrateMode
from PIL import Image

from iPodDB.library import (
    AudioEncoding,
    ArtworkAsset,
    ArtworkPixels,
    FileDependency,
    IPodLibrary,
    PreparedMedia,
    SourceFile,
    Track,
    TrackMetadata,
    WriteResources,
    WriteTarget,
    prepared_audio,
)
from iOpenPod.app.media.music_paths import MusicPathAllocator

from wheelhouse.navidrome import NavidromeClient
from wheelhouse.publish import atomic_write


# Formats Navidrome may serve that an iPod Classic supports natively.
_SUFFIX_ENCODING: dict[str, AudioEncoding] = {
    "mp3":  AudioEncoding.MP3,
    "m4a":  AudioEncoding.AAC,   # refined to ALAC below when detected
    "aac":  AudioEncoding.AAC,
    "wav":  AudioEncoding.WAV,
    "aiff": AudioEncoding.AIFF,
    "aif":  AudioEncoding.AIFF,
}

# Formats that need transcoding — not handled in this release.
SKIP_FORMATS = frozenset({"flac", "ogg", "opus", "wma", "ape", "dsf", "dff", "mp4"})


def ipod_title(song: dict) -> str:
    """Return the iPod display title, prefixing track artist for VA tracks.

    Classic firmware only shows the title field when browsing by album.
    Prefixing the track artist preserves performer context on compilations,
    matching the foo_dap `[%track artist% - ]%title%` display convention.
    """
    title = song.get("title", "")
    track_artist = song.get("artist", "")
    album_artist = (
        song.get("albumArtist")
        or song.get("displayAlbumArtist")
        or song.get("artist", "")
    )
    if album_artist and track_artist and track_artist != album_artist:
        return f"{track_artist} - {title}"
    return title


def _detect_encoding(suffix: str, audio_bytes: bytes) -> AudioEncoding | None:
    base = _SUFFIX_ENCODING.get(suffix.lower())
    if base is None:
        return None
    if base is AudioEncoding.AAC and suffix.lower() == "m4a":
        try:
            mf = mutagen.File(io.BytesIO(audio_bytes), filename="x.m4a")
            codec = getattr(getattr(mf, "info", None), "codec", "") or ""
            if "alac" in codec.lower():
                return AudioEncoding.ALAC
        except Exception:
            pass
    return base


def _audio_facts(audio_bytes: bytes, suffix: str, nd_song: dict) -> tuple[int, int, int, bool]:
    """Return (length_ms, bitrate_kbps, sample_rate_hz, vbr)."""
    nd_bitrate = nd_song.get("bitRate", 0) or 0
    nd_ms = (nd_song.get("duration", 0) or 0) * 1000
    try:
        mf = mutagen.File(io.BytesIO(audio_bytes), filename=f"x.{suffix}")
        if mf is not None and hasattr(mf, "info"):
            info = mf.info
            ms = max(1, round((getattr(info, "length", 0) or 0) * 1000))
            bitrate = round((getattr(info, "bitrate", 0) or 0) / 1000) or nd_bitrate
            sr = getattr(info, "sample_rate", 44100) or 44100
            vbr = (
                getattr(info, "bitrate_mode", None) in (BitrateMode.VBR, BitrateMode.ABR)
                if suffix.lower() == "mp3"
                else False
            )
            return ms or nd_ms, bitrate, sr, vbr
    except Exception:
        pass
    return nd_ms or 1, nd_bitrate, 44100, False


_FORMAT_NAME = {
    AudioEncoding.MP3:  "MPEG audio file",
    AudioEncoding.AAC:  "AAC audio file",
    AudioEncoding.ALAC: "Apple Lossless audio file",
    AudioEncoding.WAV:  "WAV audio file",
    AudioEncoding.AIFF: "AIFF audio file",
}


def add_tracks(
    root: Path,
    lib: IPodLibrary,
    wt: WriteTarget,
    profile: object,
    nd: NavidromeClient,
    songs: list[dict],
    *,
    on_progress: Callable[[int, str], None] | None = None,
) -> tuple[IPodLibrary, list[tuple[str, str]]]:
    """Download *songs* from Navidrome, copy to iPod, update iTunes database.

    Returns (updated_library, [(nd_id, ipod_path), ...]).
    Tracks with unsupported formats (FLAC, OGG, …) are skipped silently.
    On any failure, already-written audio files are deleted before re-raising.
    """
    occupied = [t.metadata.location for t in lib.snapshot.tracks if t.metadata.location]
    allocator = MusicPathAllocator(
        profile.capabilities.database.music_directory_count, occupied
    )
    next_id = max((t.track_id for t in lib.snapshot.tracks), default=0) + 1
    next_artwork_id = max((t.artwork_id for t in lib.snapshot.tracks), default=0) + 1

    new_tracks = list(lib.snapshot.tracks)
    media_list: list[PreparedMedia] = []
    artwork_assets: list[ArtworkAsset] = []
    added: list[tuple[str, str]] = []
    files_written: list[Path] = []
    cover_id_to_artwork_id: dict[str, int] = {}

    try:
        for i, song in enumerate(songs):
            if on_progress:
                on_progress(i, song.get("title", ""))

            suffix = (song.get("suffix") or "mp3").lower()
            if suffix in SKIP_FORMATS:
                continue

            audio_bytes = nd.download(song["id"])
            encoding = _detect_encoding(suffix, audio_bytes)
            if encoding is None:
                continue

            length_ms, bitrate, sample_rate, vbr = _audio_facts(audio_bytes, suffix, song)
            sha256 = hashlib.sha256(audio_bytes).hexdigest()
            size = len(audio_bytes)

            ext = "aif" if suffix == "aiff" else suffix
            location = allocator.allocate(ext)
            track_id = next_id
            next_id += 1

            album_artist = (
                song.get("albumArtist")
                or song.get("displayAlbumArtist")
                or song.get("artist", "")
            )
            compilation = bool(
                album_artist
                and song.get("artist", "")
                and song.get("artist", "") != album_artist
            )

            artwork_id = 0
            cover_id = song.get("coverArt")
            if cover_id:
                if cover_id not in cover_id_to_artwork_id:
                    try:
                        art_bytes = nd.cover_art(cover_id, size=600)
                        img = Image.open(io.BytesIO(art_bytes)).convert("RGB")
                        w, h = img.size
                        pixels = ArtworkPixels(w, h, img.tobytes())
                        cover_id_to_artwork_id[cover_id] = next_artwork_id
                        artwork_assets.append(ArtworkAsset(next_artwork_id, pixels))
                        next_artwork_id += 1
                    except Exception:
                        cover_id_to_artwork_id[cover_id] = 0
                artwork_id = cover_id_to_artwork_id[cover_id]

            track = Track(
                track_id=track_id,
                title=ipod_title(song),
                artist=song.get("artist", ""),
                album=song.get("album", ""),
                album_artist=album_artist,
                length_ms=length_ms,
                genre=song.get("genre", ""),
                year=int(song.get("year", 0) or 0),
                track_number=int(song.get("track", 0) or 0),
                size_bytes=size,
                bitrate_kbps=bitrate,
                artwork_id=artwork_id,
                metadata=TrackMetadata(
                    file_format=_FORMAT_NAME.get(encoding, "audio file"),
                    variable_bitrate=vbr,
                    sample_rate_hz=sample_rate,
                    disc_number=int(song.get("discNumber", 0) or 0),
                    date_added=int(time.time()),
                    location=location,
                    compilation=compilation,
                ),
            )
            file_dep = FileDependency(location, size, sha256)
            media_list.append(prepared_audio(track_id, file_dep, encoding))

            dest = root / location
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(audio_bytes)
            files_written.append(dest)

            new_tracks.append(track)
            added.append((song["id"], location))

        if not added:
            return lib, []

        new_snapshot = replace(lib.snapshot, tracks=tuple(new_tracks))
        updated = _commit(root, lib, wt, new_snapshot, tuple(media_list), False,
                          artwork=tuple(artwork_assets))
        return updated, added

    except Exception:
        for f in files_written:
            f.unlink(missing_ok=True)
        raise


def remove_tracks(
    root: Path,
    lib: IPodLibrary,
    wt: WriteTarget,
    ipod_track_ids: set[int],
    paths: list[str],
) -> IPodLibrary:
    """Remove *ipod_track_ids* from the library and delete their files.

    Only managed tracks should be passed here; unmanaged tracks are preserved.
    Files are deleted after a successful database commit.
    """
    new_playlists = tuple(
        replace(pl, entries=tuple(e for e in pl.entries if e.track_id not in ipod_track_ids))
        for pl in lib.snapshot.playlists
    )
    new_snapshot = replace(
        lib.snapshot,
        tracks=tuple(t for t in lib.snapshot.tracks if t.track_id not in ipod_track_ids),
        playlists=new_playlists,
    )
    updated = _commit(root, lib, wt, new_snapshot, (), True)
    for p in paths:
        if p:
            (root / p).unlink(missing_ok=True)
    return updated


def _commit(
    root: Path,
    lib: IPodLibrary,
    wt: WriteTarget,
    new_snapshot: object,
    media: tuple[PreparedMedia, ...],
    delete_omissions: bool,
    *,
    artwork: tuple[ArtworkAsset, ...] = (),
) -> IPodLibrary:
    from iPodDB.sidecars import PlaybackSidecar, is_playback_sidecar

    itunes_dir = root / "iPod_Control" / "iTunes"
    artwork_dir = root / "iPod_Control" / "Artwork"

    sidecar_files = [f for f in itunes_dir.iterdir() if is_playback_sidecar(f.name)]
    sidecars: tuple[PlaybackSidecar, ...] = ()
    if sidecar_files:
        sidecars = tuple(PlaybackSidecar(f.name, f.read_bytes()) for f in sidecar_files)
        lib = lib.with_sidecars(sidecars)

    # Pre-load existing .ithmb files so iOpenPod can append new thumbnails to them
    source_files: tuple[SourceFile, ...] = ()
    file_inventory: tuple[FileDependency, ...] | None = None
    if artwork and artwork_dir.exists():
        ithmb: list[tuple[FileDependency, bytes]] = []
        for f in sorted(artwork_dir.iterdir()):
            if f.suffix == ".ithmb":
                data = f.read_bytes()
                ithmb.append((
                    FileDependency(f"iPod_Control/Artwork/{f.name}", len(data),
                                   hashlib.sha256(data).hexdigest()),
                    data,
                ))
        if ithmb:
            source_files = tuple(SourceFile(dep, data) for dep, data in ithmb)
            file_inventory = tuple(dep for dep, _ in ithmb)

    def _attempt(l: IPodLibrary) -> object:
        d = l.begin_draft(new_snapshot, delete_omissions=delete_omissions)
        p = l.analyze(d, wt)
        if p.blocked:
            raise RuntimeError(f"Library preparation blocked: {[i for i in p.issues if i.severity.value == 'error']}")
        return l.prepare(p, WriteResources(
            media=media, artwork=artwork,
            files=source_files, file_inventory=file_inventory,
            pending_playback_sidecars=False,
        ))

    result = _attempt(lib)

    if result.prepared is None and artwork:
        # Stale ArtworkDB (e.g. after replacing all tracks) — retry without it
        if any(e.code == "preparation.cannot_encode" for e in result.issues):
            lib2 = IPodLibrary.parse((root / "iPod_Control" / "iTunes" / "iTunesDB").read_bytes())
            if sidecars:
                lib2 = lib2.with_sidecars(sidecars)
            result = _attempt(lib2)

    if result.prepared is None:
        errors = [i for i in result.issues if i.severity.value == "error"]
        raise RuntimeError(f"Library preparation failed: {errors}")

    atomic_write(root / "iPod_Control" / "iTunes" / "iTunesDB", result.prepared.itunes)

    # Write updated artwork files back to the iPod
    if result.prepared.artwork is not None:
        artwork_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(artwork_dir / "ArtworkDB", result.prepared.artwork)
    for pf in result.prepared.artwork_files:
        dest = root / pf.relative_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = pf.data if isinstance(pf.data, bytes) else pf.data.read_at(0, len(pf.data))
        atomic_write(dest, data)

    for f in sidecar_files:
        f.unlink(missing_ok=True)
    from wheelhouse.device import load_library
    return load_library(root, wt)
