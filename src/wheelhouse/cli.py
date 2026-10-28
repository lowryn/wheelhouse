"""CLI entry point: wheelhouse status | adopt | sync | eject."""
from __future__ import annotations

import datetime
import logging
import time
from pathlib import Path

import click
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Column

from wheelhouse.device import MountedIPod, apply_config_overrides, find_ipods, identify

logging.getLogger("iPodDB").setLevel(logging.ERROR)


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _print_ipod(
    ipod: MountedIPod,
    last_sync: int | None = None,
    last_playlists: list[str] | None = None,
) -> None:
    guid_str = ipod.guid.hex().upper() if ipod.guid else "—"
    hash58_str = (
        "—" if ipod.hash58_valid is None
        else ("OK" if ipod.hash58_valid else "INVALID")
    )
    click.echo(f"  Mount:    {ipod.root}")
    click.echo(f"  Model:    {ipod.display_name}")
    click.echo(f"  Serial:   {ipod.serial or '—'}")
    click.echo(f"  GUID:     {guid_str}")
    click.echo(f"  Firmware: {ipod.firmware or '—'}")
    click.echo(f"  Tracks:   {ipod.track_count}")
    click.echo(f"  Free:     {_fmt_bytes(ipod.free_bytes)}")
    click.echo(f"  DB:       {'OK' if ipod.db_ok else 'MISSING/CORRUPT'}")
    if ipod.hash58_valid is not None:
        click.echo(f"  HASH58:   {hash58_str}")
    if last_sync is not None:
        ts = datetime.datetime.fromtimestamp(last_sync).strftime("%-d %b %Y %H:%M")
        click.echo(f"  Synced:   {ts}")
    if last_playlists is not None:
        if last_playlists:
            click.echo(f"  Playlist: {', '.join(last_playlists)}")
        else:
            click.echo("  Playlist: All music")


@click.group(invoke_without_command=True)
@click.option("--int", "interactive_mode", is_flag=True, default=False,
              help="Launch interactive guided flow.")
@click.pass_context
def cli(ctx: click.Context, interactive_mode: bool) -> None:
    if interactive_mode:
        from wheelhouse.interactive import run
        run()
        ctx.exit()
    elif ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command()
def status() -> None:
    """Show all connected iPods: model, serial, GUID, track count, free space."""
    from wheelhouse import state as state_mod

    mounts = find_ipods()
    if not mounts:
        click.echo("No iPods found under /Volumes.")
        return

    errors: list[tuple[str, Exception]] = []
    found: list[MountedIPod] = []
    for root in sorted(mounts):
        try:
            found.append(identify(root))
        except Exception as exc:
            errors.append((str(root), exc))

    db = state_mod.open_state()
    for ipod in found:
        click.echo(f"\n{ipod.display_name}")
        _print_ipod(
            ipod,
            last_sync=state_mod.get_last_sync(db, ipod.serial),
            last_playlists=state_mod.get_last_playlists(db, ipod.serial),
        )
    db.close()

    for path, exc in errors:
        click.echo(f"\n{path}: ERROR — {exc}", err=True)

    click.echo(f"\n{len(found)} iPod(s) found.")


@cli.command()
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--csv-out", type=click.Path(), default=None,
              help="Write match report to this CSV file (default: adopt-<serial>.csv)")
