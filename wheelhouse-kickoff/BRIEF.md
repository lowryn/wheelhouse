# Wheelhouse — build brief

Read this whole file, then `spike/spike.py`, before writing any code.

## What we're building

A personal-use macOS command-line tool that syncs iPods running stock Apple firmware from Navidrome, in the way iTunes used to:

- Curation happens in Navidrome.
- Each iPod is remembered between sessions.
- Plugging in and running `wheelhouse sync`:
  - mirrors that iPod's selection onto it;
  - scrobbles the iPod's plays back to Navidrome;
  - writes Navidrome's play counts and ratings onto the iPod.

It is not a product, so there is no GUI and no packaging. Licence: GPL-3.0-or-later, because it imports iOpenPod, which is GPL-3.

**Naming:** the project and the command are `wheelhouse`. The PyPI name `wheelhouse` is already taken by an unrelated packaging utility, so name the Python distribution `wheelhouse-ipod` in `pyproject.toml`, keep the import package `wheelhouse` and the console command `wheelhouse`.

## Hard constraints

- **Never change** Navidrome's configuration, the Linux VM, Docker, TrueNAS or Proxmox. The tool is a Subsonic API client and nothing more.
- **Never write to a real iPod** without an explicit go-ahead from Nigel for that run.
  - Every write command has `--dry-run`.
  - The first run against each iPod is a dry run.
  - Before any write, say exactly what will change.
- **Only ever delete tracks or files the tool manages**, meaning they are recorded in its state. Unknown tracks on an iPod are left alone.
- **Evidence before claims.** When firmware or API behaviour is uncertain, test it or say you don't know. Don't guess.
- **Be pragmatic.** Build the smallest thing that works, with no speculative abstraction.

## Environment

- **Host:** Mac Mini M4 Pro (Apple Silicon, macOS). The iPods connect here and mount under `/Volumes`.
- **Tools:**
  - Python **3.12 exactly** (iOpenPod requires `>=3.12,<3.13`), managed with `uv`.
  - FFmpeg and FFprobe from Homebrew. The build must include libx264, which video needs.
- **Navidrome:**
  - Runs in Docker on a Linux VM on the LAN. Use the Subsonic API (`/rest/...`) only.
  - In Nigel's setup `/rest/` bypasses Authentik forward auth, so plain Subsonic token auth works.
  - Log in as Nigel's own account, because play counts are per user. Keep the password in the macOS Keychain using `keyring`, which iOpenPod already depends on.
- **iPods** (all currently synced by foobar2000 + foo_dop from a Windows VM):

| iPod | Current contents | Notes |
|---|---|---|
| Classic 6.5G 120GB, flash storage (~512GB), firmware 2.0.2 via a SysCFG flash | Whole library (~25k tracks) | How it identifies after the flash is **unverified** |
| 5.5G (5th-gen Video) 30GB | 1001 Albums playlist | Firmware hung at boot at ~21k tracks; ~15k was fine. Artwork stopped loading when more was added. Needs a track cap |
| Third iPod | A few test files | Use this one for **all** first real writes |

## Decisions already made (don't revisit)

1. **Navidrome is the source of truth** for music, play counts and ratings. Files come from Subsonic `download` (the original file), so no network mount is needed.
2. **Selection is set per iPod** in the config: either `all`, or a list of Navidrome playlists (support globs such as `iPod:*`). Navidrome smart playlists should appear as ordinary playlists, but this is **untested**, so verify it.
3. **Shared drop folder, mirror semantics.** Everything in it goes onto every iPod. Delete a file and it comes off each iPod at that iPod's next sync.
4. **Audio formats:**
   - lossless → ALAC;
   - MP3 and AAC → copied as-is;
   - other lossy formats (Opus, Vorbis, WMA…) → AAC 256k.
