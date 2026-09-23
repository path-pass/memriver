from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AccessContext:
    """Which projects one caller may read and write.

    Built only by the application facade from a resolved session, never from
    tool arguments: knowing an id grants nothing.
    """

    project_id: str | None
    global_project_id: str | None

    def readable(self) -> frozenset[str]:
        return frozenset(p for p in (self.project_id, self.global_project_id) if p is not None)

    def writable(self) -> frozenset[str]:
        # global is read-only to every agent-facing caller, whatever the
        # context claims
        if self.project_id is None or self.project_id == self.global_project_id:
            return frozenset()
        return frozenset({self.project_id})
