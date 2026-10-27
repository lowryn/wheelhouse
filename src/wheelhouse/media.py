"""ffprobe/ffmpeg conversion and cache."""
from __future__ import annotations

import subprocess
from pathlib import Path


def probe_codec(path: Path) -> str:
    """Return the audio codec name of the first audio stream."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def to_alac(src: Path, dest: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(src),
         "-map", "0:a", "-c:a", "alac", "-movflags", "+faststart", str(dest)],
        check=True,
    )


def to_aac(src: Path, dest: Path, bitrate: str = "256k") -> None:
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(src),
         "-map", "0:a", "-c:a", "aac", "-b:a", bitrate,
         "-movflags", "+faststart", str(dest)],
        check=True,
    )
