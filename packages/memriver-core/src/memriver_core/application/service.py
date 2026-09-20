"""The application facade: every memriver use case, transport-free.

Storage and content acceptance arrive as the two dependency protocols; the
limits arrive as constructor arguments. Nothing here knows about files,
frontmatter, git, or configuration.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from memriver_core.application.errors import (
    ContentRejected,
    ProjectUnavailable,
)
from memriver_core.models import IndexListing, Memory, Scope, sanitize_name, single_line

if TYPE_CHECKING:
    from memriver_core.content_policy.protocol import ContentPolicy
    from memriver_core.models import AccessContext, SearchHit
    from memriver_core.repository.protocol import MemoryRepository

# 'harness' is persisted verbatim into the stored memory, so without this it
# is a policy-free channel for secrets or megabytes of text. The shape check
# caps size and charset; the content policy then rejects the values that still
# look like credentials (a bare 'ghp_...' is all word characters). Neither
# error echoes the rejected value.
_HARNESS_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")

# What MemoryService.index returns for a store with nothing visible in it --
# the single source transports compare against, rather than each keeping its
# own copy of the literal in sync by hand.
EMPTY_INDEX = "(no memories yet)"


class MemoryService:
    def __init__(self, memory_repository: MemoryRepository, content_policy: ContentPolicy, *,
                 max_body_chars: int, metadata_max_chars: int,
                 search_limit_default: int, search_limit_max: int,
                 index_budget_lines: int) -> None:
        self._memory_repository = memory_repository
        self._content_policy = content_policy
        self._max_body_chars = max_body_chars
        # metadata keeps its own budget so that lowering the configured body
        # limit does not silently tighten harness/name/description acceptance
        self._metadata_max_chars = metadata_max_chars
        self._search_limit_default = search_limit_default
        self._search_limit_max = search_limit_max
        self._index_budget_lines = index_budget_lines

    def create(self, *, content: str, type: str, name: str, sync: bool,
               harness: str, description: str, ctx: AccessContext) -> Memory:
        if ctx.project_id is None:
            # path-free on purpose, and one message for every cause: the
            # transport is what resolved the project, so it is what turns this
            # into the state-specific line the agent reads
            raise ProjectUnavailable("no writable project in this context")
        if not _HARNESS_RE.fullmatch(harness):
            raise ContentRejected("invalid harness identifier "
                                  "(allowed: letters, digits, ., _, -, max 64 chars)")
        # the harness identifier is already capped at 64 chars by the shape
        # check above, so the configured body budget does not apply to it
        self._content_policy.check(harness, self._metadata_max_chars)
        self._content_policy.check(content, self._max_body_chars)
        # description is persisted verbatim too, and only checked when
        # non-empty since it is optional and the policy refuses ""
        if description.strip():
            self._content_policy.check(description, self._metadata_max_chars)
        # 'name' becomes the stored id verbatim once sanitize_name
        # lowercases/strips it -- that transform does not scrub secret-shaped
        # content, so the policy must run on the raw proposal first, same as
        # content/harness/description
        if name.strip():
            self._content_policy.check(name, self._metadata_max_chars)
        memory = Memory.new(body=content, type=type, scope=Scope.project(ctx.project_id),
                            sync=sync, id=sanitize_name(name), description=description,
                            source={"harness": harness, "method": "agent"})
        self._memory_repository.create(memory, ctx)
        return memory

    def read(self, memory_id: str, ctx: AccessContext) -> Memory:
        return self._memory_repository.get(memory_id, ctx)

    def search(self, query: str, ctx: AccessContext,
               limit: int | None = None) -> list[SearchHit]:
        limit = self._search_limit_default if limit is None else limit
        # the repository answers exactly what it is asked for; clamping the
        # agent-supplied limit is the application's job
        return self._memory_repository.search(
            query, ctx, max(1, min(limit, self._search_limit_max)))

    def dream(self, ctx: AccessContext, limit: int,
              max_limit: int = 10) -> list[Memory]:
        # max_limit is a fixed internal guard, not a user-configurable default --
        # no Settings field backs it, so it lives here as the signature literal.
        #
        # `updated` doubles as "last confirmed true": reviewing a memory and
        # finding it still correct is recorded by rewriting it with an unchanged
        # body, which bumps `updated` and rotates it to the back of this queue.
        # Oldest-first selection therefore cycles through the current project's
        # entries over successive reviews instead of jamming on evergreen ones.
        if ctx.project_id is None:
            return []
        limit = max(1, min(limit, max_limit))
        project_scope = Scope.project(ctx.project_id)
        entries = sorted((m for m in self._memory_repository.iter_visible(ctx)
                          if m.scope == project_scope),
                         key=lambda m: (m.updated, m.id))
        return entries[:limit]

    def index(self, ctx: AccessContext) -> str:
        listing = IndexListing(entries=tuple(
            sorted(self._memory_repository.iter_visible(ctx),
                   key=lambda m: (m.updated, m.id), reverse=True)))
        if not listing.entries:
            return EMPTY_INDEX
        lines = []
        for m in listing.entries[:self._index_budget_lines]:
            # stored memories are hand-editable, so an empty body must not
            # break the index
            raw_cue = m.description or (m.body.splitlines() or [""])[0]
            # every stored field is normalized on its own before the line is
            # composed: the id is a filename and `updated` a stored string, so
            # either can carry a newline just as the cue can, and per-field
            # normalization keeps one field's characters out of another's
            # budget. `type` is the codec's Literal, so it needs none.
            cue = single_line(raw_cue)[:60]
            memory_id, updated = single_line(m.id), single_line(m.updated[:10])
            lines.append(f"- [{m.type}] {memory_id}: {cue} ({updated})")
        omitted = len(listing.entries) - self._index_budget_lines
        if omitted > 0:
            lines.append(f"… ({omitted} more entries omitted; use memory_search)")
        return "\n".join(lines)

    def update(self, memory_id: str, content: str, ctx: AccessContext,
               description: str | None = None) -> Memory:
        self._content_policy.check(content, self._max_body_chars)
        if description is not None and description.strip():
            self._content_policy.check(description, self._metadata_max_chars)
        # scoped lookup: an id leaked from another project cannot resolve
        return self._memory_repository.update_body(
            memory_id, content, ctx, description=description)

    def delete(self, memory_id: str, ctx: AccessContext) -> None:
        self._memory_repository.delete(memory_id, ctx)
