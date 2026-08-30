"""Selection over the live entries: substring search and the review queue.

At local scale (hundreds of entries) an LLM scanning the rendered index
outperforms any keyword engine, so the local layer ships no search
infrastructure (docs/memory-model.md). The scan exists to keep the
memory_search tool contract stable; a future mode swaps the engine
behind it without agents noticing.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import DEFAULT_SEARCH_LIMIT_MAX
from .store import MemoryStore

_SNIPPET_CHARS = 60


@dataclass
class SearchHit:
    id: str
    scope: str
    type: str
    snippet: str


def search_entries(store: MemoryStore, query: str, scopes: list[str],
                   limit: int, max_limit: int = DEFAULT_SEARCH_LIMIT_MAX) -> list[SearchHit]:
    limit = max(1, min(limit, max_limit))
    needle = query.replace("\x00", "").lower()
    if not needle:
        return []
    entries = sorted(store.iter_entries(scopes=scopes),
                     key=lambda e: (e.updated, e.id), reverse=True)
    hits = []
    for e in entries:
        if (needle in e.body.lower() or needle in e.id.lower()
                or needle in e.description.lower()):
            body = e.body if len(e.body) <= _SNIPPET_CHARS else e.body[:_SNIPPET_CHARS] + "…"
            hits.append(SearchHit(id=e.id, scope=e.scope, type=e.type, snippet=body))
            if len(hits) == limit:
                break
    return hits


def review_queue(store: MemoryStore, scopes: list[str],
                 limit: int, max_limit: int = 10) -> list:
    # max_limit is a fixed internal guard, not a user-configurable default --
    # no Settings field backs it, so it lives here as the signature literal.
    #
    # `updated` doubles as "last confirmed true": reviewing an entry and
    # finding it still correct is recorded by rewriting it with an unchanged
    # body, which bumps `updated` and rotates it to the back of this queue.
    # Oldest-first selection therefore cycles through the whole store over
    # successive reviews instead of jamming on evergreen entries.
    limit = max(1, min(limit, max_limit))
    entries = sorted(store.iter_entries(scopes=scopes),
                     key=lambda e: (e.updated, e.id))
    return entries[:limit]
