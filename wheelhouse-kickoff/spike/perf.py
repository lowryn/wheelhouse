"""Time iPodDB on a synthetic large library: initial build, re-parse, and a small incremental change."""

import sys
import time
from dataclasses import replace

from iPodDB.library import (
    ArtworkAsset, ArtworkPixels, AudioEncoding, FileDependency, IPodLibrary, Playlist,
    Track, TrackMetadata, WriteResources, create_empty_library, playlist_entries, prepared_audio,
)

sys.path.insert(0, sys.argv[2])
from spike import FAKE_GUID, device_target  # noqa: E402

N = int(sys.argv[1])
ALBUMS = max(N // 15, 1)
ART_ALBUMS = int(sys.argv[3]) if len(sys.argv) > 3 else 0
target, _ = device_target("MB565", FAKE_GUID)
created = create_empty_library("Perf iPod", target, supports_podcasts=True)
src = IPodLibrary.parse(created.prepared.itunes)

tracks, media = [], []
for i in range(N):
    tid = -(i + 1)
    alb = i % ALBUMS
    rel = f"iPod_Control/Music/F{i % 50:02d}/T{i:05d}.m4a"
    tracks.append(Track(tid, f"Title {i}", f"Artist {alb % 400}", f"Album {alb}", 240000,
                        genre="Rock", year=1990 + alb % 30, track_number=i % 15 + 1,
                        size_bytes=8_000_000, bitrate_kbps=900, album_artist=f"Artist {alb % 400}",
                        artwork_id=-(alb + 1) if alb < ART_ALBUMS else 0,
                        metadata=TrackMetadata(location=rel, file_format="Apple Lossless audio file",
                                               sample_rate_hz=44100)))
    media.append(prepared_audio(tid, FileDependency(rel, 8_000_000, f"{i:064x}"), AudioEncoding.ALAC))
art = tuple(ArtworkAsset(-(a + 1), ArtworkPixels(320, 320, bytes((a % 256, 80, 160)) * (320 * 320)))
            for a in range(ART_ALBUMS))
pl = Playlist(-1, "1001 Albums", entries=playlist_entries([t.track_id for t in tracks[: N // 2]]))

t0 = time.perf_counter()
plan = src.analyze(src.begin_draft(replace(src.snapshot, tracks=tuple(tracks), playlists=(pl,))), target)
t1 = time.perf_counter()
res = src.prepare(plan, WriteResources(media=tuple(media), artwork=art, file_inventory=(),
                                       pending_playback_sidecars=False))
t2 = time.perf_counter()
assert res.prepared, res.issues[:3]
db = res.prepared.itunes
lib = IPodLibrary.parse(db)
_ = lib.snapshot.tracks
t3 = time.perf_counter()
# incremental: drop 10 tracks, add nothing
snap = lib.snapshot
plan2 = lib.analyze(lib.begin_draft(replace(snap, tracks=snap.tracks[10:],
                    playlists=tuple(replace(p, entries=tuple(e for e in p.entries if e.track_id not in
                    {t.track_id for t in snap.tracks[:10]})) for p in snap.playlists)), delete_omissions=True), target)
res2 = lib.prepare(plan2, WriteResources(file_inventory=None if not ART_ALBUMS else (), pending_playback_sidecars=False))
t4 = time.perf_counter()
print(f"N={N} albums={ALBUMS} art_albums={ART_ALBUMS} | iTunesDB {len(db)/1e6:.1f} MB"
      f"{f', ArtworkDB+ithmb {sum(len(bytes(f.data)) for f in res.prepared.artwork_files)/1e6:.0f} MB' if ART_ALBUMS else ''}")
print(f"analyze {t1-t0:.1f}s | prepare {t2-t1:.1f}s | parse+project {t3-t2:.1f}s | incremental re-plan+prepare {t4-t3:.1f}s "
      f"({'ok' if res2.prepared else res2.issues[:2]})")
