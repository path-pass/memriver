"""Contract tests for the four format-specific install editors.

Each editor is pure text-in/text-out: no filesystem, no harness knowledge. The
assertions below pin the two properties that make ``memriver install`` safe to
run against a file the user already owns -- foreign content survives every
edit, and anything the editor cannot classify with certainty raises
``PlanningError`` instead of guessing.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
import tomlkit
from memriver.install import (
    EditOperation,
    EditResult,
    PlanningError,
    Target,
    apply_edit,
    hook_array_identity_merge,
    hook_group,
    hook_identity,
    json_object_merge,
    marker_block,
    operation_label,
    render_change_summary,
    toml_roundtrip,
    validate_document,
)
from memriver.protocol_text import PROTOCOL_BLOCK

HOME = Path("/tmp")  # where the operation() target below lives
MEMRIVER_MCP = {"command": "uvx", "args": ["memriver"]}
SESSION_START_IDENTITY = ("uvx", "memriver", "hook", "session-start")
NEW_COMMAND = "uvx memriver hook session-start --harness claude-code"


def foreign_group(name: str) -> dict:
    return {
        "matcher": name,
        "hooks": [{"type": "command", "command": f"/opt/{name}/run --quiet"}],
    }


def command_group(command: str, matcher: str = "*") -> dict:
    return {"matcher": matcher, "hooks": [{"type": "command", "command": command}]}


def hooks_json(groups: list[dict], event: str = "SessionStart") -> str:
    return json.dumps({"hooks": {event: groups}}, indent=2) + "\n"


def duplicate_memriver_json() -> str:
    return hooks_json([
        command_group("uvx memriver hook session-start --harness codex"),
        command_group("uvx memriver hook session-start --harness claude-code"),
    ])


def mixed_foreign_and_memriver_group_json() -> str:
    return hooks_json([{
        "matcher": "*",
        "hooks": [
            {"type": "command", "command": "uvx memriver hook session-start"},
            {"type": "command", "command": "/opt/audit/run --quiet"},
        ],
    }])


def operation(kind: str, expected: object, **fields) -> EditOperation:
    target = Target(
        path=Path("/tmp/does-not-matter.json"),
        user_level=True,
        rollback_instruction="remove the memriver entry",
    )
    return EditOperation(
        id="claude-code:mcp",
        target=target,
        label="register memriver MCP server",
        kind=kind,
        expected=expected,
        **fields,
    )


def test_the_installed_hook_command_starts_with_the_identity_that_finds_it():
    """The command written into a harness config and the identity that finds
    that entry again on reinstall are one source: spelled apart, a rename to
    either drifts into a second appended hook group."""
    identity = hook_identity("session-start")
    assert identity == ("uvx", "memriver", "hook", "session-start")
    command = hook_group("session-start", "claude-code")["hooks"][0]["command"]
    assert tuple(shlex.split(command))[: len(identity)] == identity


# --- Step 1: the four editor contracts -------------------------------------


def test_json_object_merge_preserves_foreign_values():
    source = '{"token":"secret","mcpServers":{"other":{"command":"x"}}}'
    result = json_object_merge(
        source, ("mcpServers", "memriver"),
        {"command": "uvx", "args": ["memriver"]},
    )
    parsed = json.loads(result.rendered)
    assert parsed["token"] == "secret"
    assert parsed["mcpServers"]["other"] == {"command": "x"}


def test_hook_identity_merge_replaces_only_memriver_group():
    foreign_a, foreign_b = foreign_group("a"), foreign_group("b")
    old_memriver = command_group("uvx   memriver hook session-start --harness old")
    expected = command_group(
        "uvx memriver hook session-start --harness claude-code",
        matcher="startup|resume|clear|compact",
    )
    result = hook_array_identity_merge(
        hooks_json([foreign_a, old_memriver, foreign_b]),
        "SessionStart", ("uvx", "memriver", "hook", "session-start"), expected,
    )
    assert json.loads(result.rendered)["hooks"]["SessionStart"] == [
        foreign_a, expected, foreign_b
    ]


def test_duplicate_or_mixed_memriver_hook_is_ambiguous():
    identity = ("uvx", "memriver", "hook", "session-start")
    expected = command_group(
        "uvx memriver hook session-start --harness claude-code",
        matcher="startup|resume|clear|compact",
    )
    with pytest.raises(PlanningError):
        hook_array_identity_merge(
            duplicate_memriver_json(), "SessionStart", identity, expected,
        )
    with pytest.raises(PlanningError):
        hook_array_identity_merge(
            mixed_foreign_and_memriver_group_json(),
            "SessionStart", identity, expected,
        )


def test_toml_roundtrip_updates_one_semantic_table_without_duplicate():
    source = '# keep\n[foreign]\ntoken = "secret"\n[mcp_servers.memriver]\ncommand="old"\n'
    result = toml_roundtrip(
        source, ("mcp_servers", "memriver"),
        {"command": "uvx", "args": ["memriver"]},
    )
    parsed = tomlkit.parse(result.rendered)
    assert parsed["foreign"]["token"] == "secret"
    assert parsed["mcp_servers"]["memriver"]["command"] == "uvx"
    assert result.rendered.count("[mcp_servers.memriver]") == 1


@pytest.mark.parametrize(
    "source",
    [
        "<!-- memriver:begin -->\n",
        "<!-- memriver:end -->\n",
        "<!-- memriver:begin -->\na\n<!-- memriver:begin -->\nb\n<!-- memriver:end -->",
    ],
)
def test_broken_markers_fail(source):
    with pytest.raises(PlanningError):
        marker_block(source, PROTOCOL_BLOCK)


# --- more editor behaviour the plan stage depends on -----------------------


def test_json_object_merge_starts_from_empty_document_and_creates_parents():
    result = json_object_merge("", ("mcpServers", "memriver"), MEMRIVER_MCP)
    assert json.loads(result.rendered) == {"mcpServers": {"memriver": MEMRIVER_MCP}}
    assert result.changed and not result.takeover


@pytest.mark.parametrize("source", ["[1, 2]", '"text"', "{oops"])
def test_json_object_merge_rejects_unusable_documents(source):
    with pytest.raises(PlanningError):
        json_object_merge(source, ("mcpServers", "memriver"), MEMRIVER_MCP)


def test_json_object_merge_maps_a_too_deeply_nested_document_to_planning_error():
    """`json.loads` raises `RecursionError`, not `ValueError`, once a
    syntactically legal document nests past the interpreter's limit -- a
    distinct exception that needs its own boundary mapping alongside the
    duplicate-name and non-finite-number guards. 2,000 levels still parses on
    CPython 3.12 and 3.14, and 3.14's C-stack guard admits tens of thousands
    of levels (more on a larger stack); 1,000,000 fails on the parse side
    itself on every supported interpreter, which is the branch this pins."""
    nested = "[" * 1_000_000 + "]" * 1_000_000
    source = '{"foreign": ' + nested + "}"

    with pytest.raises(PlanningError) as raised:
        json_object_merge(source, ("mcpServers", "memriver"), MEMRIVER_MCP)

    assert str(raised.value) == (
        "file nests too deeply for memriver to parse; flatten it and run "
        "install again"
    )
    assert isinstance(raised.value.__cause__, RecursionError)


def test_json_object_merge_maps_an_encoder_recursion_error_to_planning_error(monkeypatch):
    """The render side has its own catch: where the `indent` encoder is pure
    Python (CPython 3.12) it recurses once per level and can give up on a
    document the C decoder accepted. On 3.14 the C encoder handles `indent`,
    so no real document reaches this branch -- force it instead."""
    def too_deep(*args, **kwargs):
        raise RecursionError("maximum recursion depth exceeded")
    monkeypatch.setattr("memriver.install.editors.json.dumps", too_deep)

    with pytest.raises(PlanningError) as raised:
        json_object_merge("{}", ("mcpServers", "memriver"), MEMRIVER_MCP)

    assert str(raised.value) == (
        "file nests too deeply for memriver to parse; flatten it and run "
        "install again"
    )
    assert isinstance(raised.value.__cause__, RecursionError)


def test_json_object_merge_rejects_non_dict_intermediate():
    with pytest.raises(PlanningError):
        json_object_merge(
            '{"mcpServers": "off"}', ("mcpServers", "memriver"), MEMRIVER_MCP,
        )


def test_json_object_merge_takes_over_a_differing_memriver_entry():
    source = json.dumps({"mcpServers": {"memriver": {"command": "python"}}})
    result = json_object_merge(source, ("mcpServers", "memriver"), MEMRIVER_MCP)
    assert result.changed and result.takeover
    assert json.loads(result.rendered)["mcpServers"]["memriver"] == MEMRIVER_MCP


def test_hook_identity_merge_appends_when_no_memriver_group_exists():
    foreign = foreign_group("a")
    expected = command_group(NEW_COMMAND)
    result = hook_array_identity_merge(
        hooks_json([foreign]), "SessionStart", SESSION_START_IDENTITY, expected,
    )
    assert json.loads(result.rendered)["hooks"]["SessionStart"] == [foreign, expected]
    assert result.changed and not result.takeover


def test_hook_identity_merge_creates_missing_event_array():
    expected = command_group(NEW_COMMAND)
    result = hook_array_identity_merge(
        "{}", "SessionStart", SESSION_START_IDENTITY, expected,
    )
    assert json.loads(result.rendered) == {"hooks": {"SessionStart": [expected]}}


def test_hook_identity_merge_rejects_an_unlexable_memriver_command():
    broken = command_group('uvx memriver hook session-start --harness "codex')
    with pytest.raises(PlanningError) as raised:
        hook_array_identity_merge(
            hooks_json([foreign_group("a"), broken]),
            "SessionStart", SESSION_START_IDENTITY, command_group(NEW_COMMAND),
        )
    assert "hooks.SessionStart" in str(raised.value)
    assert "codex" not in str(raised.value)


def test_hook_identity_merge_still_ignores_an_unlexable_foreign_command():
    foreign = command_group('/opt/audit/run --label "unclosed')
    expected = command_group(NEW_COMMAND)
    result = hook_array_identity_merge(
        hooks_json([foreign]), "SessionStart", SESSION_START_IDENTITY, expected,
    )
    assert json.loads(result.rendered)["hooks"]["SessionStart"] == [foreign, expected]


def test_hook_identity_merge_rejects_a_non_array_event():
    with pytest.raises(PlanningError):
        hook_array_identity_merge(
            '{"hooks": {"SessionStart": "off"}}',
            "SessionStart", SESSION_START_IDENTITY, command_group(NEW_COMMAND),
        )


def test_toml_roundtrip_inserts_absent_table_and_keeps_foreign_formatting():
    source = "# keep\n[foreign]\ntoken = 'secret'   # inline note\n"
    result = toml_roundtrip(source, ("mcp_servers", "memriver"), MEMRIVER_MCP)
    assert "# keep" in result.rendered
    assert "token = 'secret'   # inline note" in result.rendered
    assert tomlkit.parse(result.rendered)["mcp_servers"]["memriver"].unwrap() == (
        MEMRIVER_MCP
    )
    assert result.changed and not result.takeover


def test_toml_roundtrip_handles_a_scalar_leaf():
    result = toml_roundtrip("[features]\nmemories = true\n", ("features", "memories"),
                            False)
    assert tomlkit.parse(result.rendered)["features"]["memories"] is False
    assert result.changed and result.takeover


@pytest.mark.parametrize("source", ["mcp_servers = {}\n", "mcp_servers = {a = 1}\n"])
def test_toml_roundtrip_refuses_an_inline_table_parent(source):
    with pytest.raises(PlanningError) as raised:
        toml_roundtrip(source, ("mcp_servers", "memriver"), MEMRIVER_MCP)
    assert "mcp_servers" in str(raised.value)
    assert "a = 1" not in str(raised.value)


def test_toml_roundtrip_replaces_an_inline_table_leaf():
    source = '[mcp_servers]\nmemriver = {command = "old"}\n'
    result = toml_roundtrip(source, ("mcp_servers", "memriver"), MEMRIVER_MCP)
    assert result.changed and result.takeover
    parsed = tomlkit.parse(result.rendered)
    assert parsed["mcp_servers"]["memriver"].unwrap() == MEMRIVER_MCP
    assert result.rendered.count("memriver") == 2  # the table header and the arg


def test_toml_roundtrip_leaves_a_matching_inline_table_leaf_alone():
    source = '[mcp_servers]\nmemriver = {command = "uvx", args = ["memriver"]}\n'
    result = toml_roundtrip(source, ("mcp_servers", "memriver"), MEMRIVER_MCP)
    assert result.rendered == source
    assert not result.changed and not result.takeover


def test_toml_roundtrip_rejects_broken_toml_and_scalar_intermediates():
    with pytest.raises(PlanningError):
        toml_roundtrip("nope = = 1\n", ("mcp_servers", "memriver"), MEMRIVER_MCP)
    with pytest.raises(PlanningError):
        toml_roundtrip('mcp_servers = "off"\n', ("mcp_servers", "memriver"),
                       MEMRIVER_MCP)


def test_marker_block_appends_after_the_files_own_tail():
    result = marker_block("# Project\n\nnotes\n", PROTOCOL_BLOCK)
    assert result.rendered == (
        "# Project\n\nnotes\n\n"
        f"<!-- memriver:begin -->\n{PROTOCOL_BLOCK}\n<!-- memriver:end -->\n"
    )
    assert result.changed and not result.takeover


def test_marker_block_replaces_a_single_pair_in_place():
    source = (
        "# Project\n\n<!-- memriver:begin -->\nstale\n<!-- memriver:end -->\n\ntail\n"
    )
    result = marker_block(source, PROTOCOL_BLOCK)
    assert result.rendered == (
        "# Project\n\n"
        f"<!-- memriver:begin -->\n{PROTOCOL_BLOCK}\n<!-- memriver:end -->\n\ntail\n"
    )
    assert result.changed and result.takeover


# --- what install renders on the tails that used to be ambiguous ------------

# The two tests below pin a deliberate change to install's own output. An
# appended table or block used to be padded up to a blank line, with the
# file's own trailing newlines stripped or absorbed first, so a file ending in
# no newline, one, or three all produced the same bytes -- information no
# removal could ever give back, and the reason uninstall could not restore a
# file it had not written itself. The separator is now exactly one newline
# sequence in front of whatever the file already ended with. That is a visible
# difference in install's rendering for these tails, accepted for the sake of
# an invertible pair, and pinned here so it stays deliberate.


def test_install_appends_a_toml_table_after_one_newline_whatever_the_tail_is():
    assert toml_roundtrip("model = 'gpt'", ("mcp_servers", "memriver"),
                          MEMRIVER_MCP).rendered == (
        "model = 'gpt'\n"
        '[mcp_servers.memriver]\ncommand = "uvx"\nargs = ["memriver"]\n'
    )
    assert toml_roundtrip("model = 'gpt'\n\n\n", ("mcp_servers", "memriver"),
                          MEMRIVER_MCP).rendered == (
        "model = 'gpt'\n\n\n\n"
        '[mcp_servers.memriver]\ncommand = "uvx"\nargs = ["memriver"]\n'
    )


def test_install_appends_a_marker_block_after_one_newline_whatever_the_tail_is():
    block = f"<!-- memriver:begin -->\n{PROTOCOL_BLOCK}\n<!-- memriver:end -->\n"
    assert marker_block("notes", PROTOCOL_BLOCK).rendered == "notes\n" + block
    assert marker_block("notes\n\n\n", PROTOCOL_BLOCK).rendered == (
        "notes\n\n\n\n" + block)
    # an empty file gets no separator at all -- there is nothing to separate from
    assert marker_block("", PROTOCOL_BLOCK).rendered == block


# --- Step 2: idempotency ---------------------------------------------------


def test_json_object_merge_is_idempotent():
    first = json_object_merge('{"token":"secret"}', ("mcpServers", "memriver"),
                              MEMRIVER_MCP)
    again = json_object_merge(first.rendered, ("mcpServers", "memriver"), MEMRIVER_MCP)
    assert again.rendered == first.rendered
    assert not again.changed and not again.takeover


def test_hook_identity_merge_is_idempotent():
    expected = command_group(NEW_COMMAND, matcher="startup|resume|clear|compact")
    first = hook_array_identity_merge(
        hooks_json([foreign_group("a")]), "SessionStart",
        SESSION_START_IDENTITY, expected,
    )
    again = hook_array_identity_merge(
        first.rendered, "SessionStart", SESSION_START_IDENTITY, expected,
    )
    assert again.rendered == first.rendered
    assert not again.changed and not again.takeover


def test_toml_roundtrip_is_idempotent():
    source = '# keep\n[foreign]\ntoken = "secret"\n'
    first = toml_roundtrip(source, ("mcp_servers", "memriver"), MEMRIVER_MCP)
    again = toml_roundtrip(first.rendered, ("mcp_servers", "memriver"), MEMRIVER_MCP)
    assert again.rendered == first.rendered
    assert not again.changed and not again.takeover


def test_marker_block_is_idempotent():
    first = marker_block("# Project\n", PROTOCOL_BLOCK)
    again = marker_block(first.rendered, PROTOCOL_BLOCK)
    assert again.rendered == first.rendered
    assert not again.changed and not again.takeover


# --- Step 2: change summary ------------------------------------------------


def test_the_label_names_the_harness_and_the_target_it_writes():
    """With --all, four changes share the words "register memriver MCP server"
    and differ only by harness and file; a confirmation that shows neither is
    unanswerable. A home-relative path is abbreviated, never spelled out."""
    op = operation("json-object", MEMRIVER_MCP, key_path=("mcpServers", "memriver"))

    assert operation_label(op, HOME) == (
        "claude-code: register memriver MCP server -> ~/does-not-matter.json")
    assert operation_label(op, Path("/elsewhere")) == (
        "claude-code: register memriver MCP server -> /tmp/does-not-matter.json")


def test_change_summary_shows_label_key_path_and_new_fragment():
    op = operation("json-object", MEMRIVER_MCP, key_path=("mcpServers", "memriver"))
    result = json_object_merge('{"token":"secret"}', op.key_path, op.expected)
    summary = render_change_summary(op, result, HOME)
    assert summary.startswith(operation_label(op, HOME) + "\n")
    assert "mcpServers.memriver" in summary
    assert '"command": "uvx"' in summary
    assert "secret" not in summary


def test_change_summary_shows_the_new_hook_commands_and_takeover_line():
    expected = command_group(NEW_COMMAND, matcher="startup|resume|clear|compact")
    op = operation(
        "hook-array", expected,
        key_path=("hooks", "SessionStart"), identity=SESSION_START_IDENTITY,
    )
    source = hooks_json([
        command_group("uvx memriver hook session-start --harness old-secret-value"),
    ])
    result = hook_array_identity_merge(source, "SessionStart", op.identity, expected)
    summary = render_change_summary(op, result, HOME)
    assert result.takeover
    assert "hooks.SessionStart" in summary
    assert NEW_COMMAND in summary
    assert summary.rstrip("\n").endswith(
        "existing memriver entry differs and will be replaced (old value not shown)"
    )
    assert "old-secret-value" not in summary


def test_change_summary_names_the_harness_for_a_key_memriver_does_not_own():
    """``env.CLAUDE_CODE_DISABLE_AUTO_MEMORY`` and ``features.memories`` are the
    harness's own settings that memriver turns off, so the takeover line must
    not call the old value a memriver entry."""
    op = operation("json-object", "1", harness_owned=True,
                   key_path=("env", "CLAUDE_CODE_DISABLE_AUTO_MEMORY"))
    result = json_object_merge('{"env":{"CLAUDE_CODE_DISABLE_AUTO_MEMORY":"0"}}',
                               op.key_path, op.expected)
    summary = render_change_summary(op, result, HOME)
    assert result.takeover
    assert summary.rstrip("\n").endswith(
        "existing harness setting differs and will be replaced (old value not shown)"
    )
    assert "existing memriver entry" not in summary


def test_change_summary_of_a_fresh_change_has_no_takeover_line():
    op = operation("toml-table", MEMRIVER_MCP, key_path=("mcp_servers", "memriver"))
    result = toml_roundtrip("", op.key_path, op.expected)
    summary = render_change_summary(op, result, HOME)
    assert "[mcp_servers.memriver]" in summary
    assert 'command = "uvx"' in summary
    assert "old value not shown" not in summary


def test_change_summary_of_a_marker_block_names_the_region():
    op = operation("marker-block", f"<!-- memriver:begin -->\n{PROTOCOL_BLOCK}\n"
                                   "<!-- memriver:end -->")
    summary = render_change_summary(op, EditResult(rendered="", changed=True,
                                                   takeover=False), HOME)
    assert "<!-- memriver:begin -->" in summary
    assert "memriver shared memory" in summary


# --- dispatch --------------------------------------------------------------


def test_apply_edit_dispatches_by_kind():
    op = operation("json-object", MEMRIVER_MCP, key_path=("mcpServers", "memriver"))
    assert json.loads(apply_edit(op, "{}").rendered) == {
        "mcpServers": {"memriver": MEMRIVER_MCP}
    }


def test_apply_edit_rejects_operations_missing_kind_specific_fields():
    with pytest.raises(PlanningError):
        apply_edit(operation("json-object", None, key_path=("mcpServers",)), "{}")
    with pytest.raises(PlanningError):
        apply_edit(operation("json-object", MEMRIVER_MCP), "{}")
    with pytest.raises(PlanningError):
        apply_edit(
            operation("hook-array", command_group(NEW_COMMAND),
                      key_path=("hooks", "SessionStart")),
            "{}",
        )
    with pytest.raises(PlanningError):
        apply_edit(operation("marker-block", 42), "")


@pytest.mark.parametrize(
    "expected",
    [
        {"type": "command", "command": NEW_COMMAND},          # a bare handler
        {"matcher": "*", "hooks": []},                         # no handler at all
        command_group("/opt/audit/run --quiet"),               # not memriver's command
        {"matcher": "*", "hooks": [
            {"type": "command", "command": NEW_COMMAND},
            {"type": "command", "command": "/opt/audit/run"},
        ]},                                                    # a mixed group
    ],
)
def test_apply_edit_rejects_a_hook_group_it_could_not_find_again(expected):
    op = operation(
        "hook-array", expected,
        key_path=("hooks", "SessionStart"), identity=SESSION_START_IDENTITY,
    )
    with pytest.raises(PlanningError):
        apply_edit(op, "{}")


@pytest.mark.parametrize(
    "key_path", [("SessionStart",), ("plugins", "hooks", "SessionStart"),
                 ("plugins", "SessionStart")],
)
def test_apply_edit_rejects_a_hook_key_path_the_editor_would_not_touch(key_path):
    op = operation(
        "hook-array", command_group(NEW_COMMAND),
        key_path=key_path, identity=SESSION_START_IDENTITY,
    )
    with pytest.raises(PlanningError):
        apply_edit(op, "{}")


# --- the exported validator still answers a two-argument call ---------------


def test_validate_document_names_install_when_no_command_is_given():
    """``validate_document`` is part of this package's public surface, so a
    two-argument call has to keep working. The command a caller ran is
    something only that caller knows; one that does not say gets install, the
    command every existing caller of this signature was running."""
    with pytest.raises(PlanningError) as raised:
        validate_document("<!-- memriver:begin -->\nno end in sight\n",
                          "marker-block")

    assert "run install again" in str(raised.value)


def test_validate_document_still_takes_the_command_that_is_running():
    with pytest.raises(PlanningError) as raised:
        validate_document("<!-- memriver:begin -->\nno end in sight\n",
                          "marker-block", "uninstall")

    assert "run uninstall again" in str(raised.value)
