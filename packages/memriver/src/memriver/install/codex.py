"""Declarative installation plan for Codex CLI.

Both managed files are user-level: ``~/.codex/config.toml`` carries the MCP
server registration and the optional native-memory toggle, ``~/.codex/hooks.json``
carries the SessionStart/Stop hooks. This module only builds ``Target`` and
``EditOperation`` values -- it never opens a file, prompts, or writes.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path

from memriver.install.editors import (
    EditOperation,
    PlanningError,
    Snapshot,
    Target,
    hook_group,
    hook_identity,
    mcp_server_payload,
)

HARNESS = "codex"

# Spec 5.3's other half: unset or already-off means "do nothing **and say
# so**". Read-only completion text, never a confirmable operation -- there is
# nothing to write, and a prompt for it would ask the user to accept a no-op.
NATIVE_MEMORY_OFF_NOTE = (
    "codex: built-in memories are already off in ~/.codex/config.toml; "
    "nothing to change there."
)


def targets(home: Path, project_root: Path | None) -> tuple[Target, Target]:
    """``(~/.codex/config.toml, ~/.codex/hooks.json)``; both targets are user-level."""
    del project_root  # Codex CLI has no project-scoped target.
    config = Target(
        path=home / ".codex" / "config.toml",
        user_level=True,
        rollback_instruction=(
            "remove [mcp_servers.memriver] (and, if present, features.memories) "
            "from ~/.codex/config.toml"
        ),
    )
    hooks = Target(
        path=home / ".codex" / "hooks.json",
        user_level=True,
        rollback_instruction="remove the memriver SessionStart/Stop hooks from "
                              "~/.codex/hooks.json",
    )
    return config, hooks


def operations(
    snapshots: tuple[Snapshot, Snapshot], env: Mapping[str, str],
) -> tuple[EditOperation, ...]:
    """MCP registration, both hooks, and -- when offered -- the native-memory toggle."""
    del env  # Codex's native-memory conflict is read from its own config, not env.
    config, hooks = snapshots
    ops = [
        EditOperation(
            id="codex:mcp",
            target=config.target,
            label="register memriver MCP server",
            kind="toml-table",
            expected=mcp_server_payload(),
            key_path=("mcp_servers", "memriver"),
        ),
        EditOperation(
            id="codex:hooks-session-start",
            target=hooks.target,
            label="install the session-start hook",
            kind="hook-array",
            expected=hook_group("session-start", HARNESS),
            key_path=("hooks", "SessionStart"),
            identity=hook_identity("session-start"),
        ),
        EditOperation(
            id="codex:hooks-stop",
            target=hooks.target,
            label="install the stop hook",
            kind="hook-array",
            expected=hook_group("stop", HARNESS),
            key_path=("hooks", "Stop"),
            identity=hook_identity("stop"),
        ),
    ]
    if _memories_enabled(config.text):
        ops.append(EditOperation(
            id="codex:native-memory",
            target=config.target,
            label="disable built-in auto memory (memriver replaces it)",
            kind="toml-table",
            expected=False,
            key_path=("features", "memories"),
            optional=True,
            harness_owned=True,
        ))
    return tuple(ops)


def notes(snapshots: tuple[Snapshot, Snapshot], env: Mapping[str, str]) -> tuple[str, ...]:
    """Read-only completion text: what was checked and deliberately left alone."""
    del env
    config, _ = snapshots
    return () if _memories_enabled(config.text) else (NATIVE_MEMORY_OFF_NOTE,)


def _memories_enabled(config_text: str | None) -> bool:
    """Offer the toggle only when the existing semantic TOML value is ``true``.

    Spec 5.3: unset or already-off means nothing to do; read-only parse, the
    write itself goes through the toml-table editor's scalar support.
    """
    try:
        document = tomllib.loads(config_text or "")
    except tomllib.TOMLDecodeError as error:
        raise PlanningError(f"~/.codex/config.toml is not valid TOML: {error}") from error
    features = document.get("features", {})
    if not isinstance(features, dict):
        raise PlanningError("~/.codex/config.toml: features is not a table")
    return features.get("memories") is True
