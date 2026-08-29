from __future__ import annotations

from .store import MemoryStore


def render_index(store: MemoryStore, scopes: list[str], budget_lines: int = 100) -> str:
    entries = sorted(store.iter_entries(scopes=scopes),
                     key=lambda e: e.updated, reverse=True)
    if not entries:
        return "(no memories yet)"
    lines = []
    for e in entries[:budget_lines]:
        first = e.body.splitlines()[0][:60]
        lines.append(f"- [{e.type}] {first} ({e.id}, {e.updated[:10]})")
    omitted = len(entries) - budget_lines
    if omitted > 0:
        lines.append(f"… ({omitted} more entries omitted; use memory_search)")
    return "\n".join(lines)