@click.argument("serial", required=False)
def adopt(serial: str | None, dry_run: bool, csv_out: str | None) -> None:
    """Match an existing iPod's tracks to Navidrome and record them as managed."""
    from wheelhouse import adopt as adopt_mod
    from wheelhouse.config import load
    from wheelhouse.navidrome import NavidromeClient

    cfg = load()
    mounts = find_ipods()
    if not mounts:
        click.echo("No iPods found.", err=True)
        raise SystemExit(1)

    ipods = [identify(r) for r in mounts]
    if serial:
        target = next((i for i in ipods if i.serial == serial), None)
        if target is None:
            click.echo(f"No iPod with serial {serial} found.", err=True)
            raise SystemExit(1)
    elif len(ipods) == 1:
        target = ipods[0]
    else:
        click.echo("Multiple iPods connected — specify a serial:", err=True)
        for i in ipods:
            click.echo(f"  {i.serial}  {i.display_name}", err=True)
        raise SystemExit(1)

    ipod_cfg = cfg.ipod_by_serial(target.serial)
    if ipod_cfg is None:
        click.echo(
            f"iPod {target.serial} is not in config. Add it to "
            f"~/.config/wheelhouse/config.yaml first.", err=True
        )
        raise SystemExit(1)

    click.echo(f"iPod:   {target.display_name} ({target.serial})")
    click.echo(f"Config: {ipod_cfg.name}")

    nd = NavidromeClient(cfg.navidrome.url, cfg.navidrome.user, cfg.navidrome.password())
    click.echo("Fetching Navidrome library…", nl=False)
    plan = adopt_mod.plan(target, ipod_cfg, nd)
    click.echo(f" {len(plan.all_songs)} songs")

    click.echo(f"Tracks on iPod: {len(plan.ipod_tracks)}")
    click.echo(f"Desired from Navidrome: {len(plan.desired_songs)} songs")

    click.echo(f"\nMatch results:")
    click.echo(f"  Matched:           {len(plan.result.matched)}")
    click.echo(f"  Ambiguous:         {len(plan.result.ambiguous)}")
    click.echo(f"  Unmatched on iPod: {len(plan.result.unmatched_ipod)}")
    click.echo(f"  Missing from iPod: {len(plan.result.missing_from_ipod)}")

    out_path = Path(csv_out) if csv_out else Path(f"adopt-{plan.target.serial}.csv")
    adopt_mod.write_csv(plan, out_path)
    click.echo(f"\nReport written to {out_path}")

    if dry_run:
        click.echo("(dry run — nothing written to iPod or state)")
        return

    saved = adopt_mod.save(plan, ipod_cfg)
    click.echo(f"State saved: {saved} tracks managed.")
    if plan.result.missing_from_ipod:
        click.echo(
            f"{len(plan.result.missing_from_ipod)} Navidrome tracks not on iPod — "
            "run `wheelhouse sync` to download them."
        )


from wheelhouse.state import nd_sig as _nd_sig


@cli.command()
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--eject", is_flag=True, default=False)
@click.option("--no-drop", "skip_drop", is_flag=True, default=False,
              help="Skip the drop folder (audiobooks, videos, notes).")
