"""Declarative installation plan for Claude Code.

Both managed files are user-level: ``~/.claude.json`` carries the MCP server
registration, ``~/.claude/settings.json`` carries the SessionStart/Stop hooks
and the optional native-memory toggle. This module only builds ``Target`` and
``EditOperation`` values -- it never opens a file, prompts, or writes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from memriver.install.editors import EditOperation, PlanningError, Snapshot, Target

HARNESS = "claude-code"

_MCP_PAYLOAD = {"command": "uvx", "args": ["memriver"]}


def targets(home: Path, project_root: Path | None) -> tuple[Target, Target]:
    """``(~/.claude.json, ~/.claude/settings.json)``; both targets are user-level."""
    del project_root  # Claude Code has no project-scoped target.
    config = Target(
        path=home / ".claude.json",
        user_level=True,
        rollback_instruction="remove mcpServers.memriver from ~/.claude.json",
    )
    settings = Target(
        path=home / ".claude" / "settings.json",
        user_level=True,
        rollback_instruction=(
            "remove the memriver SessionStart/Stop hooks (and, if present, "
            "env.CLAUDE_CODE_DISABLE_AUTO_MEMORY) from ~/.claude/settings.json"
        ),
    )
    return config, settings


def operations(
    snapshots: tuple[Snapshot, Snapshot], env: Mapping[str, str],
) -> tuple[EditOperation, ...]:
    """MCP registration, both hooks, and -- when offered -- the native-memory toggle."""
    config, settings = snapshots
    ops = [
        EditOperation(
            id="claude-code:mcp",
            target=config.target,
            label="register memriver MCP server",
            kind="json-object",
            expected=_MCP_PAYLOAD,
            key_path=("mcpServers", "memriver"),
        ),
        EditOperation(
            id="claude-code:hooks-session-start",
            target=settings.target,
            label="install the session-start hook",
            kind="hook-array",
            expected=_hook_expected("session-start"),
            key_path=("hooks", "SessionStart"),
            identity=("uvx", "memriver", "hook", "session-start"),
        ),
        EditOperation(
            id="claude-code:hooks-stop",
            target=settings.target,
            label="install the stop hook",
            kind="hook-array",
            expected=_hook_expected("stop"),
            key_path=("hooks", "Stop"),
            identity=("uvx", "memriver", "hook", "stop"),
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
            optional=True,
        ))
    return tuple(ops)


def _hook_expected(verb: str) -> dict:
    return {
        "hooks": [{
            "type": "command",
            "command": f"uvx memriver hook {verb} --harness {HARNESS}",
        }],
    }


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
