"""The classifier behind core's ContentClassifier port: the [classifier] source
switches, then one backend check."""

from __future__ import annotations

from collections.abc import Callable

from memriver_core import Verdict


class Classifier:
    """core's ContentClassifier. "human" is never checked; "mcp" follows agent_writes
    and "dream" dream_writes; any other source is checked (fail closed)."""

    def __init__(self, check: Callable[[str], Verdict | None], *, agent_writes: bool,
                 dream_writes: bool) -> None:
        self._check = check
        self._skipped = ({"human"} | (set() if agent_writes else {"mcp"})
                         | (set() if dream_writes else {"dream"}))

    def classify(self, text: str, *, changed_by: str) -> Verdict | None:
        if changed_by in self._skipped:
            return None
        return self._check(text)
