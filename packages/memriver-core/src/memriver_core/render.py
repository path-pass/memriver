from __future__ import annotations

from .store import MemoryStore

DEFAULT_BUDGET_LINES = 100


def render_index(store: MemoryStore, scopes: list[str],
                 budget_lines: int = DEFAULT_BUDGET_LINES) -> str:
    entries = sorted(store.iter_entries(scopes=scopes),
                     key=lambda e: (e.updated, e.id), reverse=True)
    if not entries:
        return "(no memories yet)"
    lines = []
    for e in entries[:budget_lines]:
        # entry files are hand-editable, so an empty body must not break the index
        first = (e.body.splitlines() or [""])[0][:60]
        lines.append(f"- [{e.type}] {e.id}: {first} ({e.updated[:10]})")
    omitted = len(entries) - budget_lines
    if omitted > 0:
        lines.append(f"… ({omitted} more entries omitted; use memory_search)")
    return "\n".join(lines)
