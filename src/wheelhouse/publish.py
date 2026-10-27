"""Copy media, prepare DB, safe write, verify, clean up."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, data: bytes) -> None:
    """Write data to path via fsync + rename — crash-safe."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def publish(*, root: Path, prepared, dry_run: bool) -> None:
    raise NotImplementedError


def verify(*, root: Path, library) -> None:
    raise NotImplementedError