5. **CLI plus a YAML config.** No GUI. Optional later: a `watch` mode that syncs when an iPod is mounted, and a Telegram notification.
6. **Build on iOpenPod's library packages, not its app.** Pin iOpenPod v2.0.17 and apply `patches/ipoddb-perf.patch` (see Performance).

## What to reuse from iOpenPod v2.0.17

The PyPI wheel ships four top-level packages. `iPodDB`, `storage` and `device_registry` have no imports from the GUI app.

- **`iPodDB.library`** reads and writes the iPod database. It is pure, with no I/O:
  - `IPodLibrary.parse(bytes)` and `.with_artwork(artworkdb_bytes)`
  - `create_empty_library(name, target, supports_podcasts=True)`
  - `lib.begin_draft(snapshot, delete_omissions=True)` → `lib.analyze(draft, target)` → `lib.prepare(plan, WriteResources(...))` → `prepared.itunes`, `.artwork`, `.artwork_files`, `.identities`
- **`iPodDB.sidecars`** has `parse_play_counts` and the positional-remap helpers.
- **`iPodDB.device_time`** has `DeviceTimeContext` and `mac_to_unix`.
- **`device_registry`** has `DEFAULT_DEVICE_REGISTRY` (`profile_for_model_number`, `identify(evidence)`) and `parse_sysinfo` / `parse_sysinfo_extended`.
- **`iOpenPod.app.artwork_policy`** (no Qt) has `application_cover_formats(profile)` and `application_artwork_root_value(profile)`.
- **`iOpenPod.app.media.transcoding`** (no Qt) is the reference for the iPod video recipe (H.264 Baseline via libx264) and for audio conversion. Reuse it if it imports cleanly; otherwise copy the ffmpeg arguments.
- **`storage`** handles device discovery, filesystem sessions, and transactional publishing with a recovery journal.
- **`iOpenPod.app.services.device_coordinator`** (`prepare_library` / `save_library`) and `library_resources` show the full real write path. Study them before writing the publish step. They cover:
  - building `WriteTarget` from the device profile (around line 1300);
  - reading the FireWire GUID;
  - the device time context;
  - preconditions for the iPod's play-count files.

  Reuse the `storage` transactions if that's practical; otherwise use the simple safe sequence below.
- **Tests worth reading:** `tests/iPodDB/library/test_creation.py`, and `tests/iOpenPod/app/services/test_library_resources.py` (`build_device`, `storage.testing.VirtualStoragePlatform`).
- **Avoid** anything that uses Qt: `iOpenPod.GUI`, the `*_controller.py` modules, `library_workspace.py`.

## Spike results (`spike/spike.py`)

The spike ran against a throwaway Navidrome 0.64.2 and a simulated 6.5G filesystem.

- **End to end works with no GUI:** `search3` index → download/convert → `iPodDB` → a HASH58-signed iTunesDB plus ArtworkDB. libgpod, an independent C reader (`spike/gpodcheck.c`), reads it correctly: tracks, album artist, compilation flag, artwork and playlists.
- **Incremental sync works.** The second run added and removed only the difference, and other files were untouched. iPod track IDs stay stable via the state database. Map draft IDs to output IDs with `prepared.identities`.
- **Scrobbling works.** Plays from the iPod's Play Counts file were sent with `scrobble?id=…&time=<ms>&submission=true`. Navidrome honours the backdated `time`, and its `playCount` and `played` values updated correctly.

**Gotchas found:**

