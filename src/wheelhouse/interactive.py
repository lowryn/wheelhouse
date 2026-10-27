"""Interactive guided flow: wheelhouse --int."""
from __future__ import annotations

import subprocess
import time

import questionary
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.rule import Rule
from rich.table import Table

from wheelhouse import state as state_mod
from wheelhouse import writer
from wheelhouse import dropfolder as df_mod
from wheelhouse.config import Config, IPodConfig, append_ipod_to_config, load as load_config
from wheelhouse.device import MountedIPod, build_write_target, find_ipods, identify, load_library
from wheelhouse.navidrome import NavidromeClient

console = Console()


def _register_unknown_ipod(ipod: MountedIPod) -> bool:
    """Prompt for name + music mode and write to config. Returns True on success."""
    console.print(f"\n[yellow]Unknown iPod:[/yellow] {ipod.display_name} ({ipod.serial})")
    name = questionary.text("Name for this iPod:").ask()
    if not name:
        return False
    mode = questionary.select(
        "Default music mode:",
        choices=["All music", "Specific playlists (choose each sync)"],
    ).ask()
    if mode is None:
        return False
    append_ipod_to_config(name, ipod.serial, music_all=(mode == "All music"))
    return True


def _ipod_label(ipod: MountedIPod, cfg) -> str:
    ipod_cfg = cfg.ipod_by_serial(ipod.serial)
    name = ipod_cfg.name if ipod_cfg else "Unknown"
    return f"{name}  ({ipod.display_name})"


def _select_playlists(nd: NavidromeClient, last_playlists: list[str] | None) -> list[str] | None | bool:
    """
    Ask whether to sync all music or specific playlists.
    Returns:
      None        — "All music" chosen
      list[str]   — specific playlist names chosen
      False       — user went back / cancelled
    """
    all_was_last = last_playlists is None or last_playlists == []

    top = questionary.select(
        "Music to sync:",
        choices=[
            questionary.Choice("All music", value="all"),
            questionary.Choice("Select playlists…", value="playlists"),
            questionary.Choice("Back", value="back"),
        ],
        default="all" if all_was_last else "playlists",
    ).ask()

    if top is None or top == "back":
        return False
    if top == "all":
        return None

    # Specific playlists
    console.print("Fetching playlists…", end=" ")
    all_pls = sorted(nd.playlists(), key=lambda p: p["name"])
    console.print(f"{len(all_pls)} playlists")

    hint = f" (last: {', '.join(last_playlists)})" if last_playlists else ""
    choices = [
        questionary.Choice(pl["name"], checked=(pl["name"] in (last_playlists or [])))
        for pl in all_pls
    ]

    selected = questionary.checkbox(
        f"Select playlists{hint}:",
        choices=choices,
    ).ask()

    if not selected:
        return False
    return selected


