from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path

from memriver_core.application.errors import (
    InvalidScope,
    MemoryNotFound,
    NameTaken,
    StorageFailure,
    UnreadableMemory,
)
from memriver_core.models import (
    ID_RE,
    AccessContext,
    Memory,
    ProjectId,
    Scope,
    SearchHit,
    now,
)

from .locking import store_lock
from .markdown_codec import UnparsableStoredScope, decode, encode

logger = logging.getLogger(__name__)

_SNIPPET_CHARS = 60


def _dir_scope(entries_dir: Path) -> Scope | None:
    """Inverse of `_scope_dir`: the scope a directory of memories stands for.

    None when the directory is not one of the two known shapes.
    """
    if entries_dir.name != "entries":
        return None
    parent = entries_dir.parent
    if parent.name == "global":
        return Scope.global_()
    if parent.parent.name == "projects":
        return Scope.project(ProjectId(parent.name))
    return None


def _scope_dir(scope: Scope) -> Path:
    if scope.project_id is None:
        return Path("global")
    slug = scope.project_id
    # slugs come from untrusted tool input; reject path traversal
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        raise ValueError(f"invalid project slug: {slug!r}")
    return Path("projects") / slug


class FileMemoryRepository:
    """`MemoryRepository` over a directory tree of frontmatter markdown files."""

    def __init__(self, root: Path):
        self.root = Path(root)

    # --- port ---

    def create(self, memory: Memory, ctx: AccessContext) -> None:
        # the collision check below searches the caller's scopes, but _write
        # routes by memory.scope: let the two disagree and a foreign project's
        # entry is replaced without ever being looked at. Bind them here, so
        # the guarantee is the adapter's own and does not depend on every
        # caller having checked first. Global stays writable from anywhere --
        # it is in every context's visible scopes.
        if (memory.scope.project_id is not None
                and memory.scope not in ctx.visible_scopes()):
            # names the scope, never the memory: the refusal must not echo
            # content across the boundary it is enforcing
            raise InvalidScope(f"scope {memory.scope.to_storage()!r} is not "
                               "writable from this context")
        with store_lock(self.root):
            # check-then-write must hold the lock, or two writers race to
            # the same name and the loser silently overwrites the winner.
            #
            # a global name must never shadow, or claim, a name any project
            # already uses -- so a global write checks every scope in the
            # store, not just the caller's own two
            check_scopes = (None if memory.scope.project_id is None
                            else list(ctx.visible_scopes()))
            old = None
            if self._occupied(memory.id, check_scopes):
                # a file sits at this name -- _read may still refuse it
                # (unparseable, or frontmatter scope contradicting its
                # directory); either way the name is taken and the write
                # must not be allowed to replace it
                try:
                    old = self._read(memory.id, check_scopes)
                except Exception:  # noqa: BLE001
                    raise UnreadableMemory(
                        f"name {memory.id!r} is taken by a file that is not "
                        "a readable entry") from None
            if old is not None:
                if memory.scope.project_id is None and old.scope != memory.scope:
                    # the collision lives in another scope (a project, reached
                    # only because a global write searches the whole store);
                    # its content/type must not leak across that boundary, so
                    # the refusal stays generic
                    raise NameTaken(
                        f"name {memory.id!r} is already used elsewhere in the "
                        "store; choose another name", existing=None)
                raise NameTaken(
                    f"name {memory.id!r} already exists; memory_update it, or "
                    "choose a more precise name if this is a different fact",
                    existing=old)
            self._write(memory)

    def get(self, memory_id: str, ctx: AccessContext) -> Memory:
        return self._read(memory_id, list(ctx.visible_scopes()))

    def update_body(self, memory_id: str, body: str, ctx: AccessContext,
                    description: str | None = None) -> Memory:
        # read-modify-write must not interleave with a peer process
        with store_lock(self.root):
            memory = self._read(memory_id, list(ctx.visible_scopes()))
            memory.body = body.strip()
            if description is not None:
                # None keeps the existing description; "" explicitly clears it
                memory.description = description.strip()
            memory.updated = now()
            self._write(memory)
        return memory

    def delete(self, memory_id: str, ctx: AccessContext) -> None:
        scopes = list(ctx.visible_scopes())
        with store_lock(self.root):
            # resolve through _read first: a hand-written non-entry file, or
            # one whose frontmatter scope contradicts its directory, must
            # raise here rather than being unlinked untouched -- the same
            # directory-is-truth check _read already enforces
            self._read(memory_id, scopes)
            try:
                self._find(memory_id, scopes).unlink()
            except OSError as err:
                raise StorageFailure("could not delete memory") from err

    def iter_visible(self, ctx: AccessContext) -> Iterator[Memory]:
        dirs = [self.root / _scope_dir(s) / "entries" for s in ctx.visible_scopes()]
        for d in dirs:
            if not d.is_dir():
                continue
            expected = _dir_scope(d)
            for f in sorted(d.glob("*.md")):
                # the store is hand-editable and users may drop their own notes
                # next to memories: one broken file must never break a traversal
                try:
                    m = decode(f.read_text(encoding="utf-8"))
                except UnparsableStoredScope:
                    # a stored scope outside the grammar cannot match any
                    # directory, so it is a scope mismatch like the check below
                    logger.warning("skipping entry with mismatched scope: %s", f)
                    continue
                except Exception:  # noqa: BLE001
                    logger.warning("skipping unreadable entry file: %s", f)
                    continue
                # this walk selects by directory, so a hand-edited frontmatter
                # scope that disagrees with it would let one memory answer
                # queries for a scope it does not live in. The directory is the
                # physical truth; a file that contradicts it is not trusted.
                if m.scope != expected:
                    logger.warning("skipping entry with mismatched scope: %s", f)
                    continue
                # same as the scope check above: the filename is the identity,
                # and an id that doesn't resolve back to its own file must not
                # be exposed through the index/search -- it would name an id
                # that get()/update_body() can no longer find
                if m.id != f.stem:
                    logger.warning("skipping entry with mismatched id: %s", f)
                    continue
                yield m

    def search(self, query: str, ctx: AccessContext, limit: int) -> list[SearchHit]:
        # no clamping here: the application facade owns the limit policy
        needle = query.replace("\x00", "").lower()
        if not needle:
            return []
        memories = sorted(self.iter_visible(ctx),
                          key=lambda m: (m.updated, m.id), reverse=True)
        hits = []
        for m in memories:
            if (needle in m.body.lower() or needle in m.id.lower()
                    or needle in m.description.lower()):
                body = (m.body if len(m.body) <= _SNIPPET_CHARS
                        else m.body[:_SNIPPET_CHARS] + "…")
                hits.append(SearchHit(id=m.id, scope=m.scope, type=m.type,
                                      snippet=body))
                if len(hits) == limit:
                    break
        return hits

    # --- internals ---

    def _memory_path(self, memory: Memory) -> Path:
        return self.root / _scope_dir(memory.scope) / "entries" / f"{memory.id}.md"

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

    def _write(self, memory: Memory) -> None:
        # memory.id is untrusted at the core-API boundary too (Memory.new
        # accepts it verbatim by design -- the service sanitizes, but this
        # adapter must not assume every caller does); reject unknown shapes
        # before _memory_path interpolates it into a filesystem path
        if not ID_RE.fullmatch(memory.id):
            raise ValueError(f"invalid entry id: {memory.id!r}")
        path = self._memory_path(memory)
        try:
            self._atomic_write(path, encode(memory))
        except OSError as err:
            raise StorageFailure("could not write memory") from err

    def _occupied(self, memory_id: str, scopes: list[Scope] | None) -> bool:
        """Whether any file sits at this name -- decodable or not.

        The write-side collision check must treat every existing file as
        occupying its name, including files _read refuses (unparseable, or
        frontmatter scope contradicting the directory): claiming such a name
        would atomically replace a file the user may have hand-edited.
        """
        try:
            self._find(memory_id, scopes)
        except MemoryNotFound:
            return False
        return True

    def _find(self, memory_id: str, scopes: list[Scope] | None) -> Path:
        # memory ids are untrusted tool input; reject unknown shapes before globbing
        if not ID_RE.fullmatch(memory_id):
            raise MemoryNotFound(memory_id)
        if scopes is None:
            patterns = [f"global/entries/{memory_id}.md",
                        f"projects/*/entries/{memory_id}.md"]
        else:
            # searching only the caller's scopes makes cross-project resolution
            # impossible by construction, not by a check after the fact
            patterns = [str(_scope_dir(s) / "entries" / f"{memory_id}.md")
                        for s in scopes]
        for pattern in patterns:
            for path in self.root.glob(pattern):
                return path
        raise MemoryNotFound(memory_id)

    def _read(self, memory_id: str, scopes: list[Scope] | None) -> Memory:
        path = self._find(memory_id, scopes)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as err:
            raise StorageFailure("could not read memory") from err
        try:
            memory = decode(text)
        except UnparsableStoredScope:
            # a stored scope outside the grammar cannot equal the scope of the
            # directory the file sits in, so it fails the directory-is-truth
            # check below by construction: the same MemoryNotFound, not an
            # UnreadableMemory that would answer "unreadable entry file"
            logger.warning("refusing entry with mismatched scope: %s", path)
            raise MemoryNotFound(memory_id) from None
        except Exception as err:
            raise UnreadableMemory(memory_id) from err
        # _find resolves an id across every project directory, so the file's own
        # frontmatter would otherwise decide which scope it answers for: a
        # hand-edited file left under one project while declaring another scope
        # could be read across the physical boundary. The directory is the
        # truth here for the same reason it is in iter_visible; a memory that
        # contradicts it does not exist as far as callers go -- MemoryNotFound,
        # not UnreadableMemory, so callers that distinguish "free" from "exists
        # but unreadable" (create's collision check) treat it the same as a
        # genuinely absent name.
        if memory.scope != _dir_scope(path.parent):
            logger.warning("refusing entry with mismatched scope: %s", path)
            raise MemoryNotFound(memory_id)
        # the filename is the identity, same reason as the scope check above:
        # a hand-edited frontmatter `id` that no longer matches its filename
        # does not exist as far as callers go. Without this, update_body would
        # mutate this Memory and _write would route it by memory.id -- writing
        # under the declared id (e.g. bar.md) while this file (foo.md) stays
        # untouched, potentially clobbering an unrelated existing memory.
        if memory.id != path.stem:
            logger.warning("refusing entry with mismatched id: %s", path)
            raise MemoryNotFound(memory_id)
        return memory
