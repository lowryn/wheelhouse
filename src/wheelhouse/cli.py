"""CLI entry point: wheelhouse status | adopt | sync | eject."""
from __future__ import annotations

import click

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
@click.argument("serial", required=False)
def adopt(serial: str | None, dry_run: bool, import_playlists: bool, reencode: bool) -> None:
    """Match an existing iPod's tracks to Navidrome and record them as managed."""
    raise NotImplementedError


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