- **Compilations:** the compilation flag is on the album (`getAlbum` → `isCompilation`), not on the song. Use `displayAlbumArtist` for the album artist.
- **Artwork files:** `prepare` needs the existing `.ithmb` artwork files supplied on every write (as a `SourceFile` plus `file_inventory`). Without them it fails with "Artwork encoding requires a captured file inventory". The spike reads them into memory, which at whole-library scale is about 0.5 GB. Use the `ContentBuffer` / `create_file_buffer` mechanism iOpenPod uses.
- **Play Counts rows are positional:** each row refers to a track by its position in the current iTunesDB.
  - Scrobble the plays first.
  - Then clear the Play Counts file as part of the next database write.
  - Never write a reordered database while an unconsumed Play Counts file exists (see `iPodDB`'s `pending_playback_sidecars` contract).
  - The spike left the file in place, which is why libgpod still showed the plays.
- **Timestamps:** Play Counts times are in device-local Mac time. The spike assumed UTC; use `iPodDB.device_time` with the device's time context.
- **Paths:** Navidrome's `path` field is virtual unless real-path reporting is turned on. That doesn't matter on the download route, so don't rely on `path`.
- **Draft IDs:** new drafts use negative IDs for tracks, artwork and playlists. Use one `ArtworkAsset` per album cover (`coverArt` id), shared by that album's tracks.

## Performance

Upstream `iPodDB` with 25k synthetic tracks on a 2-vCPU Linux sandbox:

| Step | Upstream | With `patches/ipoddb-perf.patch` |
|---|---|---|
| Plan the change (`analyze`) | 102 s | 4.6 s |
| Build the database (`prepare`) | 175 s | 71 s |
| Incremental re-sync | 209 s | 87 s |
| Peak memory | ~1.7 GB | ~1.7 GB |

- **The cause** was three loops that compare everything against everything, plus a generic nested function that was rebuilt for every database entry. The patch is 24 lines added and 10 removed, across three files.
- **Tests:** upstream's `tests/iPodDB` pass with the patch: 2,397 tests. The 3 failures also happen without the patch; they come from private test fixtures and missing tkinter.
- **How to consume it:**
  - Fork TheRealSavi/iOpenPod to Nigel's GitHub.
  - Branch from commit `2ee3248` (v2.0.17) and apply the patch.
  - Depend on it with a pinned git URL, e.g. `iopenpod @ git+https://github.com/<nigel>/iOpenPod@<commit>`.
  - Nigel decides whether to also offer the patch upstream as a pull request.
- **Album artwork** costs about 80 ms per cover, on the first encode only.
- **The remaining 70–90 s** is spread across many full passes over the database. That's acceptable for a sync job. Measure on the M4 first (`spike/perf.py`), and don't optimise further unless real timings are painful.

## Architecture

```
wheelhouse/
  config.py      # load and validate the YAML
  state.py       # SQLite: per-iPod state, scrobble ledger, conversion cache index
  navidrome.py   # Subsonic client
  dropfolder.py  # scan the shared drop folder into items
  media.py       # ffprobe/ffmpeg conversion and cache
  device.py      # find mounted iPods, identify them, build WriteTarget, read DB, play counts and time
  plan.py        # desired set vs state → diff; space and track-cap checks
  adopt.py       # first-run matching of tracks already on an iPod
  publish.py     # copy media, prepare DB, safe write, verify, clean up
  cli.py         # status | adopt | sync | eject   (later: watch)
```

### Config (`~/.config/wheelhouse/config.yaml`)

```yaml
navidrome:
  url: https://music.example        # Nigel fills in
  user: nigel                       # password in the macOS Keychain
drop_folder: ~/Wheelhouse
cache_dir: ~/Library/Caches/wheelhouse
ipods:
  - name: Classic 6.5G
    serial: XXXXXXXX                # printed by `wheelhouse status`
    music: all
  - name: 5.5G
    serial: YYYYYYYY
    music: { playlists: ["iPod: 1001 Albums"] }
    max_tracks: 15000
  - name: Test
    serial: ZZZZZZZZ
    music: { playlists: ["iPod: Test"] }
```

### State (SQLite, `~/Library/Application Support/wheelhouse/state.sqlite`)

- `ipod(serial PK, guid, model, name, last_seen, last_db_sha256)`
- `item(ipod_serial, source, source_id, ipod_track_id, ipod_path, source_sig, media_kind, encoding, PRIMARY KEY(ipod_serial, source, source_id))`
  - `source` is `navidrome` or `drop`.
  - `source_id` is the Navidrome song ID, or for the drop folder the path relative to the folder.
  - `source_sig` is Navidrome `size` plus whichever timestamp changes on re-tag (check whether that's `created` or another field). For the drop folder it's size plus mtime.
- `scrobble_ledger(ipod_serial, play_counts_sha256, row, track_id, sent_at)` makes scrobbling safe to repeat after a crash.
- `cache(source, source_id, source_sig, profile, path, size)` holds converted files, shared across all iPods.

### Sync sequence

1. **Find and identify the iPod.** Look under `/Volumes` and identify it from SysInfo / SysInfoExtended via `device_registry`. Match it to the config by serial, and refuse unknown iPods (print how to add them).
2. **Read the iPod.** Load the iTunesDB, ArtworkDB and device time context. Compare the database's sha256 with `last_db_sha256`; if it changed outside the tool, reconcile by track ID before trusting the state.
3. **Handle play counts.**
   - Map Play Counts rows to items.
   - Scrobble Navidrome items to Navidrome and record each in `scrobble_ledger`.
   - Drop-folder items' plays are only kept on the iPod.
   - When a row has several plays, give the earlier ones synthesised timestamps spaced by the track length.
4. **Work out the diff.** The desired set is the Navidrome selection plus the drop folder. Compare it with the state to get adds, removes and updates; a changed `source_sig` becomes a `replace_media`. Check free space and `max_tracks`, and fail with a clear report rather than ever going over.
5. **Fetch, convert and copy.** Use the cache, which is shared across iPods. Copy media to `iPod_Control/Music/Fxx`; take the folder count from the device profile.
6. **Build the snapshot.**
   - Kept tracks plus new tracks.
   - Navidrome playlists recreated by name, in Navidrome's order.
   - For every Navidrome item, set `play_count`, `rating` (Navidrome 1–5 stars × 20) and `last_played` from Navidrome.
7. **Write.**
   - Run `prepare`.
   - Back up the current iTunesDB and ArtworkDB to the Mac, keeping the last 10 per iPod.
   - Publish, then clear Play Counts.
   - Delete removed managed files, update the state, and print a summary.
8. **Verify.** Re-parse the database, check HASH58 on Classics, and confirm every referenced file exists with the expected size.
9. **Eject** with `wheelhouse eject` (`diskutil eject`), optionally automatically at the end of `sync`.

**Safe publish**, if you don't use the `storage` transactions:
1. Copy media first.
2. Write the database bytes to a temporary file in the same folder, `fsync`, then rename over the original. Do the same for ArtworkDB and the `.ithmb` files.
3. Only then delete removed files.

A crash at any point leaves either the old database (with nothing worse than orphan files) or the new one.

### Adoption (first run on each existing iPod)

- **`wheelhouse adopt --dry-run`** matches every iPod track to a Navidrome song.
  - Key: normalised (album artist, album, disc number, track number, title), confirmed by duration within ±2 s.
  - Fallback: (artist, title, duration), used only when unique.
  - It writes a CSV plus a summary covering:
    - matched;
    - ambiguous;
    - on the iPod but unmatched;
    - selected in Navidrome but missing from the iPod.
- **`wheelhouse adopt`**, run after Nigel has reviewed the report, records matched tracks as managed items using their existing files. Nothing is recopied. Unmatched iPod tracks stay unmanaged and untouched.
- **Play counts:** plays found in the Play Counts file at adoption are scrobbled once. Historical play counts already in the database are **not** sent, because that would count them twice.
- **`--import-playlists`** creates Navidrome playlists from the iPod's playlists, named with an `iPod: ` prefix, using `createPlaylist` with the matched song IDs. This is how the existing 1001 Albums playlist gets into Navidrome without fixing the old M3Us, which use Windows `Z:\` paths. Ask before creating anything.
- **Format differences:** if an adopted file's format differs from what the conversion rules would produce, keep it unless `--reencode` is given.

### Drop folder (shared by all iPods, mirror semantics)

```
~/Wheelhouse/
  Music/  Audiobooks/<book>/  Movies/  TV/<show>/  Music Videos/  Notes/
```

The subfolder decides the type:

| Folder | Conversion | Type on the iPod |
|---|---|---|
| Music | Same rules as Navidrome music | music |
| Audiobooks | M4B/AAC/MP3 kept; others → AAC 96k | audiobook, with resume on. Chapters kept from M4B. Album = book folder; track order by file name |
| Movies, TV, Music Videos | H.264 Baseline ≤640×480 + AAC in `.m4v`, using iOpenPod's video recipe | movie / tv_show (show = folder; season and episode from `SxxEyy` if present) / music_video |
| Notes | `.txt` copied to the iPod's `Notes/` folder; notes aren't in the database. Track which notes the tool owns | — |

Read tags with `mutagen`. If there's no title, use the file name.

### Navidrome API notes

- **Auth parameters:** `u`, `t = md5(password + salt)`, `s`, `v=1.16.1`, `c=wheelhouse`, `f=json`.
- **Index:** `search3` with an empty query and `songCount=500`, paging with `songOffset`. That's about 50 requests for 25k songs. This worked in the spike; verify it on the real server.
- **Endpoints:**
  - `getPlaylists` / `getPlaylist`
  - `getAlbum` (`isCompilation`, `coverArt`)
  - `getCoverArt` (`size=600`)
  - `download`: should be the original file. Check that its size equals `song.size`.
  - `scrobble` (`time` in ms, `submission=true`)
  - `getSong`
  - `createPlaylist`, only for adoption import
- **Song fields used:** `id, title, artist, album, displayAlbumArtist, track, discNumber, year, genre, duration, bitRate, samplingRate, suffix, size, playCount, userRating, played, created`.

## Milestones

Each one ends with Nigel testing on real hardware.

- **M0 — Set up.** Create the repo and `uv` project (Python 3.12), with the iOpenPod fork plus the performance patch as a dependency. Turn the spike's setup into pytest fixtures: a throwaway Navidrome (release binary, `ND_*` environment variables, `POST /auth/createAdmin`) and a simulated iPod.
- **M1 — Read-only status.** `wheelhouse status` detects each real iPod and shows model, serial, GUID, track count, free space and whether the database parses. Nigel runs it on all three; this also confirms how the 6.5G identifies after its flash.
- **M2 — Adoption dry run** on all three iPods. Nigel reviews the match quality.
- **M3 — First real sync on the Test iPod** with a small Navidrome playlist. Nigel checks on the device:
  - playback, including ALAC;
  - artwork;
  - album and compilation grouping;
  - playlist order.
- **M4 — Play-count loop.** Play on the device, sync, and check Navidrome's counts. Check that Navidrome's counts and ratings show on the iPod.
- **M5 — The real iPods.** Adopt and sync the 5.5G (1001 Albums) and the 6.5G (whole library). Time the whole-library sync on the M4.
- **M6 — Drop folder:** music, audiobooks (check resume works), notes, then video.
- **Later / optional:** `watch` mode, Telegram notification, two-way ratings.

## Working with Nigel

- Be direct; no fluff.
- Show evidence — command output, test results — rather than assertions.
- Say "I don't know" rather than guessing, and propose a test instead.
- Pragmatic over perfect.
- Before any command that writes to a real iPod or creates something in Navidrome, state exactly what will change and wait for a yes.

## Files in this kickoff bundle

- `spike/spike.py` — the working spike: Navidrome → iPod database, incremental sync, scrobbling.
- `spike/perf.py` — the synthetic large-library timing harness.
- `spike/gpodcheck.c` — independent check using libgpod: `gcc gpodcheck.c $(pkg-config --cflags --libs libgpod-1.0)`.
- `patches/ipoddb-perf.patch` — the `iPodDB` performance fix against upstream commit `2ee3248`.
