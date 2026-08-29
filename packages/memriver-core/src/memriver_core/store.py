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

# Records an in-flight supersede so a crash between its two writes can be
# replayed. It lives at the store root, next to .lock, where no entry glob
# ("*/entries/*.md") can ever pick it up as a memory.
_JOURNAL = ".supersede.journal"


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

    def _find(self, entry_id: str) -> Path:
        # entry ids are untrusted tool input; reject non-ULID shapes before globbing
        if not re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", entry_id):
            raise KeyError(entry_id)
        for pattern in (f"global/entries/{entry_id}.md", f"projects/*/entries/{entry_id}.md"):
            for path in self.root.glob(pattern):
                return path
        raise KeyError(entry_id)

    def read(self, entry_id: str) -> Entry:
        return Entry.from_markdown(self._find(entry_id).read_text(encoding="utf-8"))

    def iter_entries(self, scopes: list[str] | None = None,
                     include_superseded: bool = False) -> Iterator[Entry]:
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
                if include_superseded or e.superseded_by is None:
                    yield e

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the store-wide exclusive lock for the duration of the block.

        Several processes may share one root, so any read-check-write over
        entries (supersede) and any full snapshot of them (index rebuild) has to
        be serialized here, or two of them interleave and fork the chain into
        contradictory active entries. Recovery of an interrupted supersede runs
        first, inside the lock, so every holder sees a consistent store.
        fcntl is POSIX-only: this project does not support Windows yet.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / ".lock", "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                self._recover()
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _recover(self) -> None:
        """Replay an interrupted supersede recorded in the journal.

        Only ever called with the lock held. If the new entry landed, the old
        one is marked to match it; if it never landed the operation never
        happened and the old entry stays active. Either way the journal goes.
        """
        journal = self.root / _JOURNAL
        try:
            parts = journal.read_text(encoding="utf-8").split()
        except FileNotFoundError:
            return
        except OSError:
            logger.warning("unreadable supersede journal, discarding: %s", journal)
            parts = []
        if len(parts) == 2:
            self._replay_supersede(*parts)
        elif parts:
            logger.warning("malformed supersede journal, discarding: %s", journal)
        journal.unlink(missing_ok=True)

    def _replay_supersede(self, old_id: str, new_id: str) -> None:
        try:
            self._find(new_id)
        except KeyError:
            # the new entry never landed: nothing to point the old one at
            return
        try:
            old = self.read(old_id)
        except Exception:
            logger.warning("supersede journal names an unreadable entry: %s", old_id)
            return
        if old.superseded_by is None:
            old.superseded_by = new_id
            old.updated = _now()
            self.write(old)

    def supersede(self, old_id: str, new_entry: Entry) -> Entry:
        with self.locked():
            old = self.read(old_id)
            if old.superseded_by is not None:
                raise ValueError(f"entry {old_id} already superseded "
                                 f"by {old.superseded_by}")
            # The two writes below are individually atomic but not atomic as a
            # pair: a crash between them would leave both entries active
            # forever. Record the intent first so the next lock holder can
            # finish or discard the operation.
            self._atomic_write(self.root / _JOURNAL, f"{old_id} {new_entry.id}")
            self.write(new_entry)
            old.superseded_by = new_entry.id
            old.updated = _now()
            self.write(old)
            (self.root / _JOURNAL).unlink(missing_ok=True)
        return new_entry
