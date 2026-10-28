# wheelhouse

Sync music from a [Navidrome](https://www.navidrome.org/) server to an iPod Classic
on macOS, without iTunes.

Built around [TheRealSavi/iOpenPod](https://github.com/TheRealSavi/iOpenPod) for the
iTunesDB authoring; wheelhouse is the application layer: a Navidrome client,
state tracking per iPod, a drop folder for audiobooks/videos/notes, scrobble
sync, and both a CLI and interactive flow.

## Status

Personal project. Works on my 5.5G, 6G, and 6.5G (flash-modded) iPod Classics
against my Navidrome instance. Not polished enough to install blindly — expect
to read the code before trusting it with a device you care about.

## Install

```sh
git clone https://github.com/lowryn/wheelhouse.git
cd wheelhouse
uv sync
```

Requires `python 3.12` and [`uv`](https://docs.astral.sh/uv/). Everything else
is pulled in automatically. Run commands via `uv run wheelhouse ...` — never
activate the venv manually.

## Configure

Create `~/.config/wheelhouse/config.yaml`:

```yaml
navidrome:
  url: https://music.example.com
  user: you@example.com
drop_folder: ~/Wheelhouse
cache_dir: ~/Library/Caches/wheelhouse
ipods:
  - name: My iPod
    serial: XXXXXXXXXXX        # from `wheelhouse status`
    music: all                 # or `music: { playlists: ["Favourites"] }`
```

Store the Navidrome password in the macOS Keychain:

```sh
python -c "import keyring; keyring.set_password('wheelhouse', 'you@example.com', input('Password: '))"
```

For flash-modded iPods with an empty SysInfo (no serial in firmware), the
serial is auto-derived from the volume name as `__vol__<VolumeName>`. Add
`model: MB565` (or whichever model number) and `guid: "000A..."` (auto-detected
on registration from `ioreg`'s `kUSBSerialNumberString`).

## Interactive mode

The friendliest way in — guided prompts for everything.

```sh
uv run wheelhouse --int
```

The flow when you plug in an iPod:

1. **Unknown iPod?** Prompts for a name, music mode (all / playlists), and —
   for flash-modded devices — model number and GUID (auto-detected).
2. **Known iPod?** Shows a one-line summary and the action menu:
   - **Sync** — choose playlists, include/exclude drop folder, review the plan,
     commit.
   - **Adopt** — match existing on-iPod tracks to Navidrome and record them as
     managed. Run this once per iPod on first setup.
   - **Verify** — compare state DB against iPod disk and Navidrome; reports
     missing files, orphaned iTunesDB entries, stale metadata signatures.
   - **Status** — full device detail (model, GUID, firmware, hash58).
   - **Eject** — safe unmount.

If nothing has changed since the last sync, the whole thing collapses to a
one-line `Up to date.` — no plan table, no confirmation prompt.

## CLI mode

For scripting or when you know exactly what you want.

```sh
wheelhouse status                          # list all connected iPods
wheelhouse adopt [SERIAL]                  # first-run match to Navidrome
wheelhouse sync [SERIAL]                   # scrobble + sync music + drop folder
wheelhouse sync [SERIAL] --no-drop         # skip the drop folder entirely
wheelhouse sync [SERIAL] --dry-run         # preview without writing
wheelhouse sync [SERIAL] --eject           # eject on completion
wheelhouse verify [SERIAL]                 # state/library/Navidrome drift check
wheelhouse eject [SERIAL]                  # safe unmount
```

`[SERIAL]` is only needed when multiple iPods are connected.

## Drop folder

Anything in `~/Wheelhouse/` (configurable) gets picked up in a sync:

- `Audiobooks/<Author>/<Title>/*.m4a` → iPod Audiobooks
- `Movies/*.m4v` or `*.mp4` → iPod Videos
- `Music Videos/<Artist>/*.m4v` → iPod Music Videos
- `Notes/*.txt` → iPod Notes

Files are tracked by relative path + content hash, so you can edit metadata or
replace a file in place and the next sync picks up the change.

## How it works

- **Navidrome is the source of truth** for the music library. Wheelhouse reads
  Navidrome's track metadata, syncs the selected set onto the iPod, and
  pushes play counts back as Subsonic scrobbles.
- **State lives in SQLite** at `~/Library/Application Support/wheelhouse/state.sqlite`,
  one row per managed track, keyed by iPod serial and Navidrome track ID. This
  is how wheelhouse knows what it previously synced vs. what else is on the iPod.
- **Metadata-change detection**: each synced track stores a hash of the
  Navidrome metadata that mattered at the time (title, artist, album artist,
  size, …). If Navidrome changes any of those, the track is flagged "to update"
  and re-downloaded on the next sync, preserving play count.
- **Audio cache**: downloaded tracks land in `<cache_dir>/audio/` during a
  sync. If the sync crashes, re-running it reads from the cache and skips the
  network entirely — the retry only has to re-run the iTunesDB commit. Cache
  entries are cleaned per-track once the commit succeeds.
- **Verify** cross-checks the state DB against (a) the files on the iPod,
  (b) the iTunesDB, and (c) the current Navidrome library. Catches external
  database overwrites (e.g. another app syncing to the same iPod) and metadata
  drift that would otherwise be silent.

## Limitations

- macOS only (uses `ioreg`, `diskutil`).
- FLAC / OGG / Opus / WMA / APE / DSF / DFF are not synced — transcoding to an
  iPod-supported codec isn't wired up yet. The sync plan surfaces the
  skipped tracks so you can convert manually in Navidrome.
- One iPod GUID is auto-detected per sync; multi-iPod scenarios fall back to
  manual GUID entry in config.
- Navidrome playlists are only used as a *selection filter* — they aren't
  materialised as iPod playlists on-device yet.

## Licence

GPL-3.0-or-later — same as the upstream iOpenPod it builds on.
