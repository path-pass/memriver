"""Store layout and the file primitives both filesystem stores share."""

from __future__ import annotations

import contextlib
import logging
import os
import stat
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

MEMORIES_DIRNAME = "memories"
PROJECTS_DIRNAME = "projects"
MANIFEST_FILENAME = "store.toml"


def memory_path(root: Path, memory_id: str) -> Path:
    return root / MEMORIES_DIRNAME / f"{memory_id}.md"


def project_path(root: Path, project_id: str) -> Path:
    return root / PROJECTS_DIRNAME / f"{project_id}.toml"


def data_dir_exists(root: Path, dirname: str) -> bool:
    """Whether the data directory `root/dirname` exists as a real directory.

    False when absent. OSError when something else sits there -- a symlink
    (live or dangling), a file, a FIFO: O_NOFOLLOW only protects the last path
    component, so a symlinked `projects/` or `memories/` would otherwise
    redirect every read and write outside the store. The store root itself
    may be a symlink: it is explicit configuration, resolved as given.
    """
    try:
        info = os.lstat(root / dirname)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode):         # S_ISDIR is false for a symlink under lstat
        raise OSError("store data directory is not a real directory")
    return True


def read_regular_text(path: Path) -> str | None:
    """The file's UTF-8 text, or None when nothing is at `path`.

    Opened without following a symlink and without blocking, then required
    to be a regular file: a link, FIFO, socket, device or directory at a
    store name is never read. Raises OSError (including for a non-regular
    file) and UnicodeDecodeError; the callers decide what those mean.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            return handle.read().decode("utf-8")
    finally:
        if fd != -1:
            os.close(fd)


def mkdir_private(root: Path, directory: Path) -> None:
    """`mkdir -p` from `root` down to `directory`, 0700 on created levels only.

    A directory that already existed is left exactly as it was: some callers
    deliberately lock one down, and a write must not silently undo that.
    """
    current = root
    for part in ("", *directory.relative_to(root).parts):
        current = current / part if part else current
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            # below the root, an existing level must be a real directory: a
            # symlinked data directory would send the write outside the store
            if part and not stat.S_ISDIR(os.lstat(current).st_mode):
                raise OSError("store data directory is not a real directory") from None
            continue
        # umask can only narrow the mode, but a mode-mangling filesystem can
        # still widen it: pin it
        current.chmod(0o700)


def _temp_file(root: Path, directory: Path, text: str) -> str:
    mkdir_private(root, directory)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")   # mode 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    except BaseException:
        with contextlib.suppress(OSError):     # the write's own error is the answer
            os.unlink(tmp)
        raise
    return tmp


def write_new(root: Path, path: Path, text: str) -> None:
    """Create `path` atomically; raise FileExistsError instead of replacing it."""
    tmp = _temp_file(root, path.parent, text)
    try:
        os.link(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):     # the link's own error is the answer
            os.unlink(tmp)
        raise
    # the link committed the file: a failed cleanup must not report it as failed
    try:
        os.unlink(tmp)
    except OSError:
        logger.warning("could not remove a temporary file after a completed write")


def replace_file(root: Path, path: Path, text: str) -> None:
    """Replace `path` atomically with `text`."""
    tmp = _temp_file(root, path.parent, text)
    try:
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):     # the replace's own error is the answer
            os.unlink(tmp)
        raise
