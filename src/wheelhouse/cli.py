"""CLI entry point: wheelhouse status | adopt | sync | eject."""
from __future__ import annotations

import click


@click.group()
def cli() -> None:
    pass


@cli.command()
def status() -> None:
    """Show all connected iPods: model, serial, GUID, track count, free space."""
    raise NotImplementedError


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
