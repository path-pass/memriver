"""Planning contracts for the four declarative harness installers.

Every module here only builds ``Target``/``EditOperation`` values from
``targets()``/``operations()``; nothing touches the filesystem, so all paths
below are plain compositions that never need to exist. The exact payload
constants and target paths are pinned by the phase-2 spec (5.5) and mirrored
here so a change to either is caught immediately.
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest
import tomlkit
from memriver.install import PlanningError, Snapshot, claude_code, codex, cursor, kiro
from memriver.protocol_text import PROTOCOL_BLOCK

HOME = Path("/home/user")

MCP = {"command": "uvx", "args": ["memriver"]}

CLAUDE_SESSION = {
    "hooks": [{
        "type": "command",
        "command": "uvx memriver hook session-start --harness claude-code",
    }],
}
CLAUDE_STOP = {
    "hooks": [{
        "type": "command",
        "command": "uvx memriver hook stop --harness claude-code",
    }],
}
CODEX_SESSION = {
    "hooks": [{
        "type": "command",
        "command": "uvx memriver hook session-start --harness codex",
    }],
}
CODEX_STOP = {
    "hooks": [{
        "type": "command",
        "command": "uvx memriver hook stop --harness codex",
    }],
}


def _snapshot(target, text: str = "") -> Snapshot:
    return Snapshot(target=target, text=text, mode=None)


def claude_snapshots(*, claude_json: dict | None = None,
                      settings_json: dict | None = None) -> tuple[Snapshot, Snapshot]:
    config_target, settings_target = claude_code.targets(HOME, None)
    config_text = json.dumps(claude_json) if claude_json is not None else "{}"
    settings_text = json.dumps(settings_json) if settings_json is not None else "{}"
    return (_snapshot(config_target, config_text), _snapshot(settings_target, settings_text))


def codex_snapshots(*, config: dict | None = None) -> tuple[Snapshot, Snapshot]:
    config_target, hooks_target = codex.targets(HOME, None)
    config_text = tomlkit.dumps(config) if config is not None else ""
    return (_snapshot(config_target, config_text), _snapshot(hooks_target, "{}"))


# --- Step 2: targets --------------------------------------------------------


def test_claude_code_targets():
    config, settings = claude_code.targets(HOME, None)
    assert config.path == HOME / ".claude.json"
    assert settings.path == HOME / ".claude" / "settings.json"
    assert config.user_level and settings.user_level


def test_codex_targets():
    config, hooks = codex.targets(HOME, None)
    assert config.path == HOME / ".codex" / "config.toml"
    assert hooks.path == HOME / ".codex" / "hooks.json"
    assert config.user_level and hooks.user_level


def test_cursor_targets_need_a_project(tmp_path):
    with pytest.raises(PlanningError):
        cursor.targets(HOME, None)
    mcp, instructions = cursor.targets(HOME, tmp_path)
    assert mcp.path == HOME / ".cursor" / "mcp.json"
    assert instructions.path == tmp_path / "AGENTS.md"
    assert mcp.user_level and not instructions.user_level


def test_kiro_targets_need_a_project(tmp_path):
    with pytest.raises(PlanningError):
        kiro.targets(HOME, None)
    mcp, instructions = kiro.targets(HOME, tmp_path)
    assert mcp.path == HOME / ".kiro" / "settings" / "mcp.json"
    assert instructions.path == tmp_path / ".kiro" / "steering" / "memriver.md"
    assert mcp.user_level and not instructions.user_level


# --- Step 2: exact semantic payloads ----------------------------------------


def test_claude_code_operation_payloads():
    snapshots = claude_snapshots()
    ops = claude_code.operations(snapshots, {})
    by_id = {op.id: op for op in ops}

    mcp_op = by_id["claude-code:mcp"]
    assert mcp_op.kind == "json-object"
    assert mcp_op.key_path == ("mcpServers", "memriver")
    assert mcp_op.expected == MCP

    session_op = by_id["claude-code:hooks-session-start"]
    assert session_op.kind == "hook-array"
    assert session_op.key_path == ("hooks", "SessionStart")
    assert session_op.identity == ("uvx", "memriver", "hook", "session-start")
    assert session_op.expected == CLAUDE_SESSION

    stop_op = by_id["claude-code:hooks-stop"]
    assert stop_op.kind == "hook-array"
    assert stop_op.key_path == ("hooks", "Stop")
    assert stop_op.identity == ("uvx", "memriver", "hook", "stop")
    assert stop_op.expected == CLAUDE_STOP


def test_codex_operation_payloads():
    snapshots = codex_snapshots()
    ops = codex.operations(snapshots, {})
    by_id = {op.id: op for op in ops}

    mcp_op = by_id["codex:mcp"]
    assert mcp_op.kind == "toml-table"
    assert mcp_op.key_path == ("mcp_servers", "memriver")
    assert mcp_op.expected == MCP

    session_op = by_id["codex:hooks-session-start"]
    assert session_op.kind == "hook-array"
    assert session_op.key_path == ("hooks", "SessionStart")
    assert session_op.identity == ("uvx", "memriver", "hook", "session-start")
    assert session_op.expected == CODEX_SESSION

    stop_op = by_id["codex:hooks-stop"]
    assert stop_op.kind == "hook-array"
    assert stop_op.key_path == ("hooks", "Stop")
    assert stop_op.identity == ("uvx", "memriver", "hook", "stop")
    assert stop_op.expected == CODEX_STOP


def test_cursor_operation_payloads(tmp_path):
    mcp_target, instructions_target = cursor.targets(HOME, tmp_path)
    snapshots = (_snapshot(mcp_target), _snapshot(instructions_target))
    ops = cursor.operations(snapshots, {})
    by_id = {op.id: op for op in ops}

    mcp_op = by_id["cursor:mcp"]
    assert mcp_op.kind == "json-object"
    assert mcp_op.key_path == ("mcpServers", "memriver")
    assert mcp_op.expected == MCP

    instructions_op = by_id["cursor:instructions"]
    assert instructions_op.kind == "marker-block"
    assert instructions_op.expected == PROTOCOL_BLOCK


def test_kiro_operation_payloads(tmp_path):
    mcp_target, instructions_target = kiro.targets(HOME, tmp_path)
    snapshots = (_snapshot(mcp_target), _snapshot(instructions_target))
    ops = kiro.operations(snapshots, {})
    by_id = {op.id: op for op in ops}

    mcp_op = by_id["kiro:mcp"]
    assert mcp_op.kind == "json-object"
    assert mcp_op.key_path == ("mcpServers", "memriver")
    assert mcp_op.expected == MCP

    instructions_op = by_id["kiro:instructions"]
    assert instructions_op.kind == "marker-block"
    assert instructions_op.expected == PROTOCOL_BLOCK


# --- Step 3: native-memory decisions ----------------------------------------


@pytest.mark.parametrize(
    ("env", "settings", "expected_optional"),
    [
        ({"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}, {}, False),
        ({}, {"autoMemoryEnabled": False}, False),
        ({}, {}, True),
        ({"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0"}, {}, True),
    ],
)
def test_claude_native_memory_offer(env, settings, expected_optional):
    snapshots = claude_snapshots(settings_json=settings)
    operations = claude_code.operations(snapshots, env)
    optional = [operation for operation in operations if operation.optional]
    assert bool(optional) is expected_optional
    if optional:
        assert "disable built-in auto memory" in optional[0].label
        assert optional[0].id not in {
            "claude-code:mcp", "claude-code:hooks-session-start",
            "claude-code:hooks-stop",
        }


@pytest.mark.parametrize(
    ("config", "expected_optional"),
    [
        ({"features": {"memories": True}}, True),
        ({"features": {"memories": False}}, False),
        ({}, False),
        (None, False),
    ],
)
def test_codex_native_memory_offer(config, expected_optional):
    snapshots = codex_snapshots(config=config)
    operations = codex.operations(snapshots, {})
    optional = [operation for operation in operations if operation.optional]
    assert bool(optional) is expected_optional
    if optional:
        assert "disable built-in auto memory" in optional[0].label
        assert optional[0].kind == "toml-table"
        assert optional[0].key_path == ("features", "memories")
        assert optional[0].expected is False


# --- planning never touches the filesystem ----------------------------------


def test_planning_performs_no_filesystem_writes(tmp_path, monkeypatch):
    def _forbidden_open(*args, **kwargs):
        raise AssertionError("planners must never open a file")

    monkeypatch.setattr(builtins, "open", _forbidden_open)

    claude_code.operations(claude_snapshots(), {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
    codex.operations(codex_snapshots(), {})

    cursor_mcp, cursor_instructions = cursor.targets(HOME, tmp_path)
    cursor.operations((_snapshot(cursor_mcp), _snapshot(cursor_instructions)), {})

    kiro_mcp, kiro_instructions = kiro.targets(HOME, tmp_path)
    kiro.operations((_snapshot(kiro_mcp), _snapshot(kiro_instructions)), {})
