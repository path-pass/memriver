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
    - The global scope is read-only through this port: ``create`` of a
      global-scoped memory, and ``update_body``/``delete`` whose located entry
      is global, raise ``GlobalReadOnly`` and change nothing. Global entries
      are written by hand (or by a future reviewed maintenance step), never
      by an agent-facing caller.
    - ``create(memory, ctx)`` owns the atomic name reservation. A project
      write checks the caller's visible scopes; a readable collision in any of
      them raises ``NameTaken(memory_id, existing=m)``; occupied but
      unreadable storage raises ``UnreadableMemory(memory_id)``. ``existing``
      is always the colliding memory: the search never leaves the caller's
      scopes, so there is no collision it would have to withhold.
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
