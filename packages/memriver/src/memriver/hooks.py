"""Harness hook composition: index injection at session start, one Stop nudge.

Two rules shape this module.

*Never fail the harness.* A hook that exits non-zero, or writes a traceback to
stdout, degrades the session it was meant to help. Every path here returns
exit code 0, and a broken store costs the user one stderr line, never a
message the agent can read as instructions.

*Per-harness envelopes stay separate.* Every event keeps one encoder per
harness even where both currently build the same object: the schemas are owned
by two vendors and have diverged before. Composition of the text itself is
shared, because that is ours.

*Stop stays light.* The Stop path only needs to know whether the current
directory belongs to a registered project, so it resolves that through
``project_context`` and ``memriver_core.settings`` alone: it never builds the
service stack, never takes the store lock, and never creates the store.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .core_logging import quiet_core_logging
from .protocol_text import (
    COMPACT_PREFIX,
    COMPACT_RESCUE_SUFFIX,
    INDEX_BEGIN_DELIMITER,
    INDEX_END_DELIMITER,
    SESSION_START_PREFIX,
    STOP_NUDGE,
)

Harness = Literal["claude-code", "codex"]
HookEvent = Literal["session-start", "stop"]

INVALID_INPUT = "memriver hook: invalid input\n"
STORE_UNAVAILABLE = "memriver hook: memory store is unavailable\n"

def _utf16_units(text: str) -> int:
    """JavaScript's ``string.length``: UTF-16 code units, non-BMP counting 2."""
    return len(text.encode("utf-16-le")) // 2


def _utf8_bytes(text: str) -> int:
    return len(text.encode("utf-8"))


@dataclass(frozen=True)
class _InlineBudget:
    """One harness's inline cap, in the unit that harness counts it in."""

    measure: Callable[[str], int]
    limit: int


# How much injected text each harness actually forwards inline, and how each
# one measures it. Past its own limit a harness stops handing the model the
# payload: Claude Code spills a hook output whose JavaScript `string.length`
# exceeds 10,000 to a file and injects a head/tail preview plus the path
# (claude 2.1.269), and Codex truncates at a default `additionalContextLimit`
# of 2,500 tokens estimated as `ceil(utf-8 bytes / 4)` (codex v0.154.0) --
# which is exactly 10,000 bytes, the form used here because a byte count adds
# up across lines and a rounded token count does not. Both are fixed vendor
# protocol limits, like the 60-character cue budget: not memriver policy, so
# no setting backs them. Python's own `len` is a third unit again, and
# counting in it lets a legal CJK or emoji index overflow both harnesses.
#
# memriver deliberately does not write `additionalContextLimit` into the Codex
# hook group to raise its own ceiling: the shared `hook_group` payload stays
# identical for both harnesses, and spending more of the user's context window
# than the vendor's default is their call to make in their own config.
INLINE_CONTEXT_BUDGET: dict[str, _InlineBudget] = {
    "claude-code": _InlineBudget(_utf16_units, 10_000),
    "codex": _InlineBudget(_utf8_bytes, 2_500 * 4),
}

# core's own wording when its line budget drops entries, reused verbatim so a
# truncation here reads as one continued count rather than a second notice
_OMITTED_LINE_PREFIX = "… ("
_OMITTED_LINE_SUFFIX = " more entries omitted; use memory_search)"


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
    # the documented decision-control form for Stop: `block` keeps the session
    # going and `reason` is what the agent reads (the exit-code-2 semantics in
    # JSON). `hookSpecificOutput.additionalContext` is documented for
    # SessionStart but not for Stop, and a Stop hook that emits it is ignored
    # -- the nudge never reaches the agent. Verified 2026-08-31 against
    # code.claude.com/docs/en/hooks.
    return {"decision": "block", "reason": text}


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
        return _stop(harness, payload_text, root=root, project_dir=project_dir,
                    cwd=cwd)
    return _session_start(harness, payload_text, root=root,
                          project_dir=project_dir, cwd=cwd)


def _stop(harness: Harness, payload_text: str, *, root: Path | None,
         project_dir: Path | None, cwd: Path) -> HookResult:
    try:
        payload = json.loads(payload_text)
        # only a literal JSON false is a first Stop. A missing key, a string
        # "false", or an unparseable payload all end the session, because the
        # loop guard is the only thing standing between a nudge and a hook
        # that blocks every Stop forever.
        if not (isinstance(payload, dict)
                and payload.get("stop_hook_active") is False):
            return HookResult()
        # settings only (pydantic-settings), never bootstrap: the Stop path must
        # stay light and must not create the store or take the lock
        from memriver_core.settings import storage_root

        from .project_context import resolve

        store_root = Path(root) if root is not None else storage_root()
        if resolve(store_root, _resolve_dir(payload, project_dir, cwd)).state != "registered":
            return HookResult()
        return HookResult(stdout=_emit(_STOP_ENCODERS[harness](STOP_NUDGE)))
    except Exception:  # noqa: BLE001 - a failed nudge is never worth a message
        return HookResult()


