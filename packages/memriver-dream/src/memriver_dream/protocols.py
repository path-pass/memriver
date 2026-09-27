"""What a dream run consumes from outside: an executor and a transcript source.

The umbrella implements Executor and TranscriptSource for real harnesses;
nothing here knows one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from memriver_core.models import Session

RecordKind = Literal["user", "assistant", "tool", "compact", "title"]
# "start": the process could not be started (argument list too long, executable
# missing or not executable); "too-large": the harness said the input exceeds the
# model's context
FailureKind = Literal["timeout", "exit", "start", "unparsable", "schema", "login", "quota",
                      "too-large"]


@dataclass(frozen=True)
class Record:
    """One normalized transcript entry: a prompt, an answer, a shortened tool output,
    a compaction summary or a title."""

    kind: RecordKind
    at: str | None
    text: str


@dataclass(frozen=True)
class Transcript:
    records: tuple[Record, ...]
    fingerprint: str        # over the bytes read, so any change to them changes it
    complete: bool          # False when the file ended inside a record


@dataclass(frozen=True)
class ExecutorResult:
    """The parsed JSON object, or the kind of failure -- never any of the output."""

    value: dict | None = None
    error: FailureKind | None = None


class Executor(Protocol):
    name: str               # shown in the report header
    harness: str             # recorded as changed_via of every change dream makes

    def run(self, *, system_prompt: str, prompt: str, schema: dict,
            timeout_s: int) -> ExecutorResult: ...


class TranscriptSource(Protocol):
    def read(self, session: Session) -> Transcript | None: ...
    # None: no readable transcript for this session (missing, unreadable, not a file)
