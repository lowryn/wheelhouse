"""CLI entry point: wheelhouse status | adopt | sync | eject."""
from __future__ import annotations

import click

import csv
import sys
from pathlib import Path

from wheelhouse.device import MountedIPod, find_ipods, identify


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _print_ipod(ipod: MountedIPod) -> None:
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


@click.group()
def cli() -> None:
    pass


@cli.command()
def status() -> None:
    """Show all connected iPods: model, serial, GUID, track count, free space."""
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

    for ipod in found:
        click.echo(f"\n{ipod.display_name}")
        _print_ipod(ipod)

    for path, exc in errors:
        click.echo(f"\n{path}: ERROR — {exc}", err=True)

    click.echo(f"\n{len(found)} iPod(s) found.")


@cli.command()
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--import-playlists", is_flag=True, default=False)
@click.option("--reencode", is_flag=True, default=False)
@click.option("--csv-out", type=click.Path(), default=None,
              help="Write match report to this CSV file (default: adopt-<serial>.csv)")
@click.argument("serial", required=False)
def adopt(serial: str | None, dry_run: bool, import_playlists: bool,
          reencode: bool, csv_out: str | None) -> None:
    """Match an existing iPod's tracks to Navidrome and record them as managed."""
    from wheelhouse.adopt import match
    from wheelhouse.config import load
    from wheelhouse.device import load_library, build_write_target
    from wheelhouse.navidrome import NavidromeClient

    cfg = load()
    mounts = find_ipods()
    if not mounts:
        click.echo("No iPods found.", err=True)
        raise SystemExit(1)

    # Pick the right mount
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

    # Load iPod library
    wt, _profile = build_write_target(target.model, target.guid)
    lib = load_library(target.root, wt)
    ipod_tracks = list(lib.snapshot.tracks)
    click.echo(f"Tracks on iPod: {len(ipod_tracks)}")

    # Fetch Navidrome songs
    nd = NavidromeClient(cfg.navidrome.url, cfg.navidrome.user, cfg.navidrome.password())
    click.echo("Fetching Navidrome library…", nl=False)
    all_songs = nd.all_songs()
    click.echo(f" {len(all_songs)} songs")

    # Determine desired set for this iPod
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

    click.echo(f"Desired from Navidrome: {len(desired_songs)} songs")

    result = match(ipod_tracks, desired_songs)

    # Summary
    click.echo(f"\nMatch results:")
    click.echo(f"  Matched:           {len(result.matched)}")
    click.echo(f"  Ambiguous:         {len(result.ambiguous)}")
    click.echo(f"  Unmatched on iPod: {len(result.unmatched_ipod)}")
    click.echo(f"  Missing from iPod: {len(result.missing_from_ipod)}")

    by_id = {s["id"]: s for s in all_songs}

    # Write CSV report
    out_path = Path(csv_out) if csv_out else Path(f"adopt-{target.serial}.csv")
    ipod_by_id = {t.track_id: t for t in ipod_tracks}
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["status", "method", "ipod_track_id", "ipod_title", "ipod_artist",
                    "ipod_album", "nd_id", "nd_title", "nd_artist", "nd_album", "ipod_path"])
        for (tid, nd_id, path, method) in result.matched:
            t = ipod_by_id[tid]
            s = by_id.get(nd_id, {})
            w.writerow(["matched", method, tid, t.title, t.artist, t.album,
                        nd_id, s.get("title",""), s.get("artist",""), s.get("album",""), path])
        for (tid, title, cands) in result.ambiguous:
            t = ipod_by_id[tid]
            w.writerow(["ambiguous", "", tid, t.title, t.artist, t.album,
                        "|".join(cands), "", "", "", t.metadata.location])
        for tid in result.unmatched_ipod:
            t = ipod_by_id[tid]
            w.writerow(["unmatched_ipod", "", tid, t.title, t.artist, t.album,
                        "", "", "", "", t.metadata.location])
        for nd_id in result.missing_from_ipod:
            s = by_id.get(nd_id, {})
            w.writerow(["missing_from_ipod", "", "", "", "", "",
                        nd_id, s.get("title",""), s.get("artist",""), s.get("album",""), ""])

    click.echo(f"\nReport written to {out_path}")
    if dry_run:
        click.echo("(dry run — nothing written to iPod or state)")
    else:
        click.echo("Re-run without --dry-run to record matched tracks in state.")


@cli.command()
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--eject", is_flag=True, default=False)
@click.argument("serial", required=False)
def sync(serial: str | None, dry_run: bool, eject: bool) -> None:
    """Sync an iPod: scrobble plays, fetch changes, write database."""
    raise NotImplementedError


@cli.command()
@click.argument("serial", required=False)
def eject(serial: str | None) -> None:
    """Eject an iPod cleanly."""
    raise NotImplementedError
