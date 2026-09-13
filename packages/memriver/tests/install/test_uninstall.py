"""Contracts for ``memriver uninstall``: the exact inverse of install.

Three levels are pinned here, mirroring how install itself is tested across
``test_editors.py``, ``test_harnesses.py`` and ``test_install.py``.

*The four removers are pure text-in/text-out, like their merge counterparts.*
An absent entry is already clean, not an error; foreign content and
formatting survive; anything ambiguous still raises ``PlanningError`` rather
than guessing.

*Each harness's ``uninstall_operations()`` targets exactly what its
``operations()`` writes* -- the MCP entry and the hooks/marker block, never
the native-memory toggle, which spec 5.3 leaves for install alone to manage.

*``run_uninstall`` reuses install's whole transaction machinery.* Preflight
writes nothing until every structural check passes, consent is explicit and
per change, and a failed apply rolls every touched target back from its
backup -- the same properties ``test_install.py`` pins for the write side.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest
import tomlkit
from memriver.install import (
    HARNESS_SETTING_TAKEOVER_NOTICE,
    PlanningError,
    apply_removal,
    claude_code,
    codex,
    cursor,
    hook_array_identity_remove,
    json_object_remove,
    kiro,
    marker_block,
    marker_block_remove,
    run_config_uninstall,
    run_install,
    toml_table_remove,
)
from memriver.install.codex import NATIVE_MEMORY_LEFT_NOTE as CODEX_NATIVE_MEMORY_LEFT
from memriver.protocol_text import PROTOCOL_BLOCK
from memriver.uninstall import run_uninstall as run_full_uninstall

ALL_HARNESSES = ["claude-code", "codex", "cursor", "kiro"]


# --- harness (mirrors test_install.py's own) ---------------------------------


class ReplaceSpy:
    def __init__(self, fail_at: set[int] | None = None,
                 raises: type[BaseException] = OSError) -> None:
        self.calls: list[tuple[Path, Path]] = []
        self.fail_at = fail_at or set()
        self.raises = raises

    def __call__(self, source, destination) -> None:
        import os
        self.calls.append((Path(source), Path(destination)))
        if len(self.calls) in self.fail_at:
            raise self.raises(f"injected replacement failure #{len(self.calls)}")
        os.replace(source, destination)


class Answers:
    def __init__(self, replies) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.replies:
            raise AssertionError(f"unexpected extra prompt: {prompt!r}")
        return self.replies.pop(0)


def refuse_to_read(prompt: str) -> str:
    raise EOFError(prompt)


class Run:
    def __init__(self, exit_code: int, stdout: str, answers: Answers | None,
                 replace: ReplaceSpy) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.answers = answers
        self.replace = replace


def install(harnesses, *, home: Path, cwd: Path, yes: bool = True,
           env: dict | None = None) -> Run:
    out = io.StringIO()
    replace = ReplaceSpy()
    exit_code = run_install(
        harnesses, yes=yes, dry_run=False, home=home, cwd=cwd,
        env=env if env is not None else {}, input_fn=refuse_to_read, stdout=out,
        replace_file=replace,
    )
    return Run(exit_code, out.getvalue(), None, replace)


def uninstall(harnesses, *, home: Path, cwd: Path, yes: bool = True,
             dry_run: bool = False, env: dict | None = None, replies=None,
             input_fn=None, replace: ReplaceSpy | None = None) -> Run:
    out = io.StringIO()
    replace = replace if replace is not None else ReplaceSpy()
    answers = None
    if input_fn is None:
        answers = Answers(replies or [])
        input_fn = answers
    exit_code = run_config_uninstall(
        harnesses, yes=yes, dry_run=dry_run, home=home, cwd=cwd,
        env=env if env is not None else {}, input_fn=input_fn, stdout=out,
        replace_file=replace,
    )
    return Run(exit_code, out.getvalue(), answers, replace)


def full_uninstall(harnesses, *, home: Path, cwd: Path, yes: bool = True,
                   dry_run: bool = False, purge_data: bool = False,
                   clean_uv_cache: bool = False, env: dict | None = None,
                   root: Path | None = None,
                   replies=None, input_fn=None) -> Run:
    out = io.StringIO()
    answers = None
    if input_fn is None:
        answers = Answers(replies or [])
        input_fn = answers
    exit_code = run_full_uninstall(
        harnesses, yes=yes, dry_run=dry_run, purge_data=purge_data,
        clean_uv_cache=clean_uv_cache, root=root, home=home, cwd=cwd,
        env=env if env is not None else {}, input_fn=input_fn, stdout=out,
        replace_file=lambda s, d: __import__("os").replace(s, d),
    )
    return Run(exit_code, out.getvalue(), answers, None)


def snapshot_tree(root: Path) -> dict:
    import os
    tree: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        if path.is_symlink():
            tree[key] = ("symlink", os.readlink(path))
        elif path.is_file():
            tree[key] = ("file", path.read_bytes(), path.stat().st_mode & 0o777)
        else:
            tree[key] = ("dir",)
    return tree


def backups(root: Path) -> list[Path]:
    return sorted(root.rglob("*.memriver-backup-*"))


def write(path: Path, text: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    (path / ".git").mkdir(parents=True)
    return path


def hook_group(command: str) -> dict:
    return {"hooks": [{"type": "command", "command": command}]}


SESSION_START_IDENTITY = ("uvx", "memriver", "hook", "session-start")


# --- Step 1: the four removers, pure text-in/text-out ------------------------


def test_json_object_remove_deletes_only_the_leaf_and_leaves_an_empty_parent():
    # a pre-existing empty container (the user's own, or one install
    # auto-created) is never pruned -- P2-3: removal deletes only memriver's
    # own leaf, an emptied container stays behind as an empty container
    result = json_object_remove(
        '{"token":"secret","mcpServers":{"memriver":{"command":"uvx"}}}',
        ("mcpServers", "memriver"),
    )
    assert json.loads(result.rendered) == {"token": "secret", "mcpServers": {}}
    assert result.changed and not result.takeover


def test_json_object_remove_leaves_a_populated_parent_alone():
    result = json_object_remove(
        '{"mcpServers":{"memriver":{"command":"uvx"},"other":{"command":"x"}}}',
        ("mcpServers", "memriver"),
    )
    assert json.loads(result.rendered) == {"mcpServers": {"other": {"command": "x"}}}


def test_json_object_remove_is_a_no_op_on_an_absent_key():
    source = '{"token":"secret"}'
    result = json_object_remove(source, ("mcpServers", "memriver"))
    assert result.rendered == source
    assert not result.changed and not result.takeover


def test_json_object_remove_is_a_no_op_when_an_intermediate_key_is_not_an_object():
    source = '{"mcpServers":"off"}'
    result = json_object_remove(source, ("mcpServers", "memriver"))
    assert result.rendered == source
    assert not result.changed


def test_json_object_remove_rejects_broken_json():
    with pytest.raises(PlanningError):
        json_object_remove("{ not json", ("mcpServers", "memriver"))


def test_json_object_remove_is_idempotent():
    once = json_object_remove(
        '{"mcpServers":{"memriver":{"command":"uvx"}}}', ("mcpServers", "memriver"),
    )
    twice = json_object_remove(once.rendered, ("mcpServers", "memriver"))
    assert twice.rendered == once.rendered
    assert not twice.changed


def test_toml_table_remove_deletes_the_leaf_and_leaves_the_auto_created_super_table():
    # install auto-creates mcp_servers as a headerless super table -- an empty
    # one renders invisibly, so no pruning is needed to make it disappear
    source = '# keep\n[foreign]\ntoken = "secret"\n\n[mcp_servers.memriver]\ncommand = "uvx"\n'
    result = toml_table_remove(source, ("mcp_servers", "memriver"))
    assert "# keep" in result.rendered
    assert "mcp_servers" not in result.rendered
    assert tomlkit.parse(result.rendered)["foreign"]["token"] == "secret"


def test_toml_table_remove_never_prunes_a_container_the_user_pre_created():
    # P2-3: a real, user-authored [mcp_servers] table (with its own comment)
    # is never deleted, even once memriver's own leaf inside it is gone
    source = '# my servers live here\n[mcp_servers]\n\n[mcp_servers.memriver]\ncommand = "uvx"\n'
    result = toml_table_remove(source, ("mcp_servers", "memriver"))
    assert "# my servers live here" in result.rendered
    assert "[mcp_servers]" in result.rendered


def test_toml_table_remove_round_trips_back_to_the_exact_original_bytes():
    source = 'model = "gpt"\nforeign = "keep"\n'
    from memriver.install import toml_roundtrip
    merged = toml_roundtrip(source, ("mcp_servers", "memriver"),
                            {"command": "uvx", "args": ["memriver"]})
    result = toml_table_remove(merged.rendered, ("mcp_servers", "memriver"))
    assert result.rendered == source


def test_toml_table_remove_leaves_a_sibling_table_alone():
    source = '[mcp_servers.other]\ncommand = "x"\n\n[mcp_servers.memriver]\ncommand = "uvx"\n'
    result = toml_table_remove(source, ("mcp_servers", "memriver"))
    parsed = tomlkit.parse(result.rendered)
    assert parsed["mcp_servers"]["other"]["command"] == "x"
    assert "memriver" not in parsed["mcp_servers"]


def test_toml_table_remove_preserves_an_unrelated_tables_own_blank_line_tail():
    # [other]'s own trailing blank lines have nothing to do with the deleted
    # mcp_servers.memriver leaf; a normalization scoped to the whole document
    # (rather than to what the deletion actually touched) must not collapse them
    source = ('[mcp_servers.memriver]\ncommand = "uvx"\n\n'
             '[other]\ntoken = "keep"\n\n\n')
    result = toml_table_remove(source, ("mcp_servers", "memriver"))
    assert result.rendered.endswith('token = "keep"\n\n\n')


def test_toml_table_remove_is_a_no_op_on_an_absent_key():
    source = '[foreign]\ntoken = "secret"\n'
    result = toml_table_remove(source, ("mcp_servers", "memriver"))
    assert result.rendered == source
    assert not result.changed


def test_hook_array_identity_remove_deletes_only_the_matching_group():
    foreign = {"matcher": "a", "hooks": [{"type": "command", "command": "/opt/a/run"}]}
    memriver = hook_group("uvx memriver hook session-start --harness claude-code")
    source = json.dumps({"hooks": {"SessionStart": [foreign, memriver]}})

    result = hook_array_identity_remove(source, "SessionStart", SESSION_START_IDENTITY)

    assert json.loads(result.rendered)["hooks"]["SessionStart"] == [foreign]
    assert result.changed and not result.takeover


def test_hook_array_identity_remove_leaves_an_empty_event_array_and_hooks_object():
    # P2-3: a pre-existing {"hooks": {}, ...} container (the user's own, or
    # one install auto-created) is never pruned away
    memriver = hook_group("uvx memriver hook session-start --harness claude-code")
    source = json.dumps({"hooks": {"SessionStart": [memriver]}, "env": {"X": "1"}})

    result = hook_array_identity_remove(source, "SessionStart", SESSION_START_IDENTITY)

    parsed = json.loads(result.rendered)
    assert parsed == {"hooks": {"SessionStart": []}, "env": {"X": "1"}}


def test_hook_array_identity_remove_keeps_the_event_key_when_others_remain():
    foreign = {"matcher": "a", "hooks": [{"type": "command", "command": "/opt/a/run"}]}
    memriver = hook_group("uvx memriver hook session-start --harness claude-code")
    source = json.dumps({"hooks": {"SessionStart": [foreign, memriver],
                                   "Stop": [memriver]}})

    result = hook_array_identity_remove(source, "SessionStart", SESSION_START_IDENTITY)

    parsed = json.loads(result.rendered)
    assert parsed["hooks"]["SessionStart"] == [foreign]
    assert "Stop" in parsed["hooks"]


def test_hook_array_identity_remove_is_a_no_op_when_absent():
    source = json.dumps({"hooks": {"SessionStart": []}})
    result = hook_array_identity_remove(source, "SessionStart", SESSION_START_IDENTITY)
    assert result.rendered == source
    assert not result.changed


def test_hook_array_identity_remove_is_a_no_op_on_a_missing_event():
    source = json.dumps({"hooks": {}})
    result = hook_array_identity_remove(source, "SessionStart", SESSION_START_IDENTITY)
    assert result.rendered == source
    assert not result.changed


def test_hook_array_identity_remove_rejects_more_than_one_match():
    memriver = hook_group("uvx memriver hook session-start --harness claude-code")
    other_memriver = hook_group("uvx memriver hook session-start --harness codex")
    source = json.dumps({"hooks": {"SessionStart": [memriver, other_memriver]}})
    with pytest.raises(PlanningError):
        hook_array_identity_remove(source, "SessionStart", SESSION_START_IDENTITY)


def test_hook_array_identity_remove_refuses_a_group_shared_with_a_foreign_handler():
    mixed = {
        "matcher": "*",
        "hooks": [
            {"type": "command", "command": "uvx memriver hook session-start"},
            {"type": "command", "command": "/opt/audit/run"},
        ],
    }
    source = json.dumps({"hooks": {"SessionStart": [mixed]}})
    with pytest.raises(PlanningError):
        hook_array_identity_remove(source, "SessionStart", SESSION_START_IDENTITY)


def test_hook_array_identity_remove_is_idempotent():
    memriver = hook_group("uvx memriver hook session-start --harness claude-code")
    source = json.dumps({"hooks": {"SessionStart": [memriver]}})
    once = hook_array_identity_remove(source, "SessionStart", SESSION_START_IDENTITY)
    twice = hook_array_identity_remove(once.rendered, "SessionStart",
                                       SESSION_START_IDENTITY)
    assert twice.rendered == once.rendered
    assert not twice.changed


def test_marker_block_remove_restores_the_exact_original_bytes():
    source = "# Project\n\nnotes\n"
    merged = marker_block(source, PROTOCOL_BLOCK)
    result = marker_block_remove(merged.rendered)
    assert result.rendered == source
    assert result.changed and not result.takeover


def test_marker_block_remove_is_a_no_op_when_there_is_no_block():
    source = "# Project\n\nnotes\n"
    result = marker_block_remove(source)
    assert result.rendered == source
    assert not result.changed


def test_marker_block_remove_leaves_trailing_content_intact():
    source = (
        "# Project\n\n<!-- memriver:begin -->\nstale\n<!-- memriver:end -->\n\ntail\n"
    )
    result = marker_block_remove(source)
    assert result.rendered == "# Project\n\ntail\n"


def test_marker_block_remove_does_not_add_a_trailing_newline_that_was_never_there():
    # the block sits right at the end of a file whose own content never had a
    # trailing newline; removal must not manufacture one
    source = "notes<!-- memriver:begin -->\nblock\n<!-- memriver:end -->"
    result = marker_block_remove(source)
    assert result.rendered == "notes"


def test_marker_block_remove_rejects_broken_markers():
    with pytest.raises(PlanningError):
        marker_block_remove("<!-- memriver:begin -->\na\n<!-- memriver:begin -->\nb\n"
                            "<!-- memriver:end -->")


def test_apply_removal_dispatches_marker_block_by_kind():
    from memriver.install import RemovalOperation, Target

    target = Target(path=Path("/tmp/does-not-matter.md"), user_level=False,
                    rollback_instruction="remove the memriver block")
    op = RemovalOperation(id="cursor:instructions", target=target,
                          label="remove", kind="marker-block")
    merged = marker_block("notes\n", PROTOCOL_BLOCK)
    result = apply_removal(op, merged.rendered)
    assert result.rendered == "notes\n"


# --- Step 2: each harness's uninstall_operations() targets exactly what
#     operations() writes -------------------------------------------------


def _snapshot(target, text: str = "{}"):
    from memriver.install import Snapshot
    return Snapshot(target=target, text=text, mode=None)


HOME = Path("/home/user")


def test_claude_code_uninstall_operations_target_the_mcp_entry_and_both_hooks():
    config_target, settings_target = claude_code.targets(HOME, None)
    ops = claude_code.uninstall_operations(
        (_snapshot(config_target), _snapshot(settings_target)), {},
    )
    by_id = {op.id: op for op in ops}
    assert by_id["claude-code:mcp"].key_path == ("mcpServers", "memriver")
    assert by_id["claude-code:hooks-session-start"].key_path == ("hooks", "SessionStart")
    assert by_id["claude-code:hooks-session-start"].identity == SESSION_START_IDENTITY
    assert by_id["claude-code:hooks-stop"].key_path == ("hooks", "Stop")
    assert not any(op.id == "claude-code:native-memory" for op in ops)


def test_codex_uninstall_operations_target_the_mcp_table_and_both_hooks():
    config_target, hooks_target = codex.targets(HOME, None)
    ops = codex.uninstall_operations(
        (_snapshot(config_target, ""), _snapshot(hooks_target)), {},
    )
    by_id = {op.id: op for op in ops}
    assert by_id["codex:mcp"].kind == "toml-table"
    assert by_id["codex:mcp"].key_path == ("mcp_servers", "memriver")
    assert by_id["codex:hooks-session-start"].kind == "hook-array"
    assert not any(op.id == "codex:native-memory" for op in ops)


def test_cursor_uninstall_operations_target_the_mcp_entry_and_marker_block(tmp_path):
    mcp_target, instructions_target = cursor.targets(HOME, tmp_path)
    ops = cursor.uninstall_operations(
        (_snapshot(mcp_target), _snapshot(instructions_target, "")), {},
    )
    by_id = {op.id: op for op in ops}
    assert by_id["cursor:mcp"].key_path == ("mcpServers", "memriver")
    assert by_id["cursor:instructions"].kind == "marker-block"


def test_kiro_uninstall_operations_target_the_mcp_entry_and_marker_block(tmp_path):
    mcp_target, instructions_target = kiro.targets(HOME, tmp_path)
    ops = kiro.uninstall_operations(
        (_snapshot(mcp_target), _snapshot(instructions_target, "")), {},
    )
    by_id = {op.id: op for op in ops}
    assert by_id["kiro:mcp"].key_path == ("mcpServers", "memriver")
    assert by_id["kiro:instructions"].kind == "marker-block"


def test_claude_code_uninstall_notes_report_the_untouched_native_memory_setting():
    config_target, settings_target = claude_code.targets(HOME, None)
    notes = claude_code.uninstall_notes(
        (_snapshot(config_target),
         _snapshot(settings_target,
                   json.dumps({"env": {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}}))),
        {},
    )
    assert notes == (claude_code.NATIVE_MEMORY_LEFT_NOTE,)


def test_claude_code_uninstall_notes_are_silent_when_nothing_was_toggled():
    config_target, settings_target = claude_code.targets(HOME, None)
    notes = claude_code.uninstall_notes(
        (_snapshot(config_target), _snapshot(settings_target, "{}")), {},
    )
    assert notes == ()


def test_codex_uninstall_notes_report_the_untouched_native_memory_setting():
    config_target, hooks_target = codex.targets(HOME, None)
    notes = codex.uninstall_notes(
        (_snapshot(config_target, "[features]\nmemories = false\n"),
         _snapshot(hooks_target)),
        {},
    )
    assert notes == (CODEX_NATIVE_MEMORY_LEFT,)


# --- Step 3: run_uninstall over a pristine, never-installed home -------------


def test_uninstall_on_a_never_installed_home_reports_already_clean(tmp_path, home,
                                                                    project):
    before_tree = snapshot_tree(tmp_path)

    result = uninstall(ALL_HARNESSES, home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    assert "already clean" in result.stdout
    assert snapshot_tree(tmp_path) == before_tree
    assert result.replace.calls == []


# --- Step 4: install-then-uninstall round trips ------------------------------


def test_claude_code_round_trip_removes_memriver_and_keeps_foreign_content(home,
                                                                           project):
    write(home / ".claude.json", json.dumps({"apiKey": "secret"}))
    write(home / ".claude" / "settings.json",
          json.dumps({"env": {"OTHER": "1"}}))

    install(["claude-code"], home=home, cwd=project, yes=True,
           env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
    result = uninstall(["claude-code"], home=home, cwd=project, yes=True)

    config = json.loads((home / ".claude.json").read_text())
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert result.exit_code == 0
    # the memriver leaf is gone; the container install auto-created is left
    # behind empty rather than pruned away (P2-3)
    assert config == {"apiKey": "secret", "mcpServers": {}}
    assert settings["hooks"] == {"SessionStart": [], "Stop": []}
    assert settings["env"] == {"OTHER": "1"}  # foreign key survives untouched


def test_codex_round_trip_restores_the_exact_original_bytes(home, project):
    config = write(home / ".codex" / "config.toml", 'model = "gpt"\nforeign = "keep"\n')
    original = config.read_bytes()

    install(["codex"], home=home, cwd=project, yes=True)
    result = uninstall(["codex"], home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    assert config.read_bytes() == original
    assert json.loads((home / ".codex" / "hooks.json").read_text()) == {
        "hooks": {"SessionStart": [], "Stop": []}}


def test_cursor_round_trip_restores_the_exact_original_bytes(home, project):
    agents = write(project / "AGENTS.md", "# notes\n")
    original = agents.read_bytes()

    install(["cursor"], home=home, cwd=project, yes=True)
    result = uninstall(["cursor"], home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    assert agents.read_bytes() == original
    # cursor's mcp.json is a shared harness file, never deleted -- an empty
    # mcpServers container is the documented residue (P2-6)
    assert json.loads((home / ".cursor" / "mcp.json").read_text()) == {"mcpServers": {}}


def test_kiro_round_trip_deletes_the_memriver_owned_steering_file(home, project):
    install(["kiro"], home=home, cwd=project, yes=True)
    result = uninstall(["kiro"], home=home, cwd=project, yes=True)

    # .kiro/steering/memriver.md is entirely memriver's own file (install's
    # own rollback_instruction already says to remove it); emptied by the
    # marker-block removal, it is deleted outright rather than left as an
    # empty file (P2-6) -- unlike mcp.json, a harness file shared with
    # whatever else the user keeps in it
    steering = project / ".kiro" / "steering" / "memriver.md"
    assert result.exit_code == 0
    assert not steering.exists()
    assert json.loads((home / ".kiro" / "settings" / "mcp.json").read_text()) == {
        "mcpServers": {}}


def test_a_crlf_target_keeps_every_foreign_newline_byte_for_byte(home, project):
    agents = project / "AGENTS.md"
    agents.write_bytes(b"# foreign\r\nkeep\r\n")
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_bytes(b'model = "gpt"\r\nforeign = "keep"\r\n')
    original_agents, original_config = agents.read_bytes(), config.read_bytes()

    install(["codex", "cursor"], home=home, cwd=project, yes=True)
    result = uninstall(["codex", "cursor"], home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    assert agents.read_bytes() == original_agents
    assert config.read_bytes() == original_config


def test_installing_all_four_then_uninstalling_all_four_leaves_only_empty_shared_containers_and_deletes_the_kiro_file(
        home, project):
    """The documented uninstall contract (P2-6): shared harness files
    (claude.json/settings.json, codex's config/hooks, cursor's mcp.json and
    AGENTS.md) are never deleted -- an emptied memriver container is left
    behind as an empty container. kiro's steering file is the one exception:
    it is entirely memriver's own file, so emptying it deletes it outright.
    """
    install(ALL_HARNESSES, home=home, cwd=project, yes=True)

    result = uninstall(ALL_HARNESSES, home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    assert json.loads((home / ".claude.json").read_text()) == {"mcpServers": {}}
    assert json.loads((home / ".claude" / "settings.json").read_text())["hooks"] == {
        "SessionStart": [], "Stop": []}
    assert json.loads((home / ".codex" / "hooks.json").read_text()) == {
        "hooks": {"SessionStart": [], "Stop": []}}
    assert json.loads((home / ".cursor" / "mcp.json").read_text()) == {"mcpServers": {}}
    assert json.loads((home / ".kiro" / "settings" / "mcp.json").read_text()) == {
        "mcpServers": {}}
    assert PROTOCOL_BLOCK not in (project / "AGENTS.md").read_text()
    assert not (project / ".kiro" / "steering" / "memriver.md").exists()


# --- Step 5: partial state -- some entries already gone by hand -------------


def test_partial_state_removes_what_exists_and_reports_the_rest_clean(home, project):
    install(["claude-code"], home=home, cwd=project, yes=True,
           env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
    # the user (or another tool) already removed the MCP entry by hand
    write(home / ".claude.json", json.dumps({}))

    result = uninstall(["claude-code"], home=home, cwd=project, yes=True)

    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert result.exit_code == 0
    assert settings["hooks"] == {"SessionStart": [], "Stop": []}
    # only one change was left to make -- the hooks -- so only one prompt-worthy
    # summary line names a target, and the MCP file was never rewritten
    assert not any(p.name.startswith(".claude.json.memriver-backup-")
                   for p in backups(home))


# --- Step 5b: the completion report names any empty residue left behind -----


def _left_empty_line(stdout: str) -> str:
    return next((line for line in stdout.splitlines() if "left empty" in line), "")


def test_completion_report_names_a_shared_file_left_empty(home, project):
    install(["claude-code"], home=home, cwd=project, yes=True)
    result = uninstall(["claude-code"], home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    assert "~/.claude.json" in _left_empty_line(result.stdout)


def test_completion_report_does_not_name_the_deleted_kiro_file_as_leftover(home,
                                                                           project):
    install(["kiro"], home=home, cwd=project, yes=True)

    result = uninstall(["kiro"], home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    assert "steering/memriver.md" not in _left_empty_line(result.stdout)
    assert "mcp.json" in _left_empty_line(result.stdout)


# --- Step 6: dry run ----------------------------------------------------------


def test_dry_run_shows_the_plan_and_writes_nothing(home, project, tmp_path):
    install(["claude-code"], home=home, cwd=project, yes=True)
    before_tree = snapshot_tree(tmp_path)

    result = uninstall(["claude-code"], home=home, cwd=project, yes=False,
                       dry_run=True, input_fn=refuse_to_read)

    assert result.exit_code == 0
    assert "remove memriver MCP server" in result.stdout
    assert "dry run: nothing was written" in result.stdout
    assert snapshot_tree(tmp_path) == before_tree
    assert backups(tmp_path) == []


# --- Step 7: confirmation -----------------------------------------------------


def test_declining_a_change_leaves_it_in_place_and_removes_the_rest(home, project):
    install(["claude-code"], home=home, cwd=project, yes=True)

    result = uninstall(["claude-code"], home=home, cwd=project, yes=False,
                       replies=["y", "n", "y"])

    config = json.loads((home / ".claude.json").read_text())
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert result.exit_code == 0
    assert "memriver" not in config.get("mcpServers", {})
    assert "SessionStart" in settings["hooks"]  # declined
    assert settings["hooks"]["Stop"] == []  # accepted, left as an empty array


def test_non_interactive_input_without_yes_fails_before_any_write(home, project,
                                                                   tmp_path):
    install(["claude-code"], home=home, cwd=project, yes=True)
    before_tree = snapshot_tree(tmp_path)

    result = uninstall(["claude-code"], home=home, cwd=project, yes=False,
                       input_fn=refuse_to_read)

    assert result.exit_code != 0
    assert "--yes" in result.stdout
    assert snapshot_tree(tmp_path) == before_tree


# --- Step 8: rollback ----------------------------------------------------------


def test_a_failed_apply_restores_every_touched_target_from_its_backup(home, project):
    install(["claude-code", "codex"], home=home, cwd=project, yes=True)
    claude_json = home / ".claude.json"
    settings_json = home / ".claude" / "settings.json"
    codex_toml = home / ".codex" / "config.toml"
    codex_hooks = home / ".codex" / "hooks.json"
    original = {p: p.read_bytes() for p in
               (claude_json, settings_json, codex_toml, codex_hooks)}
    # writes happen in harness order: claude-code's two targets, then codex's
    # two; fail on the third (codex's config.toml)
    replace = ReplaceSpy(fail_at={3})

    result = uninstall(["claude-code", "codex"], home=home, cwd=project, yes=True,
                       replace=replace)

    assert result.exit_code != 0
    for path, data in original.items():
        assert path.read_bytes() == data
    assert "restored" in result.stdout
    assert "backups were kept" in result.stdout


# --- Step 9: CLI wiring --------------------------------------------------------


def test_cli_parses_uninstall_with_its_defaults():
    from memriver.cli import _build_parser

    args = _build_parser().parse_args(["uninstall"])

    assert args.command == "uninstall"
    assert args.harness is None
    assert args.all is False
    assert args.yes is False
    assert args.dry_run is False
    assert args.purge_data is False
    assert args.clean_uv_cache is False
    assert args.root is None


def test_cli_parses_every_uninstall_flag():
    from memriver.cli import _build_parser

    args = _build_parser().parse_args([
        "uninstall", "--harness", "codex", "--yes", "--dry-run", "--purge-data",
        "--clean-uv-cache", "--root", "/tmp/store",
    ])

    assert args.harness == "codex"
    assert args.yes and args.dry_run and args.purge_data and args.clean_uv_cache
    assert args.root == Path("/tmp/store")


def test_cli_rejects_harness_and_all_together_for_uninstall():
    from memriver.cli import _build_parser

    with pytest.raises(SystemExit):
        _build_parser().parse_args(["uninstall", "--harness", "codex", "--all"])


# --- Step 10: memriver.uninstall's own orchestration -------------------------


def test_purge_data_without_the_flag_leaves_the_store_untouched(home, project,
                                                                 tmp_path):
    root = tmp_path / "agent-memory"
    root.mkdir()
    (root / "marker.txt").write_text("data")

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=False, env={"MEMRIVER_ROOT": str(root)})

    assert result.exit_code == 0
    assert root.exists()
    assert "memory storage root" not in result.stdout


def test_purge_data_with_yes_removes_the_resolved_root(home, project, tmp_path):
    root = tmp_path / "agent-memory"
    root.mkdir()
    (root / "marker.txt").write_text("data")

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=True, env={"MEMRIVER_ROOT": str(root)})

    assert result.exit_code == 0
    assert str(root) in result.stdout
    assert not root.exists()


def test_purge_data_declined_leaves_the_store_in_place(home, project, tmp_path):
    root = tmp_path / "agent-memory"
    root.mkdir()

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=False,
                            purge_data=True, replies=["n"],
                            env={"MEMRIVER_ROOT": str(root)})

    assert result.exit_code == 0
    assert root.exists()


def test_purge_data_falls_back_to_the_default_root_under_the_injected_home(
        home, project, tmp_path):
    # the injected `home` seam, not the real process home -- see
    # test_purge_data_uses_the_injected_env_not_the_process_environment for the
    # matching MEMRIVER_ROOT case. env is left at its default {}, so there is
    # no MEMRIVER_ROOT to fall back from.
    default_root = home / "agent-memory"
    default_root.mkdir()

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=True)

    assert result.exit_code == 0
    assert not default_root.exists()


def test_purge_data_uses_the_injected_env_not_the_process_environment(
        home, project, tmp_path, monkeypatch):
    fake_root = tmp_path / "fake-root"
    fake_root.mkdir()
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    monkeypatch.setenv("MEMRIVER_ROOT", str(real_root))

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=True, env={"MEMRIVER_ROOT": str(fake_root)})

    assert result.exit_code == 0
    assert not fake_root.exists()
    assert real_root.exists()


def test_purge_data_root_flag_wins_over_the_injected_env(home, project, tmp_path):
    flagged_root = tmp_path / "flagged-root"
    flagged_root.mkdir()
    env_root = tmp_path / "env-root"
    env_root.mkdir()

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=True, root=flagged_root,
                            env={"MEMRIVER_ROOT": str(env_root)})

    assert result.exit_code == 0
    assert not flagged_root.exists()
    assert env_root.exists()


def test_purge_data_refuses_a_symlinked_root(home, project, tmp_path):
    real = tmp_path / "real-store"
    real.mkdir()
    link = tmp_path / "linked-store"
    link.symlink_to(real, target_is_directory=True)

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=True, env={"MEMRIVER_ROOT": str(link)})

    assert result.exit_code != 0
    assert "symlink" in result.stdout
    assert real.exists() and link.is_symlink()


def test_purge_data_on_a_missing_root_reports_nothing_to_remove(home, project,
                                                                 tmp_path):
    root = tmp_path / "never-created"

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=True, env={"MEMRIVER_ROOT": str(root)})

    assert result.exit_code == 0
    assert "no data to remove" in result.stdout


def test_purge_data_refuses_a_regular_file_root(home, project, tmp_path):
    root = tmp_path / "agent-memory"
    root.write_text("not a directory")

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=True, env={"MEMRIVER_ROOT": str(root)})

    assert result.exit_code != 0
    assert "is not a directory; nothing was removed" in result.stdout
    assert root.is_file()  # untouched


def test_purge_data_reports_a_partial_removal_when_rmtree_fails(home, project,
                                                                 tmp_path,
                                                                 monkeypatch):
    root = tmp_path / "agent-memory"
    root.mkdir()
    (root / "marker.txt").write_text("data")
    error = OSError()
    error.strerror = "Permission denied"

    def broken_rmtree(path):
        raise error

    monkeypatch.setattr("memriver.uninstall.shutil.rmtree", broken_rmtree)

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=True, env={"MEMRIVER_ROOT": str(root)})

    assert result.exit_code != 0
    assert "was only partly removed (Permission denied)" in result.stdout
    assert "harness configuration above was removed successfully" in result.stdout
    assert "Delete the remaining directory by hand" in result.stdout


def test_dry_run_never_purges_even_with_yes(home, project, tmp_path):
    root = tmp_path / "agent-memory"
    root.mkdir()
    install(["claude-code"], home=home, cwd=project, yes=True)

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            dry_run=True, purge_data=True,
                            env={"MEMRIVER_ROOT": str(root)})

    assert result.exit_code == 0
    assert root.exists()


def test_clean_uv_cache_invokes_uv_with_the_exact_arguments(home, project,
                                                             monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("memriver.uninstall.subprocess.run", fake_run)

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            clean_uv_cache=True)

    assert result.exit_code == 0
    assert calls == [
        ["uv", "cache", "clean", "memriver"],
        ["uv", "cache", "clean", "memriver-core"],
    ]


def test_clean_uv_cache_never_runs_under_dry_run(home, project, monkeypatch):
    calls = []
    monkeypatch.setattr("memriver.uninstall.subprocess.run",
                        lambda *a, **k: calls.append(a))

    full_uninstall(["claude-code"], home=home, cwd=project, yes=True, dry_run=True,
                   clean_uv_cache=True)

    assert calls == []


def test_missing_uv_warns_but_keeps_the_exit_code_at_zero(home, project, monkeypatch):
    def fake_run(args, **kwargs):
        raise FileNotFoundError("uv")

    monkeypatch.setattr("memriver.uninstall.subprocess.run", fake_run)

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            clean_uv_cache=True)

    assert result.exit_code == 0
    assert "uv was not found on PATH" in result.stdout
    assert "uv cache clean memriver" in result.stdout


def test_a_wedged_uv_times_out_and_warns_but_keeps_the_exit_code_at_zero(
        home, project, monkeypatch):
    def fake_run(args, **kwargs):
        assert "timeout" in kwargs
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs["timeout"])

    monkeypatch.setattr("memriver.uninstall.subprocess.run", fake_run)

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            clean_uv_cache=True)

    assert result.exit_code == 0
    assert "timed out" in result.stdout
    assert "uv cache clean memriver" in result.stdout


def test_a_nonzero_uv_exit_warns_but_keeps_the_exit_code_at_zero(home, project,
                                                                 monkeypatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")

    monkeypatch.setattr("memriver.uninstall.subprocess.run", fake_run)

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            clean_uv_cache=True)

    assert result.exit_code == 0
    assert "failed" in result.stdout


def test_a_config_removal_failure_skips_purge_and_uv_cache_entirely(home, project,
                                                                    monkeypatch):
    write(home / ".claude.json", "{ not json")
    calls = []
    monkeypatch.setattr("memriver.uninstall.subprocess.run",
                        lambda *a, **k: calls.append(a))

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=True, clean_uv_cache=True)

    assert result.exit_code != 0
    assert calls == []
    assert "memory storage root" not in result.stdout


# --- Step 12: shared planning-failure text does not hardcode "install" ------


def test_broken_markers_uninstall_planning_failure_is_command_neutral(home, project):
    write(project / "AGENTS.md",
          "<!-- memriver:begin -->\nold\n<!-- memriver:begin -->\nmore\n")

    result = uninstall(["cursor"], home=home, cwd=project, yes=True)

    assert result.exit_code != 0
    assert "install again" not in result.stdout


def test_symlinked_target_uninstall_planning_failure_is_command_neutral(home, project):
    outside = write(project / "outside.json", json.dumps({"mcpServers": {}}))
    (home / ".claude.json").symlink_to(outside)

    result = uninstall(["claude-code"], home=home, cwd=project, yes=True)

    assert result.exit_code != 0
    assert "install again" not in result.stdout


def test_undecodable_target_uninstall_planning_failure_is_command_neutral(home,
                                                                          project):
    (home / ".claude.json").write_bytes(b"\xff")

    result = uninstall(["claude-code"], home=home, cwd=project, yes=True)

    assert result.exit_code != 0
    assert "install again" not in result.stdout


def test_duplicate_json_keys_uninstall_planning_failure_is_command_neutral(home,
                                                                           project):
    write(home / ".claude.json", '{"foreign": "first", "foreign": "second"}')

    result = uninstall(["claude-code"], home=home, cwd=project, yes=True)

    assert result.exit_code != 0
    assert "install again" not in result.stdout


# --- Step 13: a TOCTOU abort during uninstall names uninstall ---------------


def test_a_target_rewritten_between_planning_and_apply_aborts_uninstall_naming_uninstall(
        home, project):
    """The exact inverse of test_install.py's own
    test_a_target_rewritten_between_planning_and_apply_aborts_the_run: the
    abort text must say 're-run memriver uninstall', not 'install', when it is
    uninstall's own apply that hit the race."""
    install(["claude-code"], home=home, cwd=project, yes=True)
    settings = home / ".claude" / "settings.json"
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 3:  # the last claude-code uninstall confirmation
            current = json.loads(settings.read_text())
            current["addedByAnotherAgent"] = True
            settings.write_text(json.dumps(current))
        return "y"

    result = uninstall(["claude-code"], home=home, cwd=project, yes=False,
                       input_fn=answer)

    assert len(prompts) == 3
    assert result.exit_code == 1
    abort = next(line for line in result.stdout.splitlines()
                 if "file changed since planning" in line)
    assert abort.endswith("~/.claude/settings.json: file changed since planning; "
                          "nothing further was written -- re-run memriver uninstall")


# --- Step 11: the takeover notice is never used by a plain removal ----------


def test_a_removal_summary_never_prints_a_takeover_line(home, project):
    install(["claude-code"], home=home, cwd=project, yes=True)

    result = uninstall(["claude-code"], home=home, cwd=project, yes=False,
                       replies=["y", "y", "y"])

    assert HARNESS_SETTING_TAKEOVER_NOTICE not in result.stdout
