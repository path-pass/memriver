from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def store_lock(root: Path) -> Iterator[None]:
    """Hold the store-wide exclusive lock for the duration of the block.

    Several processes may share one root, so a caller doing its own
    read-check-write over memories (e.g. a name-collision check before
    write) should wrap it here to serialize against peers. Not reentrant:
    `update_body` and `delete` already take this lock internally, so
    calling either of them from inside a `store_lock` block deadlocks.
    fcntl is POSIX-only: no Windows support yet.
    """
    root.mkdir(parents=True, exist_ok=True)
    with open(root / ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
