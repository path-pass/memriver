"""Whole-store administrative inspection of the markdown store.

The ordinary read path (`FileMemoryRepository`) is scoped and forgiving: it
silently skips anything it cannot trust, which is exactly right for serving a
client and exactly wrong for a doctor. This inspector walks the same layout and
*keeps* what traversal drops, so every skipped file is reported once, with a
store-relative location and a fixed reason. It never calls `iter_visible()` for
that reason, and it never lets an exception message out: reasons are authored
here, and a failure to enumerate the store is an opaque `StorageFailure`.
"""

from __future__ import annotations

import stat
from pathlib import Path

from memriver_core.application.errors import StorageFailure
from memriver_core.models import (
    ID_RE,
    InspectedMemory,
    Scope,
    StoreFinding,
    StoreReport,
)

from .markdown_codec import UnparsableStoredScope, decode
from .repository import _dir_scope

# Fixed, client-safe wording per finding kind. Exception text never reaches a
# reason: it would carry absolute paths, errno strings and library detail
# across the same boundary `StorageFailure` exists to keep closed.
_REASONS = {
    "unreadable-file": "entry file could not be read",
    "unparsable": "entry file is not decodable memory markdown",
    "scope-directory-mismatch":
        "stored scope does not match the directory the entry lives in",
    "id-stem-mismatch": "stored id does not match the entry file name",
    "unaddressable-id": "stored id is not a shape the memory API can address",
}


class FilesystemStoreInspector:
    """`StoreInspector` over the markdown store: every scope, every file."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # --- port ---

    def inspect(self) -> StoreReport:
        try:
            root_stat = self.root.stat()
        except FileNotFoundError:
            # the one absence that is not a failure: the store was never
            # initialized. Path.exists()/is_dir() would fold "inaccessible"
            # into this same answer, so neither is used anywhere here.
            return StoreReport(initialized=False, entries=(), findings=())
        except OSError as err:
            raise StorageFailure from err
        if not stat.S_ISDIR(root_stat.st_mode):
            raise StorageFailure
        entries: list[InspectedMemory] = []
        findings: list[StoreFinding] = []
        for path, location, scope in self._candidates():
            self._classify(path, location, scope, entries, findings)
        return StoreReport(initialized=True, entries=tuple(entries),
                           findings=tuple(findings))

    # --- internals ---

    def _candidates(self) -> list[tuple[Path, str, Scope]]:
        """Every `*.md` under a recognized entries directory, in stable order.

        Ordering is by store-relative location so two runs over the same store
        produce byte-identical reports.
        """
        entries_dirs = [self.root / "global" / "entries"]
        # only directories under `projects/`: an unrelated file may sit there
        # (the user's own note, a .DS_Store), and ignoring it is not the same
        # as ignoring a project directory whose `entries` node is broken
        entries_dirs += [child / "entries"
                         for child in self._children(self.root / "projects")
                         if child.is_dir()]
        found = []
        for entries_dir in entries_dirs:
            scope = _dir_scope(entries_dir)
            if scope is None:  # not one of the two known shapes
                continue
            found += [(path, path.relative_to(self.root).as_posix(), scope)
                      for path in self._children(entries_dir)
                      if path.name.endswith(".md")]
        return sorted(found, key=lambda candidate: candidate[1])

    def _children(self, directory: Path) -> list[Path]:
        """Immediate children; empty only when the directory is not there.

        A store need not have a `projects/` tree or a `global/entries/` yet,
        so absence is the one answer that is not a defect. A layout node that
        *exists* but is not a directory is not the same state: the repository
        raises `StorageFailure` writing into exactly that store, and reporting
        it as an empty one would hand the user a healthy verdict on a store
        nothing can be written to. `NotADirectoryError` therefore falls
        through to the generic failure, like any other enumeration error.
        """
        try:
            return list(directory.iterdir())
        except FileNotFoundError:
            return []
        except OSError as err:
            raise StorageFailure from err

    def _classify(self, path: Path, location: str, dir_scope: Scope,
                  entries: list[InspectedMemory],
                  findings: list[StoreFinding]) -> None:
        """Sort one candidate into `entries`, `findings`, or both."""
        def finding(kind: str) -> StoreFinding:
            # the file name is the identity in this layout (see _read), so it
            # is the id worth reporting even when the content is undecodable
            return StoreFinding(kind=kind, scope=dir_scope, location_hint=location,
                                memory_id=path.stem, reason=_REASONS[kind])

        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            findings.append(finding("unreadable-file"))
            return
        except UnicodeDecodeError:
            # readable bytes that are not text: nothing to decode, not an
            # access problem
            findings.append(finding("unparsable"))
            return
        try:
            memory = decode(text)
        except UnparsableStoredScope:
            # a stored scope outside the grammar can never equal its
            # directory's scope, so it is that mismatch, not a decode defect
            findings.append(finding("scope-directory-mismatch"))
            return
        except Exception:  # noqa: BLE001
            findings.append(finding("unparsable"))
            return
        if memory.scope != dir_scope:
            findings.append(finding("scope-directory-mismatch"))
            return
        if memory.id != path.stem:
            findings.append(finding("id-stem-mismatch"))
            return
        # listable: it decodes and its own claims agree with where it lives
        entries.append(InspectedMemory(memory=memory, location_hint=location))
        if not ID_RE.fullmatch(memory.id):
            # ...but not addressable: get()/update_body() reject this id shape
            # before they ever look for the file. Both facts are true, so the
            # entry stays listed AND the gap is reported.
            findings.append(finding("unaddressable-id"))