def _do_sync(
    target: MountedIPod,
    ipod_cfg: IPodConfig,
    cfg: Config,
    nd: NavidromeClient,
    selected_playlists: list[str] | None,
) -> str:
    """
    Core sync: scrobble → plan → confirm → execute.
    Returns 'done' | 'back' | 'quit'.
    """
    db = state_mod.open_state()
    if not state_mod.is_adopted(db, target.serial):
        console.print("[yellow]iPod not adopted — run `wheelhouse adopt` first.[/yellow]")
        db.close()
        return "back"
    managed = state_mod.get_managed(db, target.serial)

    wt, profile = build_write_target(target.model, target.guid)
    lib = load_library(target.root, wt)
    ipod_by_tid = {t.track_id: t for t in lib.snapshot.tracks}

    # ── Scrobble plays ────────────────────────────────────────────────────────
    scrobble_count = 0
    now_ms = int(time.time() * 1000)
    for nd_id, item in managed.items():
        t = ipod_by_tid.get(item.ipod_track_id)
        if t is None or t.play_count <= item.play_count:
            continue
        delta = t.play_count - item.play_count
        for _ in range(delta):
            try:
                nd.scrobble(nd_id, now_ms)
                state_mod.record_scrobble(db, target.serial, nd_id, int(time.time()))
            except Exception:
                pass
        state_mod.update_play_count(db, target.serial, nd_id, t.play_count)
        managed[nd_id].play_count = t.play_count
        scrobble_count += delta
    console.print(f"Plays scrobbled: {scrobble_count}")

    # ── Desired set ───────────────────────────────────────────────────────────
    console.print("Fetching Navidrome library…", end=" ")
    all_songs = nd.all_songs()
    console.print(f"{len(all_songs)} songs")

    if selected_playlists is None:
        desired_songs = all_songs
    else:
        desired_ids: set[str] = set()
        pl_by_name = {pl["name"]: pl for pl in nd.playlists()}
        for pl_name in selected_playlists:
            pl = pl_by_name.get(pl_name)
            if pl:
                for entry in nd.playlist_songs(pl["id"]):
                    desired_ids.add(entry["id"])
        by_id_map = {s["id"]: s for s in all_songs}
        desired_songs = [by_id_map[i] for i in desired_ids if i in by_id_map]

    desired_by_id = {s["id"]: s for s in desired_songs}
    desired_nd_ids = set(desired_by_id.keys())
    managed_nd_ids = set(managed.keys())

    to_add_ids = desired_nd_ids - managed_nd_ids
    unsupported = {
        nd_id for nd_id in to_add_ids
        if (desired_by_id[nd_id].get("suffix") or "").lower() in writer.SKIP_FORMATS
    }
    to_add_ids -= unsupported

    keep_nd_ids = managed_nd_ids & desired_nd_ids
    keep_tids: set[int] = {managed[nd_id].ipod_track_id for nd_id in keep_nd_ids}
    all_current_tids = {t.track_id for t in lib.snapshot.tracks}
    managed_to_drop = managed_nd_ids - desired_nd_ids

    # ── Drop folder ───────────────────────────────────────────────────────────
    drop_managed = state_mod.get_drop_managed(db, target.serial)
    drop_scan = df_mod.scan(cfg.drop_folder) if cfg.drop_folder.exists() else []
    drop_by_path = {item.rel_path: item for item in drop_scan}
    drop_to_add = [item for item in drop_scan if item.rel_path not in drop_managed]
    drop_to_remove = {p for p in drop_managed if p not in drop_by_path}
    keep_tids.update(
        drop_managed[p].ipod_track_id for p in drop_managed
        if p in drop_by_path and drop_managed[p].ipod_track_id > 0
    )

    # ── Plan table ────────────────────────────────────────────────────────────
    tbl = Table.grid(padding=(0, 2))
    tbl.add_column(style="dim")
    tbl.add_column()
    tbl.add_row("Desired", str(len(desired_songs)))
    tbl.add_row(
        "To add",
        f"{len(to_add_ids)}"
        + (f"  [dim]({len(unsupported)} unsupported skipped)[/dim]" if unsupported else ""),
    )
    tbl.add_row(
        "To remove",
        f"{len(all_current_tids) - len(keep_tids)}"
        + (f"  [dim]({len(managed_to_drop)} managed + orphans)[/dim]" if managed_to_drop else ""),
    )
    if drop_scan or drop_managed:
        tbl.add_row("Drop", f"{len(drop_to_add)} to add, {len(drop_to_remove)} to remove")
    console.print()
    console.print(tbl)

    if unsupported:
        console.print("\n[dim]Skipped (transcoding not yet supported):[/dim]")
        for nd_id in sorted(unsupported):
            s = desired_by_id[nd_id]
            fmt = (s.get("suffix") or "?").upper()
            console.print(f"  [dim][{fmt}] {s.get('artist', '')} — {s.get('title', '')}[/dim]")

    console.print()

    # ── Confirm ───────────────────────────────────────────────────────────────
    plan_action = questionary.select(
        "Action:",
        choices=["Sync", "Back", "Quit"],
    ).ask()

    if plan_action is None or plan_action == "Quit":
        db.close()
        return "quit"
    if plan_action == "Back":
        db.close()
        return "back"

    # ── 1. Add Navidrome tracks ───────────────────────────────────────────────
    newly_added: list[tuple[str, str]] = []
    if to_add_ids:
        songs_to_add = [desired_by_id[nd_id] for nd_id in sorted(to_add_ids)]
        console.print(f"\nDownloading {len(songs_to_add)} tracks…")

        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(), MofNCompleteColumn(), TaskProgressColumn(), TimeRemainingColumn(),
            transient=True,
        ) as prog:
            task = prog.add_task("Downloading", total=len(songs_to_add))

            def _progress(i: int, title: str) -> None:
                prog.update(task, completed=i, description=f"[cyan]{title[:60]}[/cyan]")

            lib, newly_added = writer.add_tracks(
                target.root, lib, wt, profile, nd, songs_to_add,
                on_progress=_progress,
            )
            prog.update(task, completed=len(songs_to_add), description="[green]Done[/green]")

        console.print(f"  Added {len(newly_added)} tracks.")

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
                )

    # ── 2. Add drop folder tracks ─────────────────────────────────────────────
    if drop_to_add:
        console.print(f"\nAdding {len(drop_to_add)} tracks from drop folder…")

        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(), MofNCompleteColumn(), TaskProgressColumn(), TimeRemainingColumn(),
            transient=True,
        ) as prog:
            task = prog.add_task("Copying", total=len(drop_to_add))

            def _drop_prog(i: int, name: str) -> None:
                prog.update(task, completed=i, description=f"[cyan]{name[:60]}[/cyan]")

            lib, drop_added = writer.add_drop_tracks(
                target.root, lib, wt, profile, drop_to_add, cfg.drop_folder,
                on_progress=_drop_prog,
            )
            prog.update(task, completed=len(drop_to_add), description="[green]Done[/green]")

        console.print(f"  Added {len(drop_added)} tracks.")
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

    # ── 3. Remove tracks ──────────────────────────────────────────────────────
    remove_tids = {t.track_id for t in lib.snapshot.tracks} - keep_tids
    if remove_tids:
        remove_paths = [
            t.metadata.location
            for t in lib.snapshot.tracks
            if t.track_id in remove_tids
        ]
        console.print(f"\nRemoving {len(remove_tids)} tracks…")
        lib = writer.remove_tracks(target.root, lib, wt, remove_tids, remove_paths)
        for nd_id in managed_to_drop:
            state_mod.remove_item(db, target.serial, nd_id)
        for rel_path in drop_to_remove:
            state_mod.remove_drop_item(db, target.serial, rel_path)
        console.print("  Done.")

    # ── 4. Update artwork for tracks missing it ───────────────────────────────
    managed = state_mod.get_managed(db, target.serial)
    tracks_no_art = sum(1 for t in lib.snapshot.tracks if t.artwork_id == 0)
    if tracks_no_art and managed:
        console.print(f"\nUpdating artwork ({tracks_no_art} tracks without)…")
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(), MofNCompleteColumn(), TaskProgressColumn(), TimeRemainingColumn(),
            transient=True,
        ) as prog:
            task = prog.add_task("Artwork", total=tracks_no_art)

            def _art_prog(i: int, title: str) -> None:
                prog.update(task, completed=i, description=f"[cyan]{title[:60]}[/cyan]")

            lib, art_count = writer.update_artwork(
                target.root, lib, wt, nd, managed, all_songs,
                on_progress=_art_prog,
            )
            prog.update(task, completed=tracks_no_art, description="[green]Done[/green]")

        if art_count:
            console.print(f"  Updated {art_count} tracks.")

    # ── 5. Finalise ───────────────────────────────────────────────────────────
    state_mod.upsert_ipod(
        db, target.serial, ipod_cfg.name, target.model, target.guid, int(time.time())
    )
    state_mod.set_last_sync(db, target.serial, int(time.time()))
    state_mod.set_last_playlists(
        db, target.serial,
        selected_playlists if selected_playlists is not None else [],
    )
    db.commit()
    db.close()
    console.print("\n[green]Sync complete.[/green]")
    return "done"


