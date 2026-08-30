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
    InvalidScope,
    ProjectUnavailable,
)
from memriver_core.models import IndexListing, Memory, Scope, sanitize_name

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

_SNIPPET_CHARS = 60


class MemoryService:
    def __init__(self, repository: MemoryRepository, policy: ContentPolicy, *,
                 max_body_chars: int, metadata_max_chars: int,
                 search_limit_default: int, search_limit_max: int,
                 index_budget_lines: int) -> None:
        self._repository = repository
        self._policy = policy
        self._max_body_chars = max_body_chars
        # metadata keeps its own budget so that lowering the configured body
        # limit does not silently tighten harness/name/description acceptance
        self._metadata_max_chars = metadata_max_chars
        self._search_limit_default = search_limit_default
        self._search_limit_max = search_limit_max
        self._index_budget_lines = index_budget_lines

    def create(self, *, content: str, type: str, name: str, scope: str, sync: bool,
               harness: str, description: str, ctx: AccessContext) -> Memory:
        if not _HARNESS_RE.fullmatch(harness):
            raise ContentRejected("invalid harness identifier "
                                  "(allowed: letters, digits, ., _, -, max 64 chars)")
        # the harness identifier is already capped at 64 chars by the shape
        # check above, so the configured body budget does not apply to it
        self._policy.check(harness, self._metadata_max_chars)
        self._policy.check(content, self._max_body_chars)
        # description is persisted verbatim too, and only checked when
        # non-empty since it is optional and the policy refuses ""
        if description.strip():
            self._policy.check(description, self._metadata_max_chars)
        # 'name' becomes the stored id verbatim once sanitize_name
        # lowercases/strips it -- that transform does not scrub secret-shaped
        # content, so the policy must run on the raw proposal first, same as
        # content/harness/description
        if name.strip():
            self._policy.check(name, self._metadata_max_chars)
        resolved = self._resolve_scope(scope, ctx)
        memory = Memory.new(body=content, type=type, scope=resolved, sync=sync,
                            id=sanitize_name(name), description=description,
                            source={"harness": harness, "method": "agent"})
        self._repository.create(memory, ctx)
        return memory

    def read(self, memory_id: str, ctx: AccessContext) -> Memory:
        return self._repository.get(memory_id, ctx)

    def search(self, query: str, ctx: AccessContext,
               limit: int | None = None) -> list[SearchHit]:
        limit = self._search_limit_default if limit is None else limit
        # the repository answers exactly what it is asked for; clamping the
        # agent-supplied limit is the application's job
        return self._repository.search(query, ctx,
                                       max(1, min(limit, self._search_limit_max)))

    def review_queue(self, ctx: AccessContext, limit: int,
                     max_limit: int = 10) -> list[Memory]:
        # max_limit is a fixed internal guard, not a user-configurable default --
        # no Settings field backs it, so it lives here as the signature literal.
        #
        # `updated` doubles as "last confirmed true": reviewing a memory and
        # finding it still correct is recorded by rewriting it with an unchanged
        # body, which bumps `updated` and rotates it to the back of this queue.
        # Oldest-first selection therefore cycles through the whole store over
        # successive reviews instead of jamming on evergreen memories.
        limit = max(1, min(limit, max_limit))
        entries = sorted(self._repository.iter_visible(ctx),
                         key=lambda m: (m.updated, m.id))
        return entries[:limit]

    def index(self, ctx: AccessContext) -> str:
        listing = IndexListing(entries=tuple(
            sorted(self._repository.iter_visible(ctx),
                   key=lambda m: (m.updated, m.id), reverse=True)))
        if not listing.entries:
            return "(no memories yet)"
        lines = []
        for m in listing.entries[:self._index_budget_lines]:
            # stored memories are hand-editable, so an empty body must not
            # break the index
            cue = m.description or (m.body.splitlines() or [""])[0]
            lines.append(f"- [{m.type}] {m.id}: {cue[:_SNIPPET_CHARS]} ({m.updated[:10]})")
        omitted = len(listing.entries) - self._index_budget_lines
        if omitted > 0:
            lines.append(f"… ({omitted} more entries omitted; use memory_search)")
        return "\n".join(lines)

    def update(self, memory_id: str, content: str, ctx: AccessContext,
               description: str | None = None) -> Memory:
        self._policy.check(content, self._max_body_chars)
        if description is not None and description.strip():
            self._policy.check(description, self._metadata_max_chars)
        # scoped lookup: an id leaked from another project cannot resolve
        return self._repository.update_body(memory_id, content, ctx,
                                            description=description)

    def delete(self, memory_id: str, ctx: AccessContext) -> None:
        self._repository.delete(memory_id, ctx)

    def _resolve_scope(self, raw: str, ctx: AccessContext) -> Scope:
        if raw == "project":
            if ctx.project_id is None:
                # path-free on purpose: the transport owns project_dir and
                # restores the path-bearing message
                raise ProjectUnavailable("not inside a git project")
            resolved = Scope.project(ctx.project_id)
        else:
            try:
                resolved = Scope.parse(raw)
            except ValueError as err:
                # a malformed 'project:...' is still an attempt at a project
                # scope, so it keeps the boundary refusal rather than turning
                # into the generic grammar error
                if raw.startswith("project:"):
                    raise InvalidScope(self._outside(raw)) from err
                raise InvalidScope(str(err)) from err
        # an explicit 'project:<slug>' would otherwise let a caller seed
        # another project's memories; the caller's own project stays valid
        if resolved not in ctx.visible_scopes():
            raise InvalidScope(self._outside(raw))
        return resolved

    @staticmethod
    def _outside(raw: str) -> str:
        return f"scope {raw!r} is outside the current project; use 'project' or 'global'"
