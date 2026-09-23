"""Whole-store administrative inspection of the filesystem store.

The ordinary read paths are access-checked and forgiving: they silently skip what
they cannot trust, which is right for serving a client and wrong for a
doctor. This inspector walks the same layout and *keeps* what reads drop, so
every skipped file is reported once, with a store-relative location and a
fixed reason. Exception messages never leave it; failing to enumerate the
store is an opaque `StorageFailure`.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from memriver_core.models import ID_RE, InspectedMemory, StoreFinding, StoreReport
from memriver_core.models.errors import ProjectNotFound, StorageFailure

from .files import (
    MANIFEST_FILENAME,
    MEMORIES_DIRNAME,
    PROJECTS_DIRNAME,
    read_regular_text,
)
from .markdown_codec import decode
from .project_store import FileProjectStore

# the directory the pre-release layout kept global entries in
_LEGACY_GLOBAL_DIRNAME = "global"

# Fixed, client-safe wording per finding kind. Exception text never reaches a
# reason: it would carry absolute paths, errno strings and library detail.
_REASONS = {
    "unreadable-file": "file could not be read",
    "unparsable": "memory file is not decodable memory markdown",
    "id-stem-mismatch": "stored id does not match the memory file name",
    "unaddressable-id": "file name is not an id the memory API can address",
    "unknown-project": "memory belongs to a project that does not exist",
    "invalid-project": "project file is not a valid project document",
    "invalid-manifest": "store manifest is invalid or names a missing project",
    "legacy-layout": "pre-release store layout; this version does not read it",
    "unsafe-container": "store data directory is a symlink or not a directory; it is not followed",
}


class FilesystemStoreInspector:
    """`StoreInspector` over the filesystem store: every file, nothing modified."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._project_store = FileProjectStore(self.root)

    # --- port ---

    def inspect(self) -> StoreReport:
        try:
            root_stat = self.root.stat()
        except FileNotFoundError:
            # the one absence that is not a failure: nothing was ever written
            return StoreReport(initialized=False, entries=(), projects=(), findings=())
        except OSError as err:
            raise StorageFailure from err
        if not stat.S_ISDIR(root_stat.st_mode):
            raise StorageFailure
        findings: list[StoreFinding] = []
        projects = self._projects(findings)
        initialized = self._manifest(findings)
        entries = self._memories(set(projects), findings)
        if os.path.lexists(self.root / _LEGACY_GLOBAL_DIRNAME):
            findings.append(self._finding("legacy-layout", _LEGACY_GLOBAL_DIRNAME))
        findings.sort(key=lambda f: (f.location_hint, f.kind))
        return StoreReport(initialized=initialized, entries=tuple(entries),
                           projects=tuple(projects), findings=tuple(findings))

    # --- internals ---

    def _finding(self, kind: str, location: str, *, project_id: str | None = None,
                 memory_id: str | None = None) -> StoreFinding:
        return StoreFinding(kind=kind, project_id=project_id, location_hint=location,
                            memory_id=memory_id, reason=_REASONS[kind])

    def _children(self, directory: Path) -> list[Path]:
        """Immediate children in name order; empty only when the directory is absent.

        A layout node that exists but is not a directory is not an empty store:
        the stores raise `StorageFailure` writing into exactly that store, so
        `NotADirectoryError` falls through to the generic failure.
        """
        try:
            return sorted(directory.iterdir())
        except FileNotFoundError:
            return []
        except OSError as err:
            raise StorageFailure from err

    def _container(self, dirname: str, findings: list[StoreFinding]) -> list[Path]:
        """The children of a data directory; a symlink or non-directory there is a
        finding and is never followed (O_NOFOLLOW guards only the last component).
        Only a successful lstat proves that; failing to look is `StorageFailure`."""
        try:
            info = os.lstat(self.root / dirname)
        except FileNotFoundError:
            return []
        except OSError as err:
            raise StorageFailure from err
        if not stat.S_ISDIR(info.st_mode):         # S_ISDIR is false for a symlink under lstat
            findings.append(self._finding("unsafe-container", dirname))
            return []
        return self._children(self.root / dirname)

    def _projects(self, findings: list[StoreFinding]) -> list[str]:
        ids: list[str] = []
        for path in self._container(PROJECTS_DIRNAME, findings):
            location = path.relative_to(self.root).as_posix()
            if not path.name.endswith(".toml"):
                try:
                    is_directory = stat.S_ISDIR(path.lstat().st_mode)
                except FileNotFoundError:
                    continue                  # e.g. an atomic write's temp file, gone since the listing
                except OSError as err:
                    raise StorageFailure from err
                if is_directory:
                    # projects/<id>/ is the pre-release layout
                    findings.append(self._finding("legacy-layout", location))
                continue                      # any other stray is ignored
            stem = path.name[: -len(".toml")]
            if not ID_RE.fullmatch(stem):
                findings.append(self._finding("invalid-project", location))
                continue
            try:
                if read_regular_text(path) is None:
                    continue                  # removed while we walked
            except OSError:
                findings.append(self._finding("unreadable-file", location, project_id=stem))
                continue
            except UnicodeDecodeError:
                pass                          # readable but not text: the parse below reports it
            try:
                self._project_store.read(stem)
            except (ProjectNotFound, StorageFailure):
                findings.append(self._finding("invalid-project", location, project_id=stem))
                continue
            ids.append(stem)
        return ids

    def _manifest(self, findings: list[StoreFinding]) -> bool:
        try:
            return self._project_store.global_project_id() is not None
        except StorageFailure:
            findings.append(self._finding("invalid-manifest", MANIFEST_FILENAME))
            return True

    def _memories(self, projects: set[str], findings: list[StoreFinding]) -> list[InspectedMemory]:
        entries: list[InspectedMemory] = []
        for path in self._container(MEMORIES_DIRNAME, findings):
            if not path.name.endswith(".md"):
                continue
            location = path.relative_to(self.root).as_posix()
            stem = path.name[: -len(".md")]
            if not ID_RE.fullmatch(stem):
                findings.append(self._finding("unaddressable-id", location, memory_id=stem))
                continue
            try:
                text = read_regular_text(path)
            except UnicodeDecodeError:
                findings.append(self._finding("unparsable", location, memory_id=stem))
                continue
            except OSError:
                findings.append(self._finding("unreadable-file", location, memory_id=stem))
                continue
            if text is None:
                continue                      # removed while we walked
            try:
                memory = decode(text)
            except Exception:  # noqa: BLE001 - any decode failure is one finding
                findings.append(self._finding("unparsable", location, memory_id=stem))
                continue
            if memory.id != stem:
                findings.append(self._finding("id-stem-mismatch", location,
                                              project_id=memory.project_id, memory_id=stem))
                continue
            entries.append(InspectedMemory(memory=memory, location_hint=location))
            if memory.project_id not in projects:
                findings.append(self._finding("unknown-project", location,
                                              project_id=memory.project_id, memory_id=stem))
        return entries
