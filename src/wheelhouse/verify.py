"""Consistency check between iPod state, iPod library on disk, and Navidrome."""
from __future__ import annotations

from dataclasses import dataclass, field

from wheelhouse import state as state_mod
from wheelhouse.device import apply_config_overrides, build_write_target, load_library


@dataclass
class VerifyReport:
    missing_file: list[tuple[str, str]] = field(default_factory=list)   # (nd_id, path)
    orphaned_db: list[tuple[str, int]] = field(default_factory=list)    # (nd_id, tid)
    drop_missing: list[tuple[str, str]] = field(default_factory=list)   # (rel_path, path)
    stale_sig: list[str] = field(default_factory=list)                  # nd_ids
    unknown_in_nd: list[str] = field(default_factory=list)              # nd_ids
    managed_count: int = 0
    drop_managed_count: int = 0

    @property
    def total_issues(self) -> int:
        return (
            len(self.missing_file) + len(self.orphaned_db) + len(self.drop_missing)
            + len(self.stale_sig) + len(self.unknown_in_nd)
        )


def run(target, ipod_cfg, nd) -> VerifyReport:
    """Compare state DB against iPod library on disk and Navidrome."""
    target = apply_config_overrides(target, ipod_cfg)

    db = state_mod.open_state()
    managed = state_mod.get_managed(db, target.serial)
    drop_managed = state_mod.get_drop_managed(db, target.serial)
    db.close()

    wt, _profile = build_write_target(target.model, target.guid)
    lib = load_library(target.root, wt)
    tids_on_ipod = {t.track_id for t in lib.snapshot.tracks}
    locations_on_ipod = {t.metadata.location for t in lib.snapshot.tracks}

    r = VerifyReport(
        managed_count=len(managed),
        drop_managed_count=len(drop_managed),
    )

    for nd_id, item in managed.items():
        if item.ipod_path and item.ipod_path not in locations_on_ipod:
            r.missing_file.append((nd_id, item.ipod_path))
        if item.ipod_track_id and item.ipod_track_id not in tids_on_ipod:
            r.orphaned_db.append((nd_id, item.ipod_track_id))

    for rel_path, row in drop_managed.items():
        if row.ipod_path and row.ipod_path not in locations_on_ipod:
            r.drop_missing.append((rel_path, row.ipod_path))

    all_songs = nd.all_songs()
    nd_by_id = {s["id"]: s for s in all_songs}
    for nd_id, item in managed.items():
        song = nd_by_id.get(nd_id)
        if song is None:
            r.unknown_in_nd.append(nd_id)
            continue
        if item.source_sig and item.source_sig != state_mod.nd_sig(song):
            r.stale_sig.append(nd_id)

    return r
