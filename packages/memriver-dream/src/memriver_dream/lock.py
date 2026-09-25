"""The run lock: one maintenance run at a time per store."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from memriver_core.settings import DREAM_DIRECTORY, DREAM_LOCK_FILENAME


@contextmanager
def run_lock(root: Path) -> Iterator[bool]:
    """True while this process holds `<root>/dream/.lock`, False when another run does.

    Non-blocking; the lock dies with the process that holds it, so a killed
    run never leaves it held.
    """
    directory = Path(root) / DREAM_DIRECTORY
    directory.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(directory / DREAM_LOCK_FILENAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)
