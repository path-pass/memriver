"""Declarative installation plan for Claude Code.

Both managed files are user-level: ``~/.claude.json`` carries the MCP server
registration, ``~/.claude/settings.json`` carries the SessionStart/Stop/
UserPromptSubmit/SessionEnd hooks, the PreToolUse hook for memriver's own
tools, and the optional native-memory toggle. This
module only builds ``Target`` and ``EditOperation`` values -- it never opens a
file, prompts, or writes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from memriver.install.editors import (
    EditOperation,
    PlanningError,
    RemovalOperation,
    Snapshot,
    Target,
    hook_group,
    hook_identity,
    mcp_server_payload,
)

HARNESS = "claude-code"
# PreToolUse runs for memriver's own MCP tools only: it maps each call to the
# session making it, because Claude Code keeps the MCP server across /clear
# and an in-app /resume
PRE_TOOL_USE_MATCHER = "mcp__memriver__.*"


def targets(home: Path, project_root: Path | None,
            command_name: str = "install") -> tuple[Target, Target]:
    """``(~/.claude.json, ~/.claude/settings.json)``; both targets are user-level."""
    del project_root  # Claude Code has no project-scoped target.
    del command_name  # neither target can fail to resolve, so nothing names it.
    config = Target(
        path=home / ".claude.json",
        user_level=True,
        rollback_instruction="remove mcpServers.memriver from ~/.claude.json",
    )
    settings = Target(
        path=home / ".claude" / "settings.json",
        user_level=True,
        rollback_instruction=(
            "remove the memriver SessionStart/Stop/UserPromptSubmit/SessionEnd/"
            "PreToolUse hooks (and, if present, env.CLAUDE_CODE_DISABLE_AUTO_MEMORY) from "
            "~/.claude/settings.json"
        ),
    )
    return config, settings


def operations(
    snapshots: tuple[Snapshot, Snapshot], env: Mapping[str, str],
) -> tuple[EditOperation, ...]:
    """MCP registration, all five hooks, and -- when offered -- the
    native-memory toggle."""
    config, settings = snapshots
    ops = [
        EditOperation(
            id="claude-code:mcp",
            target=config.target,
            label="register memriver MCP server",
            kind="json-object",
            expected=mcp_server_payload(HARNESS),
            key_path=("mcpServers", "memriver"),
        ),
        EditOperation(
            id="claude-code:hooks-session-start",
            target=settings.target,
            label="install the session-start hook",
            kind="hook-array",
            expected=hook_group("session-start", HARNESS),
            key_path=("hooks", "SessionStart"),
            identity=hook_identity("session-start"),
        ),
        EditOperation(
            id="claude-code:hooks-stop",
            target=settings.target,
            label="install the stop hook",
            kind="hook-array",
            expected=hook_group("stop", HARNESS),
            key_path=("hooks", "Stop"),
            identity=hook_identity("stop"),
        ),
        EditOperation(
            id="claude-code:hooks-user-prompt-submit",
            target=settings.target,
            label="install the user-prompt-submit hook",
            kind="hook-array",
            expected=hook_group("user-prompt-submit", HARNESS),
            key_path=("hooks", "UserPromptSubmit"),
            identity=hook_identity("user-prompt-submit"),
        ),
        EditOperation(
            id="claude-code:hooks-session-end",
            target=settings.target,
            label="install the session-end hook",
            kind="hook-array",
            expected=hook_group("session-end", HARNESS),
            key_path=("hooks", "SessionEnd"),
            identity=hook_identity("session-end"),
        ),
        EditOperation(
            id="claude-code:hooks-pre-tool-use",
            target=settings.target,
            label="install the pre-tool-use hook",
            kind="hook-array",
            expected=hook_group("pre-tool-use", HARNESS, matcher=PRE_TOOL_USE_MATCHER),
            key_path=("hooks", "PreToolUse"),
            identity=hook_identity("pre-tool-use"),
        ),
    ]
    if _offer_disabling_auto_memory(settings.text, env):
        ops.append(EditOperation(
            id="claude-code:native-memory",
            target=settings.target,
            label="disable built-in auto memory (memriver replaces it)",
            kind="json-object",
            expected="1",
            key_path=("env", "CLAUDE_CODE_DISABLE_AUTO_MEMORY"),
            harness_owned=True,
        ))
    return tuple(ops)


def uninstall_operations(
    snapshots: tuple[Snapshot, Snapshot], env: Mapping[str, str],
) -> tuple[RemovalOperation, ...]:
    """The exact inverse of ``operations()``: the MCP entry and all five hooks.

    The native-memory toggle ``operations()`` may add is never undone here --
    spec 5.3's setting stays exactly where install put it; see ``uninstall_notes``.
    """
    del env  # nothing here depends on the environment memriver was run with
    config, settings = snapshots
    return (
        RemovalOperation(
            id="claude-code:mcp",
            target=config.target,
            label="remove memriver MCP server",
            kind="json-object",
            key_path=("mcpServers", "memriver"),
        ),
        RemovalOperation(
            id="claude-code:hooks-session-start",
            target=settings.target,
            label="remove the session-start hook",
            kind="hook-array",
            key_path=("hooks", "SessionStart"),
            identity=hook_identity("session-start"),
        ),
        RemovalOperation(
            id="claude-code:hooks-stop",
            target=settings.target,
            label="remove the stop hook",
            kind="hook-array",
            key_path=("hooks", "Stop"),
            identity=hook_identity("stop"),
        ),
        RemovalOperation(
            id="claude-code:hooks-user-prompt-submit",
            target=settings.target,
            label="remove the user-prompt-submit hook",
            kind="hook-array",
            key_path=("hooks", "UserPromptSubmit"),
            identity=hook_identity("user-prompt-submit"),
        ),
        RemovalOperation(
            id="claude-code:hooks-session-end",
            target=settings.target,
            label="remove the session-end hook",
            kind="hook-array",
            key_path=("hooks", "SessionEnd"),
            identity=hook_identity("session-end"),
        ),
        RemovalOperation(
            id="claude-code:hooks-pre-tool-use",
            target=settings.target,
            label="remove the pre-tool-use hook",
            kind="hook-array",
            key_path=("hooks", "PreToolUse"),
            identity=hook_identity("pre-tool-use"),
        ),
    )


NATIVE_MEMORY_LEFT_NOTE = (
    "claude-code: env.CLAUDE_CODE_DISABLE_AUTO_MEMORY is still \"1\" in "
    "~/.claude/settings.json; memriver uninstall leaves harness settings alone. "
    "Remove that entry (or set it to \"0\") to let Claude Code's built-in memory "
    "run again."
)


def uninstall_notes(
    snapshots: tuple[Snapshot, Snapshot], env: Mapping[str, str],
) -> tuple[str, ...]:
    """Read-only completion text: where the native-memory toggle was left.

    This runs after the write transaction has already committed, so every
    container shape the write phase accepted has to end in a note or in
    silence -- an ``env`` holding a string rather than an object is a shape
    memriver cannot read the toggle out of, and an unreadable shape means no
    claim, not an exception on top of a configuration already removed.
    """
    del env
    _, settings = snapshots
    try:
        data = json.loads(settings.text) if settings.text else {}
    except ValueError:
        return ()  # malformed input already failed planning before this runs
    section = data.get("env") if isinstance(data, dict) else None
    if isinstance(section, dict) and section.get(
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY") == "1":
        return (NATIVE_MEMORY_LEFT_NOTE,)
    return ()


def _offer_disabling_auto_memory(
    settings_text: str | None, env: Mapping[str, str],
) -> bool:
    """Offer the toggle unless the env or the existing settings already disable it.

    Spec 5.3: detection plus a separate confirmable diff, never a silent
    default -- declining it still installs everything else.
    """
    if env.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") == "1":
        return False
    try:
        settings = json.loads(settings_text) if settings_text else {}
    except json.JSONDecodeError as error:
        raise PlanningError(
            f"~/.claude/settings.json is not valid JSON: {error}"
        ) from error
    if not isinstance(settings, dict):
        raise PlanningError("~/.claude/settings.json is not a JSON object")
    return settings.get("autoMemoryEnabled") is not False