@click.argument("serial", required=False)
def sync(serial: str | None, dry_run: bool, eject: bool, skip_drop: bool) -> None:
    """Sync an iPod: scrobble plays, add missing tracks, remove unwanted tracks."""
    from wheelhouse.config import load
    from wheelhouse.device import load_library, build_write_target
    from wheelhouse.navidrome import NavidromeClient
    from wheelhouse import state as state_mod
    from wheelhouse import writer

    cfg = load()
    mounts = find_ipods()
    if not mounts:
        click.echo("No iPods found.", err=True)
        raise SystemExit(1)

    ipods = [identify(r) for r in mounts]
    if serial:
        target = next((i for i in ipods if i.serial == serial), None)
        if target is None:
            click.echo(f"No iPod with serial {serial} found.", err=True)
            raise SystemExit(1)
    elif len(ipods) == 1:
        target = ipods[0]
    else:
        click.echo("Multiple iPods connected — specify a serial:", err=True)
        for i in ipods:
            click.echo(f"  {i.serial}  {i.display_name}", err=True)
        raise SystemExit(1)

    ipod_cfg = cfg.ipod_by_serial(target.serial)
    if ipod_cfg is None:
        click.echo(f"iPod {target.serial} not in config.", err=True)
        raise SystemExit(1)

    target = apply_config_overrides(target, ipod_cfg)

    db = state_mod.open_state()
    if not state_mod.is_adopted(db, target.serial):
        click.echo(
            "iPod not adopted — run `wheelhouse adopt` first.", err=True
        )
        raise SystemExit(1)
    managed = state_mod.get_managed(db, target.serial)

    click.echo(f"iPod:  {target.display_name} ({target.serial})")
    click.echo(f"State: {len(managed)} managed tracks")

    wt, profile = build_write_target(target.model, target.guid)
    lib = load_library(target.root, wt)
    ipod_by_tid = {t.track_id: t for t in lib.snapshot.tracks}

    # ── Scrobble plays ────────────────────────────────────────────────────
    nd = NavidromeClient(cfg.navidrome.url, cfg.navidrome.user, cfg.navidrome.password())
    from wheelhouse.scrobble import push_plays
    report = push_plays(db, nd, target.serial, managed, ipod_by_tid, dry_run=dry_run)
    if report.scrobbled:
        click.echo(f"Plays scrobbled: {report.scrobbled}")
    if report.failed:
        click.echo(
            f"[warning] {report.failed} play(s) failed to scrobble — will retry next sync.",
            err=True,
        )

    # ── Fetch desired set ─────────────────────────────────────────────────
    click.echo("Fetching Navidrome library…", nl=False)
    all_songs = nd.all_songs()
    click.echo(f" {len(all_songs)} songs")

    if ipod_cfg.music.mode == "all":
        desired_songs = all_songs
    else:
        desired_ids: set[str] = set()
        for pl in nd.playlists():
            if ipod_cfg.music.matches_playlist(pl["name"]):
                for entry in nd.playlist_songs(pl["id"]):
                    desired_ids.add(entry["id"])
        by_id = {s["id"]: s for s in all_songs}
        desired_songs = [by_id[i] for i in desired_ids if i in by_id]

    from wheelhouse import dropfolder as df_mod

    desired_by_id  = {s["id"]: s for s in desired_songs}
    desired_nd_ids = set(desired_by_id.keys())
    managed_nd_ids = set(managed.keys())

    # Tracks to download: desired but not yet managed
    to_add_ids = desired_nd_ids - managed_nd_ids
    unsupported = {
        nd_id for nd_id in to_add_ids
        if (desired_by_id[nd_id].get("suffix") or "").lower() in writer.SKIP_FORMATS
    }
    to_add_ids -= unsupported

    # Tracks already on iPod that are still desired (will be kept)
    keep_nd_ids = managed_nd_ids & desired_nd_ids
    all_current_tids = {t.track_id for t in lib.snapshot.tracks}
    managed_to_drop = managed_nd_ids - desired_nd_ids

    # Tracks whose Navidrome metadata/file has changed since last sync
    to_update_ids = {
        nd_id for nd_id in keep_nd_ids
        if managed[nd_id].source_sig
        and managed[nd_id].source_sig != _nd_sig(desired_by_id[nd_id])
    }
    keep_tids: set[int] = {
        managed[nd_id].ipod_track_id
        for nd_id in keep_nd_ids - to_update_ids
    }

    # ── Drop folder ───────────────────────────────────────────────────────
    drop_managed = state_mod.get_drop_managed(db, target.serial)
    drop_scan = [] if skip_drop else (df_mod.scan(cfg.drop_folder) if cfg.drop_folder.exists() else [])
    drop_by_path = {item.rel_path: item for item in drop_scan}
    # Items whose DB track was removed by an external sync need re-adding.
    current_locations = {t.metadata.location for t in lib.snapshot.tracks}
    drop_stale = {
        p for p, row in drop_managed.items()
        if p in drop_by_path and row.ipod_path and row.ipod_path not in current_locations
    }
    drop_to_add = [item for item in drop_scan if item.rel_path not in drop_managed or item.rel_path in drop_stale]
    drop_to_remove = {p for p in drop_managed if p not in drop_by_path}
    keep_tids.update(
        drop_managed[p].ipod_track_id for p in drop_managed
        if p in drop_by_path and drop_managed[p].ipod_track_id > 0 and p not in drop_stale
    )

    tracks_to_remove = len(all_current_tids) - len(keep_tids)
    plan_has_work = bool(
        to_add_ids or to_update_ids or tracks_to_remove or drop_to_add or drop_to_remove
    )

    if plan_has_work:
        click.echo(f"\nDesired:    {len(desired_songs)}")
        click.echo(
            f"To add:     {len(to_add_ids)}  "
            f"({len(unsupported)} unsupported formats skipped)"
        )
        if to_update_ids:
            click.echo(f"To update:  {len(to_update_ids)}")
        click.echo(f"To remove:  {tracks_to_remove} "
                   f"({len(managed_to_drop)} managed + orphans/unmanaged)")
        if drop_scan or drop_managed:
            click.echo(
                f"Drop:       {len(drop_to_add)} to add, {len(drop_to_remove)} to remove"
            )

        if unsupported:
            click.echo("\nSkipped (transcoding not yet supported):")
            for nd_id in sorted(unsupported):
                s = desired_by_id[nd_id]
                fmt = (s.get("suffix") or "?").upper()
                click.echo(f"  [{fmt}] {s.get('artist', '')} — {s.get('title', '')}")

    if dry_run:
        if plan_has_work:
            click.echo("\n(dry run — no changes written)")
        else:
            click.echo("No changes.")
        db.close()
        return

    # ── 1. Add Navidrome tracks ───────────────────────────────────────────
    newly_added: list[tuple[str, str]] = []
    if to_add_ids:
        songs_to_add = [desired_by_id[nd_id] for nd_id in sorted(to_add_ids)]
        click.echo(f"\nDownloading {len(songs_to_add)} tracks…")

        with Progress(
            TextColumn("[progress.description]{task.description}", table_column=Column(min_width=62)),
            BarColumn(),
            MofNCompleteColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            transient=True,
        ) as prog:
            task = prog.add_task("Downloading", total=len(songs_to_add))

            def _progress(i: int, title: str) -> None:
                prog.update(task, completed=i,
                            description=f"[cyan]{title[:60]}[/cyan]")

            lib, newly_added = writer.add_tracks(
                target.root, lib, wt, profile, nd, songs_to_add,
                on_progress=_progress, cache_dir=cfg.cache_dir,
            )
            prog.update(task, completed=len(songs_to_add),
                        description="[green]Done[/green]")

        click.echo(f"  Added {len(newly_added)} tracks.")

        added_ts = int(time.time())
        ipod_by_loc = {t.metadata.location: t for t in lib.snapshot.tracks}
        for nd_id, ipod_path in newly_added:
            ipod_track = ipod_by_loc.get(ipod_path)
            if ipod_track:
                keep_tids.add(ipod_track.track_id)
                state_mod.upsert_item(
                    db, target.serial, nd_id,
                    ipod_track_id=ipod_track.track_id,
                    ipod_path=ipod_path,
                    play_count=0,
                    match_method="sync",
                    adopted_at=added_ts,
                    source_sig=_nd_sig(desired_by_id[nd_id]),
                )

    # ── 1b. Update tracks whose metadata/file changed ────────────────────
    if to_update_ids:
        songs_to_update = [desired_by_id[nd_id] for nd_id in sorted(to_update_ids)]
        click.echo(f"\nUpdating {len(songs_to_update)} tracks…")

        with Progress(
            TextColumn("[progress.description]{task.description}", table_column=Column(min_width=62)),
            BarColumn(),
            MofNCompleteColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            transient=True,
        ) as prog:
            task = prog.add_task("Updating", total=len(songs_to_update))

            def _upd_progress(i: int, title: str) -> None:
                prog.update(task, completed=i,
                            description=f"[cyan]{title[:60]}[/cyan]")

            lib, update_added = writer.add_tracks(
                target.root, lib, wt, profile, nd, songs_to_update,
                on_progress=_upd_progress, cache_dir=cfg.cache_dir,
            )
            prog.update(task, completed=len(songs_to_update),
                        description="[green]Done[/green]")

        click.echo(f"  Updated {len(update_added)} tracks.")

        update_ts = int(time.time())
        ipod_by_loc = {t.metadata.location: t for t in lib.snapshot.tracks}
        for nd_id, ipod_path in update_added:
            ipod_track = ipod_by_loc.get(ipod_path)
            if ipod_track:
                keep_tids.add(ipod_track.track_id)
                state_mod.upsert_item(
                    db, target.serial, nd_id,
                    ipod_track_id=ipod_track.track_id,
                    ipod_path=ipod_path,
                    play_count=managed[nd_id].play_count,
                    match_method="sync",
                    adopted_at=update_ts,
                    source_sig=_nd_sig(desired_by_id[nd_id]),
                )

    # ── 2. Add drop folder tracks ─────────────────────────────────────────
    for rel_path in drop_stale:
        stale_ipod_path = drop_managed[rel_path].ipod_path
        if stale_ipod_path:
            stale_file = target.root / stale_ipod_path
            stale_file.unlink(missing_ok=True)

    if drop_to_add:
        click.echo(f"\nAdding {len(drop_to_add)} tracks from drop folder…")

        with Progress(
            TextColumn("[progress.description]{task.description}", table_column=Column(min_width=62)),
            BarColumn(),
            MofNCompleteColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            transient=True,
        ) as prog:
            task = prog.add_task("Copying", total=len(drop_to_add))

            def _drop_prog(i: int, name: str) -> None:
                prog.update(task, completed=i,
                            description=f"[cyan]{name[:60]}[/cyan]")

            lib, drop_added = writer.add_drop_tracks(
                target.root, lib, wt, profile, drop_to_add, cfg.drop_folder,
                on_progress=_drop_prog,
            )
            prog.update(task, completed=len(drop_to_add),
                        description="[green]Done[/green]")

        click.echo(f"  Added {len(drop_added)} tracks.")
        drop_ts = int(time.time())
        ipod_by_loc = {t.metadata.location: t for t in lib.snapshot.tracks}
        for rel_path, ipod_path in drop_added:
            ipod_track = ipod_by_loc.get(ipod_path)
            if ipod_track:
                keep_tids.add(ipod_track.track_id)
                state_mod.upsert_drop_item(
                    db, target.serial, rel_path,
                    source_sig=drop_by_path[rel_path].source_sig,
                    ipod_track_id=ipod_track.track_id,
                    ipod_path=ipod_path,
                    adopted_at=drop_ts,
                )

    # ── 3. Remove everything not in keep_tids ────────────────────────────
    remove_tids = {t.track_id for t in lib.snapshot.tracks} - keep_tids
    if remove_tids:
        remove_paths = [
            t.metadata.location
            for t in lib.snapshot.tracks
            if t.track_id in remove_tids
        ]
        click.echo(f"\nRemoving {len(remove_tids)} tracks…")
        lib = writer.remove_tracks(target.root, lib, wt, remove_tids, remove_paths)
        for nd_id in managed_to_drop:
            state_mod.remove_item(db, target.serial, nd_id)
        for rel_path in drop_to_remove:
            state_mod.remove_drop_item(db, target.serial, rel_path)
        click.echo("  Done.")

    # ── 4. Fix metadata on tracks written before current conventions ────
    lib, comp_fixes, artist_fixes = writer.fix_track_metadata(
        target.root, lib, wt, desired_songs, managed
    )
    if comp_fixes or artist_fixes:
        click.echo(f"\nFixed metadata: {comp_fixes} compilation flags, {artist_fixes} track artists.")

    # ── 5. Update artwork for tracks missing it ───────────────────────────
    managed = state_mod.get_managed(db, target.serial)
    tracks_no_art = sum(1 for t in lib.snapshot.tracks if t.artwork_id == 0)
    if tracks_no_art and managed:
        click.echo(f"\nUpdating artwork ({tracks_no_art} tracks without)…")
        with Progress(
            TextColumn("[progress.description]{task.description}", table_column=Column(min_width=62)),
            BarColumn(),
            MofNCompleteColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            transient=True,
        ) as prog:
            task = prog.add_task("Artwork", total=tracks_no_art)

            def _art_prog(i: int, title: str) -> None:
                prog.update(task, completed=i,
                            description=f"[cyan]{title[:60]}[/cyan]")

            lib, art_count = writer.update_artwork(
                target.root, lib, wt, nd, managed, all_songs,
                on_progress=_art_prog,
            )
            prog.update(task, completed=tracks_no_art,
                        description="[green]Done[/green]")

        if art_count:
            click.echo(f"  Updated {art_count} tracks.")

    # ── 6. Sync notes ────────────────────────────────────────────────────
    notes_added = notes_removed = 0
    if not skip_drop:
        notes_added, notes_removed = writer.sync_notes(target.root, cfg.drop_folder)
        if notes_added or notes_removed:
            click.echo(f"\nNotes: {notes_added} updated, {notes_removed} removed.")

    # ── 7. Finalise ───────────────────────────────────────────────────────
    # Backfill sigs for kept tracks that predate change-detection.
    backfill = {
        nd_id: _nd_sig(desired_by_id[nd_id])
        for nd_id in keep_nd_ids - to_update_ids
        if not managed[nd_id].source_sig and nd_id in desired_by_id
    }
    if backfill:
        state_mod.bulk_set_source_sig(db, target.serial, backfill)

    state_mod.upsert_ipod(
        db, target.serial, ipod_cfg.name, target.model, target.guid,
        int(time.time()),
    )
    state_mod.set_last_sync(db, target.serial, int(time.time()))
    db.commit()
    db.close()

    any_work = (
        plan_has_work or report.scrobbled or report.failed
        or comp_fixes or artist_fixes or notes_added or notes_removed
        or (tracks_no_art and managed)
    )
    click.echo("\nSync complete." if any_work else "Up to date.")

    if eject:
        import subprocess
        subprocess.run(["diskutil", "eject", str(target.root)], check=True)
        click.echo(f"Ejected {target.root}")


@cli.command()
@click.argument("serial", required=False)
def eject(serial: str | None) -> None:
    """Eject a connected iPod safely."""
    import subprocess

    mounts = find_ipods()
    if not mounts:
        click.echo("No iPods found.", err=True)
        raise SystemExit(1)

    ipods = [identify(r) for r in mounts]
    if serial:
        target = next((i for i in ipods if i.serial == serial), None)
        if target is None:
            click.echo(f"No iPod with serial {serial} found.", err=True)
            raise SystemExit(1)
    elif len(ipods) == 1:
        target = ipods[0]
    else:
        click.echo("Multiple iPods connected — specify a serial:", err=True)
        for i in ipods:
            click.echo(f"  {i.serial}  {i.display_name}", err=True)
        raise SystemExit(1)

    subprocess.run(["diskutil", "eject", str(target.root)], check=True)
    click.echo(f"Ejected {target.display_name} ({target.serial})")
