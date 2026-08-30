"""Harness hook composition: index injection at session start, one Stop nudge.

Two rules shape this module.

*Never fail the harness.* A hook that exits non-zero, or writes a traceback to
stdout, degrades the session it was meant to help. Every path here returns
exit code 0, and a broken store costs the user one stderr line, never a
message the agent can read as instructions.

*Per-harness envelopes stay separate.* ``encode_claude_session_start`` and
``encode_codex_session_start`` currently build the same object, and are still
two functions: the schemas are owned by two vendors, and Stop already
diverges. Composition of the text itself is shared, because that is ours.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .project_context import build_context
from .protocol_text import (
    COMPACT_PREFIX,
    COMPACT_RESCUE_SUFFIX,
    EMPTY_VISIBLE,
    INDEX_BEGIN_DELIMITER,
    INDEX_END_DELIMITER,
    SESSION_START_PREFIX,
    STOP_NUDGE,
)

Harness = Literal["claude-code", "codex"]
HookEvent = Literal["session-start", "stop"]

INVALID_INPUT = "memriver hook: invalid input\n"
STORE_UNAVAILABLE = "memriver hook: memory store is unavailable\n"

# what MemoryService.index returns for a store with nothing visible in it
_EMPTY_INDEX = "(no memories yet)"


@dataclass(frozen=True)
class HookResult:
    """What the hook writes and exits with. The default is silent success."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


def encode_claude_session_start(text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                   "additionalContext": text}}


def encode_codex_session_start(text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                   "additionalContext": text}}


def encode_claude_stop(text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "Stop",
                                   "additionalContext": text}}


def encode_codex_stop(text: str) -> dict[str, Any]:
    # Codex has no additionalContext on Stop: the block reason *is* the
    # continuation prompt the model receives
    return {"decision": "block", "reason": text}


_SESSION_START_ENCODERS = {"claude-code": encode_claude_session_start,
                           "codex": encode_codex_session_start}
_STOP_ENCODERS = {"claude-code": encode_claude_stop, "codex": encode_codex_stop}


def _emit(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


def run_hook(event: HookEvent, harness: Harness, payload_text: str, *,
             root: Path | None, project_dir: Path | None,
             cwd: Path) -> HookResult:
    """Run one hook event. Returns what to write; never raises, never exits."""
    if event == "stop":
        return _stop(harness, payload_text)
    return _session_start(harness, payload_text, root=root,
                          project_dir=project_dir, cwd=cwd)


def _stop(harness: Harness, payload_text: str) -> HookResult:
    try:
        payload = json.loads(payload_text)
        # only a literal JSON false is a first Stop. A missing key, a string
        # "false", or an unparseable payload all end the session, because the
        # loop guard is the only thing standing between a nudge and a hook
        # that blocks every Stop forever.
        if not (isinstance(payload, dict)
                and payload.get("stop_hook_active") is False):
            return HookResult()
        return HookResult(stdout=_emit(_STOP_ENCODERS[harness](STOP_NUDGE)))
    except Exception:  # noqa: BLE001 - a failed nudge is never worth a message
        return HookResult()


def _session_start(harness: Harness, payload_text: str, *, root: Path | None,
                   project_dir: Path | None, cwd: Path) -> HookResult:
    try:
        payload = json.loads(payload_text)
    except (json.JSONDecodeError, TypeError):
        return HookResult(stderr=INVALID_INPUT)
    if not isinstance(payload, dict):
        return HookResult(stderr=INVALID_INPUT)
    try:
        encode = _SESSION_START_ENCODERS[harness]
        index = _read_index(root, _resolve_dir(payload, project_dir, cwd))
    except Exception:  # noqa: BLE001 - the reason belongs in `memriver doctor`
        # path-free on purpose: this line can reach a shared terminal, and a
        # store path is the one thing here worth not printing
        return HookResult(stderr=STORE_UNAVAILABLE)
    text = _compose(index, payload.get("source"))
    return HookResult(stdout=_emit(encode(text)))


def _resolve_dir(payload: dict[str, Any], project_dir: Path | None,
                 cwd: Path) -> Path:
    """Explicit option > the harness's payload cwd > the process cwd."""
    if project_dir is not None:
        return Path(project_dir)
    payload_cwd = payload.get("cwd")
    return Path(payload_cwd) if isinstance(payload_cwd, str) else Path(cwd)


def _read_index(root: Path | None, project_dir: Path) -> str:
    # imported here, not at module scope: Stop fires at the end of every turn
    # and must not pay for loading the settings/service stack it never uses
    from memriver_core.bootstrap import build_service
    from memriver_core.config import load_settings

    settings = load_settings(root_override=root)
    return build_service(settings, root=settings.root).index(
        build_context(project_dir))


def _compose(index: str, source: object) -> str:
    # a store that holds only unreadable entries is indistinguishable from an
    # empty one here, so the copy speaks about visibility, not existence
    if index == _EMPTY_INDEX:
        return EMPTY_VISIBLE
    delimited = f"{INDEX_BEGIN_DELIMITER}\n{index}\n{INDEX_END_DELIMITER}"
    if source == "compact":
        return f"{COMPACT_PREFIX}\n{delimited}\n{COMPACT_RESCUE_SUFFIX}"
    return f"{SESSION_START_PREFIX}\n{delimited}"
