"""Declarative installation plan for Kiro.

``~/.kiro/settings/mcp.json`` is user-level and carries the MCP server
registration; ``.kiro/steering/memriver.md`` at the nearest git root is
project-level and carries the marker-managed protocol block. This module
only builds ``Target`` and ``EditOperation`` values -- it never opens a file,
prompts, or writes. Creating the missing ``.kiro/steering/`` parent is the
transaction layer's job, done only after the steering-file change is
confirmed; this target's path carries that intent implicitly.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from memriver.install.editors import EditOperation, PlanningError, Snapshot, Target
from memriver.protocol_text import PROTOCOL_BLOCK

_MCP_PAYLOAD = {"command": "uvx", "args": ["memriver"]}


def targets(home: Path, project_root: Path | None) -> tuple[Target, Target]:
    """``(~/.kiro/settings/mcp.json, <git-root>/.kiro/steering/memriver.md)``.

    Raises ``PlanningError`` before returning anything when ``project_root``
    is ``None`` -- Kiro's steering file has no user-level home.
    """
    if project_root is None:
        raise PlanningError(
            "kiro needs a project (the nearest current-or-ancestor .git root) "
            "to manage its steering file; run install inside a project or pass one"
        )
    mcp = Target(
        path=home / ".kiro" / "settings" / "mcp.json",
        user_level=True,
        rollback_instruction="remove mcpServers.memriver from ~/.kiro/settings/mcp.json",
    )
    instructions = Target(
        path=project_root / ".kiro" / "steering" / "memriver.md",
        user_level=False,
        rollback_instruction="remove .kiro/steering/memriver.md",
    )
    return mcp, instructions


def operations(
    snapshots: tuple[Snapshot, Snapshot], env: Mapping[str, str],
) -> tuple[EditOperation, ...]:
    """MCP registration plus the marker-managed steering file."""
    del env  # Kiro has no native-memory conflict to resolve.
    mcp, instructions = snapshots
    return (
        EditOperation(
            id="kiro:mcp",
            target=mcp.target,
            label="register memriver MCP server",
            kind="json-object",
            expected=_MCP_PAYLOAD,
            key_path=("mcpServers", "memriver"),
        ),
        EditOperation(
            id="kiro:instructions",
            target=instructions.target,
            label="add the memriver protocol block to the steering file",
            kind="marker-block",
            expected=PROTOCOL_BLOCK,
        ),
    )