def _print_ipod_summary(target: MountedIPod) -> None:
    """Print a compact one-line status for the selected iPod."""
    import datetime
    from wheelhouse.cli import _fmt_bytes
    db = state_mod.open_state()
    last_sync = state_mod.get_last_sync(db, target.serial)
    last_pls = state_mod.get_last_playlists(db, target.serial)
    db.close()

    parts = [f"[dim]{target.track_count} tracks[/dim]",
             f"[dim]{_fmt_bytes(target.free_bytes)} free[/dim]"]
    if last_sync:
        ts = datetime.datetime.fromtimestamp(last_sync).strftime("%-d %b %Y %H:%M")
        parts.append(f"[dim]last sync {ts}[/dim]")
    if last_pls is not None:
        pl_str = ", ".join(last_pls) if last_pls else "All music"
        parts.append(f"[dim]{pl_str}[/dim]")
    console.print("  " + "  ·  ".join(parts))


def run() -> None:
    """Entry point for `wheelhouse --int`."""
    console.print(Rule("[bold]Wheelhouse[/bold]", style="dim"))

    cfg = load_config()
    mounts = find_ipods()
    if not mounts:
        console.print("[red]No iPods found under /Volumes.[/red]")
        return

    # Identify all iPods; prompt for unknown ones
    ipods: list[MountedIPod] = []
    for root in sorted(mounts):
        try:
            ipod = identify(root)
            if cfg.ipod_by_serial(ipod.serial) is None:
                if not _register_unknown_ipod(ipod):
                    continue
                cfg = load_config()
            ipods.append(ipod)
        except Exception as exc:
            console.print(f"[red]{root}: {exc}[/red]")

    if not ipods:
        return

    nd = NavidromeClient(cfg.navidrome.url, cfg.navidrome.user, cfg.navidrome.password())

    while ipods:
        # ── iPod selection ────────────────────────────────────────────────────
        if len(ipods) == 1:
            target = ipods[0]
            console.print(f"\nConnected: [bold]{_ipod_label(target, cfg)}[/bold]")
            _print_ipod_summary(target)
        else:
            choice = questionary.select(
                "Select iPod:",
                choices=[
                    questionary.Choice(_ipod_label(i, cfg), value=i) for i in ipods
                ] + ["Quit"],
            ).ask()
            if choice is None or choice == "Quit":
                break
            target = choice
            _print_ipod_summary(target)

        ipod_cfg = cfg.ipod_by_serial(target.serial)

        # ── Action menu ───────────────────────────────────────────────────────
        while True:
            console.print(Rule(style="dim"))
            action = questionary.select(
                f"{ipod_cfg.name} — what would you like to do?",
                choices=["Sync", "Adopt", "Status", "Eject", "Back", "Quit"],
            ).ask()

            if action is None or action == "Quit":
                return
            if action == "Back":
                break

            if action == "Status":
                from wheelhouse.cli import _print_ipod
                console.print()
                _print_ipod(target)
                continue

            if action == "Eject":
                subprocess.run(["diskutil", "eject", str(target.root)], check=True)
                console.print(f"Ejected [bold]{ipod_cfg.name}[/bold]")
                ipods.remove(target)
                break

            if action == "Adopt":
                console.print("[yellow]Use `wheelhouse adopt` from the command line.[/yellow]")
                continue

            if action == "Sync":
                db = state_mod.open_state()
                last_pls = state_mod.get_last_playlists(db, target.serial)
                db.close()

                # Playlist selection loop
                while True:
                    selected = _select_playlists(nd, last_pls)
                    if selected is False:
                        break  # back to action menu

                    result = _do_sync(target, ipod_cfg, cfg, nd, selected)
                    if result == "quit":
                        return
                    if result == "back":
                        continue  # back to playlist selection

                    # Sync done — ask eject
                    do_eject = questionary.confirm("Eject iPod?", default=False).ask()
                    if do_eject:
                        subprocess.run(["diskutil", "eject", str(target.root)], check=True)
                        console.print(f"Ejected [bold]{ipod_cfg.name}[/bold]")
                        ipods.remove(target)
                    break
                break  # back to iPod selection after sync
