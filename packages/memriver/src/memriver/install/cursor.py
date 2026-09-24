"""Declarative installation plan for Cursor.

``~/.cursor/mcp.json`` is user-level and carries the MCP server registration;
``AGENTS.md`` at the nearest git root is project-level and carries the
marker-managed protocol block. This module only builds ``Target`` and
``EditOperation`` values -- it never opens a file, prompts, or writes.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from memriver.install.editors import (
    EditOperation,
    PlanningError,
    RemovalOperation,
    Snapshot,
    Target,
    mcp_server_payload,
)
from memriver.protocol_text import PROTOCOL_BLOCK


def targets(home: Path, project_root: Path | None,
            command_name: str = "install") -> tuple[Target, Target]:
    """``(~/.cursor/mcp.json, <git-root>/AGENTS.md)``.

    Raises ``PlanningError`` before returning anything when ``project_root``
    is ``None`` -- Cursor's instructions file has no user-level home.
    ``command_name`` is the command the user ran, so the refusal tells them
    where to re-run that one rather than naming the wrong half of the pair.
    """
    if project_root is None:
        raise PlanningError(
            "cursor needs a project (the nearest current-or-ancestor .git root) "
            f"to manage its AGENTS.md; run {command_name} inside a project or "
            "pass one"
        )
    mcp = Target(
        path=home / ".cursor" / "mcp.json",
        user_level=True,
        rollback_instruction="remove mcpServers.memriver from ~/.cursor/mcp.json",
    )
    instructions = Target(
        path=project_root / "AGENTS.md",
        user_level=False,
        rollback_instruction="remove the memriver marker block from AGENTS.md",
    )
    return mcp, instructions


def operations(
    snapshots: tuple[Snapshot, Snapshot], env: Mapping[str, str],
) -> tuple[EditOperation, ...]:
    """MCP registration plus the marker-managed protocol block."""
    del env  # Cursor has no native-memory conflict to resolve.
    mcp, instructions = snapshots
    return (
        EditOperation(
            id="cursor:mcp",
            target=mcp.target,
            label="register memriver MCP server",
            kind="json-object",
            expected=mcp_server_payload("cursor"),
            key_path=("mcpServers", "memriver"),
        ),
        EditOperation(
            id="cursor:instructions",
            target=instructions.target,
            label="add the memriver protocol block to AGENTS.md",
            kind="marker-block",
            expected=PROTOCOL_BLOCK,
        ),
    )


def uninstall_operations(
    snapshots: tuple[Snapshot, Snapshot], env: Mapping[str, str],
) -> tuple[RemovalOperation, ...]:
    """The exact inverse of ``operations()``: the MCP entry and the marker block."""
    del env  # Cursor has no native-memory conflict to resolve.
    mcp, instructions = snapshots
    return (
        RemovalOperation(
            id="cursor:mcp",
            target=mcp.target,
            label="remove memriver MCP server",
            kind="json-object",
            key_path=("mcpServers", "memriver"),
        ),
        RemovalOperation(
            id="cursor:instructions",
            target=instructions.target,
            label="remove the memriver protocol block from AGENTS.md",
            kind="marker-block",
        ),
    )
