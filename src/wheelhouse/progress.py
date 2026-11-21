"""Shared progress bar for per-track sync phases."""
from __future__ import annotations

from rich.progress import BarColumn, Progress, ProgressColumn, Task, TextColumn
from rich.table import Column
from rich.text import Text

# Window for the rate/ETA estimate. Cache hits and Navidrome downloads run at
# very different speeds, so average over a couple of minutes for a steady
# reading rather than rich's 30 s default.
SPEED_WINDOW_SECONDS = 120


def _hms(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}"


class ItemCountColumn(ProgressColumn):
    """'Track 1,204 of 24,479' — the item currently being worked on."""

    def __init__(self, unit: str) -> None:
        self.unit = unit
        super().__init__()

    def render(self, task: Task) -> Text:
        total = int(task.total or 0)
        current = min(int(task.completed) + 1, total)
        return Text(f"{self.unit.title()} {current:,} of {total:,}", style="progress.download")


class RateColumn(ProgressColumn):
    """'38.5 tracks/min'."""

    def __init__(self, unit: str) -> None:
        self.unit = unit
        super().__init__()

    def render(self, task: Task) -> Text:
        speed = task.finished_speed or task.speed
        rate = f"{speed * 60:,.1f}" if speed else "–"
        return Text(f"{rate} {self.unit}s/min", style="progress.data.speed")


class EtaColumn(ProgressColumn):
    """'ETA 2:41:10'."""

    max_refresh = 0.5

    def render(self, task: Task) -> Text:
        remaining = task.time_remaining
        eta = _hms(remaining) if remaining is not None else "-:--:--"
        return Text(f"ETA {eta}", style="progress.remaining")


def item_progress(unit: str = "track") -> Progress:
    """Progress bar: title · bar · 'Track x of N' · rate per minute · ETA."""
    return Progress(
        TextColumn(
            "[progress.description]{task.description}",
            table_column=Column(ratio=1, min_width=20, no_wrap=True, overflow="ellipsis"),
        ),
        BarColumn(bar_width=30),
        ItemCountColumn(unit),
        RateColumn(unit),
        EtaColumn(),
        transient=True,
        expand=True,
        speed_estimate_period=SPEED_WINDOW_SECONDS,
    )
