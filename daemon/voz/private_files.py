"""Owner-only files for recordings and transcripts: folders 0700, files 0600."""
from __future__ import annotations

import os
from pathlib import Path
from typing import IO

FILE_MODE = 0o600
DIR_MODE = 0o700


def private_dir(path: Path) -> Path:
    """Create the folder (and parents) owner-only, and tighten it when it already exists."""
    path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    os.chmod(path, DIR_MODE)
    return path


def open_private(path: Path, mode: str = "w") -> IO:
    """Open for writing ("w", "a", "wb", "ab") so that only the owner can read the file, new or old."""
    if mode not in {"w", "a", "wb", "ab"}:
        raise ValueError(f"open_private writes only; got mode {mode!r}")
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if mode.startswith("a") else os.O_TRUNC)
    fd = os.open(path, flags, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
        return os.fdopen(fd, mode, **({} if "b" in mode else {"encoding": "utf-8"}))
    except BaseException:
        os.close(fd)
        raise