def _session_start(harness: Harness, payload_text: str, *, root: Path | None,
                   project_dir: Path | None, cwd: Path) -> HookResult:
    try:
        payload = json.loads(payload_text)
    except Exception:  # noqa: BLE001 - see below
        # every decoder failure is the same answer, so the boundary is the
        # decoder rather than a list of its exception classes: deeply nested
        # input raises RecursionError, and a future stdlib could raise
        # something else again. `Exception`, never `BaseException`, so a
        # KeyboardInterrupt still ends the process it interrupted.
        return HookResult(stderr=INVALID_INPUT)
    if not isinstance(payload, dict):
        return HookResult(stderr=INVALID_INPUT)
    try:
        encode = _SESSION_START_ENCODERS[harness]
        index = _read_index(root, _resolve_dir(payload, project_dir, cwd))
        text = _compose(index, payload.get("source"), harness)
        return HookResult(stdout=_emit(encode(text)))
    except Exception:  # noqa: BLE001 - the reason belongs in `memriver doctor`
        # one boundary around everything after the payload shape check --
        # encoder lookup, store read, composition and JSON emission alike --
        # because any of them escaping fails the session this hook exists to
        # help. path-free on purpose: this line can reach a shared terminal,
        # and a store path is the one thing here worth not printing.
        return HookResult(stderr=STORE_UNAVAILABLE)


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
    from memriver_core.settings import load_settings

    from .project_context import resolve
    from .session import open_session

    with quiet_core_logging():
        settings = load_settings(root_override=root)
        service = build_service(settings, root=settings.root)
        # the same seam the MCP server uses: same directory, same header and body
        session = open_session(service, resolve(settings.root, project_dir))
        body = service.index(session.read_write_set)
    return session.header + "\n" + body


def _neutralize_delimiters(index: str) -> str:
    """Break the phrase both index delimiters are built from, inside the data.

    Both delimiters fit the 60-character cue budget, so a description can spell
    one verbatim and the single-line normalization upstream never notices: no
    newline is needed to forge a terminator mid-line. The phrase is what gets
    hyphenated rather than the dashes, because dashes recombine -- neighbouring
    text can supply them back around a stripped marker, while the phrase holds
    the only space and the replacement has none.
    """
    return index.replace("memriver index", "memriver-index")


def _omitted_line(count: int) -> str:
    return f"{_OMITTED_LINE_PREFIX}{count}{_OMITTED_LINE_SUFFIX}"


def _already_omitted(line: str) -> int | None:
    """The count core itself dropped, when its own notice is the last line."""
    if not (line.startswith(_OMITTED_LINE_PREFIX)
            and line.endswith(_OMITTED_LINE_SUFFIX)):
        return None
    digits = line[len(_OMITTED_LINE_PREFIX):-len(_OMITTED_LINE_SUFFIX)]
    return int(digits) if digits.isdigit() else None


def _fit(index: str, wrap: Callable[[str], str], budget: _InlineBudget) -> str:
    """Drop whole index lines until the wrapped payload fits ``budget``.

    Truncating characters would cut a line in half and leave the fragment
    reading like a complete entry; dropping lines keeps every entry the model
    sees true, and the tail says how many it is not seeing.

    Both metrics count encoded units, so they add up across a concatenation:
    the wrapper and each line are measured once, and the largest prefix that
    fits is arithmetic from there. ``index_budget_lines`` has no upper bound
    and this runs before the first prompt of a session, so re-wrapping and
    re-measuring a whole candidate payload per dropped line is work the loop
    does not need to repeat.
    """
    measure, limit = budget.measure, budget.limit
    text = wrap(index)
    if measure(text) <= limit:
        return text
    lines = index.split("\n")
    dropped_by_core = _already_omitted(lines[-1])
    if dropped_by_core is not None:
        lines.pop()  # one continued count, not a second notice below the first
    already = dropped_by_core or 0
    wrapper, separator = measure(wrap("")), measure("\n")
    kept, body = 0, 0  # `body`: the lines kept so far, with their separators
    for candidate in range(1, len(lines)):
        body += measure(lines[candidate - 1]) + separator
        if (wrapper + body
                + measure(_omitted_line(already + len(lines) - candidate))) <= limit:
            kept = candidate
    # kept == 0 means even the notices alone exceed the budget: the harness
    # truncates from here, which is still better than handing it entries it
    # will cut mid-line
    return wrap("\n".join(
        [*lines[:kept], _omitted_line(already + len(lines) - kept)]))


def _compose(index: str, source: object, harness: Harness) -> str:
    # `index` is the project header, a newline, then the body `_read_index`
    # built. The header is never dropped by `_fit`: it goes into the wrapper
    # rather than the part that gets fitted to the harness's inline budget.
    header, _, body = index.partition("\n")
    prefix, suffix = ((COMPACT_PREFIX, "\n" + COMPACT_RESCUE_SUFFIX)
                      if source == "compact" else (SESSION_START_PREFIX, ""))
    header = _neutralize_delimiters(header)

    def wrap(body_text: str) -> str:
        return (f"{prefix}\n{INDEX_BEGIN_DELIMITER}\n{header}\n{body_text}\n"
                f"{INDEX_END_DELIMITER}{suffix}")

    return _fit(_neutralize_delimiters(body), wrap,
                INLINE_CONTEXT_BUDGET[harness])
