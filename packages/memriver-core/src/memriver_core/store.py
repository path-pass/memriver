from __future__ import annotations

import fcntl
import logging
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .entry import Entry, _now

logger = logging.getLogger(__name__)

# ids are either server-generated ULIDs (fallback) or sanitized kebab slugs;
# both shapes are safe as file stems, everything else is refused before globbing
_ID_RE = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}|[a-z0-9][a-z0-9-]{0,63}")


def _dir_scope(entries_dir: Path) -> str | None:
    """Inverse of `_scope_dir`: the scope a directory of entries stands for.

    None when the directory is not one of the two known shapes.
    """
    if entries_dir.name != "entries":
        return None
    parent = entries_dir.parent
    if parent.name == "global":
        return "global"
    if parent.parent.name == "projects":
        return f"project:{parent.name}"
    return None


def _scope_dir(scope: str) -> Path:
    if scope == "global":
        return Path("global")
    if scope.startswith("project:"):
        slug = scope.split(":", 1)[1]
        # slugs come from untrusted tool input; reject path traversal
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
            raise ValueError(f"invalid project slug: {slug!r}")
        return Path("projects") / slug
    raise ValueError(f"invalid scope: {scope!r}")


class MemoryStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _entry_path(self, entry: Entry) -> Path:
        return self.root / _scope_dir(entry.scope) / "entries" / f"{entry.id}.md"

    def _atomic_write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def write(self, entry: Entry) -> Path:
        path = self._entry_path(entry)
        self._atomic_write(path, entry.to_markdown())
        return path

    def _find(self, entry_id: str, scopes: list[str] | None = None) -> Path:
        # entry ids are untrusted tool input; reject unknown shapes before globbing
        if not _ID_RE.fullmatch(entry_id):
            raise KeyError(entry_id)
        if scopes is None:
            patterns = [f"global/entries/{entry_id}.md",
                        f"projects/*/entries/{entry_id}.md"]
        else:
            # searching only the caller's scopes makes cross-project resolution
            # impossible by construction, not by a check after the fact
            patterns = [str(_scope_dir(s) / "entries" / f"{entry_id}.md")
                        for s in scopes]
        for pattern in patterns:
            for path in self.root.glob(pattern):
                return path
        raise KeyError(entry_id)

    def read(self, entry_id: str, scopes: list[str] | None = None) -> Entry:
        path = self._find(entry_id, scopes)
        entry = Entry.from_markdown(path.read_text(encoding="utf-8"))
        # _find resolves an id across every project directory, so the file's own
        # frontmatter would otherwise decide which scope it answers for: a
        # hand-edited file left under one project while declaring another scope
        # could be read across the physical boundary. The directory is the
        # truth here for the same reason it is in iter_entries; an entry that
        # contradicts it does not exist as far as callers go.
        if entry.scope != _dir_scope(path.parent):
            logger.warning("refusing entry with mismatched scope: %s", path)
            raise KeyError(entry_id)
        return entry

    def exists(self, entry_id: str, scopes: list[str]) -> bool:
        try:
            self._find(entry_id, scopes)
        except KeyError:
            return False
        return True

    def update_body(self, entry_id: str, body: str, scopes: list[str]) -> Entry:
        # read-modify-write must not interleave with a peer process
        with self.locked():
            entry = self.read(entry_id, scopes)
            entry.body = body.strip()
            entry.updated = _now()
            self.write(entry)
        return entry

    def delete(self, entry_id: str, scopes: list[str]) -> None:
        with self.locked():
            path = self._find(entry_id, scopes)
            path.unlink()

    def iter_entries(self, scopes: list[str] | None = None) -> Iterator[Entry]:
        if scopes is None:
            dirs = [self.root / "global" / "entries",
                    *self.root.glob("projects/*/entries")]
        else:
            dirs = [self.root / _scope_dir(s) / "entries" for s in scopes]
        for d in dirs:
            if not d.is_dir():
                continue
            expected = _dir_scope(d)
            for f in sorted(d.glob("*.md")):
                # the store is hand-editable and users may drop their own notes
                # next to entries: one broken file must never break a traversal
                try:
                    e = Entry.from_markdown(f.read_text(encoding="utf-8"))
                except Exception:
                    logger.warning("skipping unreadable entry file: %s", f)
                    continue
                # this walk selects by directory, so a hand-edited frontmatter
                # scope that disagrees with it would let one entry answer
                # queries for a scope it does not live in. The directory is the
                # physical truth; a file that contradicts it is not trusted.
                if e.scope != expected:
                    logger.warning("skipping entry with mismatched scope: %s", f)
                    continue
                yield e

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the store-wide exclusive lock for the duration of the block.

        Several processes may share one root, so a caller doing its own
        read-check-write over entries (e.g. a name-collision check before
        write) should wrap it here to serialize against peers. Not reentrant:
        `update_body` and `delete` already take this lock internally, so
        calling either of them from inside a `locked()` block deadlocks.
        fcntl is POSIX-only: no Windows support yet.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / ".lock", "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
