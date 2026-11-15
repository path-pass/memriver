from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from .helpers import ID_RE, is_timestamp

# only the harnesses that hand memriver a session id of their own (spec §3.1)
Harness = Literal["claude-code", "codex"]
SessionStatus = Literal["registered", "pending"]
SessionOrigin = Literal["start", "first-seen"]
PromptOmission = Literal["secret", "too-large", "invalid", "scan-error"]

_SESSION_ID_MAX_CHARS = 128
_CALL_ID_MAX_CHARS = 256


def _is_harness_string(value: object, max_chars: int) -> bool:
    """A harness-owned id: text of 1..max_chars characters, each printable and not
    whitespace. A format character (a bidi override, a zero-width space) or a lone
    surrogate is invisible where the id is shown."""
    return (isinstance(value, str) and 1 <= len(value) <= max_chars
            and all(ch.isprintable() and not ch.isspace() for ch in value))


def is_call_id(value: object) -> bool:
    """Whether `value` can be a harness's tool-call id (Claude Code's `tool_use_id`)."""
    return _is_harness_string(value, _CALL_ID_MAX_CHARS)


@dataclass(frozen=True)
class SessionKey:
    """One harness session. The id is the harness's own string, never checked against ID_RE."""

    harness: Harness
    session_id: str

    def __post_init__(self) -> None:
        if self.harness not in get_args(Harness):
            raise ValueError("unknown harness")
        if not _is_harness_string(self.session_id, _SESSION_ID_MAX_CHARS):
            raise ValueError("invalid session id")


@dataclass(frozen=True)
class PromptEntry:
    """One prompt as recorded: its text, or why the text was left out. Never both."""

    at: str
    text: str | None = None
    omitted: PromptOmission | None = None

    def __post_init__(self) -> None:
        # messages name the rule only: a prompt's text never reaches an error
        if not is_timestamp(self.at):
            raise ValueError("prompt entry time is not a timestamp")
        if (self.text is None) == (self.omitted is None):
            raise ValueError("a prompt entry holds exactly one of text and omitted")
        if self.text is not None and not isinstance(self.text, str):
            raise ValueError("prompt entry text is not text")
        if self.omitted is not None and self.omitted not in get_args(PromptOmission):
            raise ValueError("unknown prompt omission")


@dataclass(frozen=True)
class Session:
    """One stored `sessions` row (spec §3.1); the stored row is the answer, never a caller's guess."""

    key: SessionKey
    status: SessionStatus
    origin: SessionOrigin
    project_id: str | None          # registered: the project, or None (entry unbound)
    candidate_id: str | None        # pending: the project to confirm, or None
    candidate_root: str | None      # pending: the candidate's root when it was computed
    entry_cwd: str
    branch: str | None
    transcript_path: str | None
    started_at: str                 # first recorded by memriver, not the harness's start
    last_active_at: str
    ended_at: str | None
    prompt_count: int
    last_write_prompt_count: int
    last_nudge_prompt_count: int
    first_prompt: PromptEntry | None
    recent_prompts: tuple[PromptEntry, ...]   # newest last
    # the published summary (spec §3.5): both set or both None; how it was
    # produced is the producer's business, never stored here
    summary: str | None = None
    summary_at: str | None = None


@dataclass(frozen=True)
class CallRecord:
    """A recorded tool call: its session and the working directory its payload named."""

    key: SessionKey
    cwd: str | None


@dataclass(frozen=True)
class SessionMove:
    """One applied move of a session to another directory (append-only, `seq` per session)."""

    key: SessionKey
    seq: int
    moved_at: str
    from_entry_cwd: str
    from_project_id: str | None
    to_entry_cwd: str
    to_project_id: str | None

    def __post_init__(self) -> None:
        if type(self.seq) is not int or self.seq < 1:
            raise ValueError("move sequence is not a positive integer")
        if not is_timestamp(self.moved_at):
            raise ValueError("move time is not a timestamp")
        if not (isinstance(self.from_entry_cwd, str) and isinstance(self.to_entry_cwd, str)):
            raise ValueError("a move directory is not text")  # noqa: TRY004 - a bad row
        for project in (self.from_project_id, self.to_project_id):
            if project is not None and not (isinstance(project, str)
                                            and ID_RE.fullmatch(project)):
                raise ValueError("a move project id is not addressable")


@dataclass(frozen=True)
class MoveOutcome:
    """What a move did, for the caller to answer from (a pending row compares candidates)."""

    from_project_id: str | None
    to_project_id: str | None
    from_candidate_id: str | None
    to_candidate_id: str | None
    to_entry_cwd: str
    pending: bool
    degraded: bool

    @property
    def changed(self) -> bool:
        if self.degraded:
            return True
        if self.pending:
            return self.to_candidate_id != self.from_candidate_id
        return self.to_project_id != self.from_project_id
