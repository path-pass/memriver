from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from memriver_core.models import AccessContext, Memory, SearchHit


class MemoryRepository(Protocol):
    """Storage port consumed by the application facade.

    Binding semantics:

    - ``create``, ``update_body``, and ``delete`` are atomic. Locking,
      transactions, compare-and-swap, and retries are implementation details
      and are not exposed.
    - ``create(memory, ctx)`` writes only where ``ctx`` can see: a
      project-scoped memory whose scope is not in ``ctx.visible_scopes()``
      raises ``InvalidScope`` and stores nothing, so the scope that routes the
      write and the scopes that are searched for a collision cannot disagree.
      Global-scoped memories are writable from every context.
    - ``create(memory, ctx)`` owns the atomic name reservation. A project
      write checks the caller's visible scopes; a global write checks the
      whole store; readable same-scope collisions raise
      ``NameTaken(memory_id, existing=m)``; cross-scope collisions raise
      ``NameTaken(memory_id, existing=None)``; occupied but unreadable storage
      raises ``UnreadableMemory(memory_id)``.
    - ``get``/``update_body``/``delete`` raise ``MemoryNotFound(memory_id)``,
      ``UnreadableMemory(memory_id)``, or ``StorageFailure()``. No method
      accepts ``ctx=None``, and the ordinary API has no implicit "all
      projects" query.
    - An implementation supplies these errors' *fields* and none of their
      words: client-visible copy is composed by the transport from the
      operation plus the fields (see ``memriver_core.application.errors``).
      A backend that phrases a message of its own changes nothing a client
      sees -- which is what keeps a backend swap invisible, and keeps SQL,
      driver, and path detail from reaching one.
    - ``iter_visible`` is explicitly scoped. A future administrative
      all-store operation requires a separately named method/use case.
    - ``search`` is a repository query, not a separate index abstraction. The
      filesystem implementation performs today's linear scan; a SQLite
      implementation may use LIKE or FTS5 internally without changing
      ``MemoryService``. A separate ``SearchIndex`` is deferred until an
      independently managed sidecar index actually exists.
    """

    def create(self, memory: Memory, ctx: AccessContext) -> None: ...
    def get(self, memory_id: str, ctx: AccessContext) -> Memory: ...
    def update_body(self, memory_id: str, body: str,
                    ctx: AccessContext,
                    description: str | None = None) -> Memory: ...
    def delete(self, memory_id: str, ctx: AccessContext) -> None: ...
    def iter_visible(self, ctx: AccessContext) -> Iterator[Memory]: ...
    def search(self, query: str, ctx: AccessContext,
               limit: int) -> list[SearchHit]: ...
