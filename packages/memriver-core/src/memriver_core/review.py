"""Selection of entries most in need of a maintenance review.

`updated` doubles as "last confirmed true": reviewing an entry and finding
it still correct is recorded by rewriting it with an unchanged body, which
bumps `updated` and rotates it to the back of this queue. Oldest-first
selection therefore cycles through the whole store over successive reviews
instead of jamming on evergreen entries.
"""

from __future__ import annotations

from .store import MemoryStore

MAX_REVIEW_BATCH = 10


def review_queue(store: MemoryStore, scopes: list[str],
                 limit: int, max_limit: int = MAX_REVIEW_BATCH) -> list:
    limit = max(1, min(limit, max_limit))
    entries = sorted(store.iter_entries(scopes=scopes),
                     key=lambda e: (e.updated, e.id))
    return entries[:limit]
