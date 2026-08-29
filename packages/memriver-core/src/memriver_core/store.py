from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path

from .entry import Entry, _now


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
            for f in sorted(d.glob("*.md")):
                e = Entry.from_markdown(f.read_text(encoding="utf-8"))
                if include_superseded or e.superseded_by is None:
                    yield e

    def supersede(self, old_id: str, new_entry: Entry) -> Entry:
        old = self.read(old_id)
        self.write(new_entry)
        old.superseded_by = new_entry.id
        old.updated = _now()
        self.write(old)
        return new_entry
