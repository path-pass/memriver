"""A run's report: <root>/dream/reports/<run_id>.txt, appended as the run goes and
printed as is by `memriver dream report` (spec §6.8).

Every write opens, appends and closes the file, so a killed run leaves whatever it
wrote. Never a body, a full model input or output, or secret text: every
description and model reason goes through `safe()` before it is written.

A change is one line written in two parts (R9): "applying <kind> <ids>" before
apply, then " -> change <id>; undo: memriver undo <id>" (or " -> not applied: ...")
after it. A run killed in between leaves the first part dangling at the very end of
the file, and the next run's `mark_interrupted` completes it as "outcome unknown".
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path

from memriver_core.models import ID_RE, single_line

WITHHELD = "(withheld)"
INTERRUPTED = "run interrupted; later sections unknown"
_APPLYING = "applying "
# a created memory's id is only known once apply returns, so the line says one is coming
_CREATES = " (creates a memory)"


def _append(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    # errors="replace": a lone surrogate never turns a report line into a crash
    with open(fd, "a", encoding="utf-8", errors="replace") as file:
        file.write(text)


def _unknown(line: str) -> str:
    """The completion of an applying line whose outcome is not known (R9, §6.1)."""
    ids = [token for token in line.split()[2:] if ID_RE.fullmatch(token)]
    text = " -> outcome unknown"
    if ids:
        text += f" — see memriver history {' '.join(ids)}"
    if line.endswith(_CREATES):
        text += "; a created memory, if any, is not listed — see memriver list"
    return text


class Report:
    def __init__(self, path: Path, check_text: Callable[[str], str | None]) -> None:
        self.path = Path(path)
        self._check_text = check_text
        self._needs_you: list[str] = []
        self._pending: str | None = None     # an applying line still waiting for its outcome

    def header(self, *, run_id: str, started_at: str, trigger: str,
               executor: str | None) -> None:
        self._write(f"memriver dream run {run_id}\nstarted: {started_at}\n"
                    f"trigger: {trigger}\nexecutor: {executor or 'none'}\n")

    def section(self, title: str) -> None:
        self._write(f"\n== {single_line(title)} ==\n")

    def line(self, text: str) -> None:
        """One line of the report's own wording; model text must go through safe()."""
        self._write(" ".join(text.splitlines()) + "\n")

    def safe(self, text: str) -> str:
        """`text` on one line, or WITHHELD when the content policy hits it -- checked
        whole, before any cut, so a cut can never split a secret past the check."""
        return WITHHELD if self._check_text(text) is not None else single_line(text)

    def applying(self, kind: str, items: Sequence[str], *, creates: bool = False) -> None:
        """The first part of a change line, "applying <kind> <ids>", left open until
        applied() or not_applied(); `items` are the ids of the existing memories the
        change touches."""
        text = " ".join(["applying", kind, *items]) + (_CREATES if creates else "")
        self._write(text)
        self._pending = text

    def applied(self, change_id: str) -> None:
        self._complete(f" -> change {change_id}; undo: memriver undo {change_id}")

    def not_applied(self, reason: str) -> None:
        self._complete(f" -> not applied: {reason}")

    def needs_you(self, text: str) -> None:
        """Collected and written under "Needs you" by footer()."""
        self._needs_you.append(" ".join(text.splitlines()))

    def footer(self, *, status: str, finished_at: str) -> None:
        needs = "".join(f"{item}\n" for item in self._needs_you)
        self._write((f"\n== Needs you ==\n{needs}" if needs else "")
                    + f"\nstatus: {status}\nfinished: {finished_at}\n")

    def _complete(self, outcome: str) -> None:
        self._pending = None
        _append(self.path, outcome + "\n")

    def _write(self, text: str) -> None:
        if self._pending is not None:
            # anything written after an applying line that got no outcome closes it
            text = _unknown(self._pending) + "\n" + text
            self._pending = None
        _append(self.path, text)


def mark_interrupted(path: Path) -> None:
    """Close the report of a run a crash left `running`: complete its dangling applying
    line, if any, as "outcome unknown" and say the run was interrupted (R9, §6.1)."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        text = ""
    tail = ""
    if text and not text.endswith("\n"):
        last = text.rpartition("\n")[2]
        tail = (_unknown(last) if last.startswith(_APPLYING) else "") + "\n"
    _append(path, f"{tail}\n{INTERRUPTED}\nstatus: failed\n")
