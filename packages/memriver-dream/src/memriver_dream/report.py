"""What a run did, per phase: counts, outcomes and the non-change items `memriver
dream report` prints -- ids and outcomes, never a body, summary, prompt or secret.
Change groups are not copied here: the report reads them from the change log."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

# outcomes that count as failed (retried next run) or skipped; any other is done
FAILED = frozenset({"timeout", "exit", "start", "unparsable", "schema", "login", "quota",
                    "invalid", "rejected", "conflict", "failed", "unreadable", "incomplete"})
SKIPPED = frozenset({"unchanged", "kept", "moved", "waiting", "partial", "too-large",
                     "not-configured", "group-limit", "changed-meanwhile", "policy",
                     "no-memories"})


@dataclass
class PhaseReport:
    outcomes: Counter = field(default_factory=Counter)
    items: list[dict] = field(default_factory=list)

    def record(self, outcome: str, item: dict | None = None) -> None:
        self.outcomes[outcome] += 1
        if item is not None:
            self.items.append(item)

    def as_json(self) -> dict:
        failed = sum(n for outcome, n in self.outcomes.items() if outcome in FAILED)
        skipped = sum(n for outcome, n in self.outcomes.items() if outcome in SKIPPED)
        return {"done": sum(self.outcomes.values()) - failed - skipped, "failed": failed,
                "skipped": skipped, "outcomes": dict(sorted(self.outcomes.items())),
                "items": list(self.items)}

    def summary_line(self, phase: str) -> str:
        counts = self.as_json()
        return (f"{phase}: done={counts['done']} failed={counts['failed']} "
                f"skipped={counts['skipped']}")


@dataclass
class RunReport:
    run_id: str
    status: str
    phases: dict[str, PhaseReport] = field(default_factory=dict)

    def phase(self, name: str) -> PhaseReport:
        return self.phases.setdefault(name, PhaseReport())

    def as_json(self) -> dict:
        return {name: phase.as_json() for name, phase in self.phases.items()}
