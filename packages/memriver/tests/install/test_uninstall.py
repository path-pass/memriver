"""Contracts for ``memriver uninstall``: the exact inverse of install.

Three levels are pinned here, mirroring how install itself is tested across
``test_editors.py``, ``test_harnesses.py`` and ``test_install.py``.

*The four removers are pure text-in/text-out, like their merge counterparts.*
An absent entry is already clean, not an error; anything ambiguous still
raises ``PlanningError`` rather than guessing; and every byte outside the
entry being removed survives the removal, whoever wrote it:

- ``remove(merge(source)) == source`` byte for byte for TOML and marker-block
  files, over every EOF state a real file turns up with -- no trailing
  newline, one, a run of them, trailing spaces, CRLF -- and a table or block
  an earlier memriver merged still comes back out cleanly.
- JSON removals splice out the bytes of the one member (or one hook group)
  they take back. Escapes, number spellings, indentation, CRLF endings and
  trailing whitespace elsewhere in the file are the user's and are copied
  through untouched, whether install wrote them or the user reformatted the
  file afterwards. ``json_object_merge`` is the one editor that still
  re-renders a whole document, and that is install's side of the pair.

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
import os
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
from memriver_core.bootstrap import build_service
from memriver_core.settings import Settings


def _bind_new(store: Path, directory: Path, name: str) -> str:
    service = build_service(Settings(root=store), root=store)
    return service.init_project(name, service.plan_root(str(directory))).id


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
    out, err = io.StringIO(), io.StringIO()
    replace = ReplaceSpy()
    exit_code = run_install(
        harnesses, yes=yes, dry_run=False, home=home, cwd=cwd,
        env=env if env is not None else {}, input_fn=refuse_to_read, stdout=out,
        stderr=err, replace_file=replace,
    )
    assert exit_code == 0, err.getvalue()
    assert err.getvalue() == ""
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


# --- a JSON removal rewrites the removed member's bytes and nothing else -----

# CRLF endings, tab indentation, an escaped character, a number spelling and
# trailing EOF whitespace -- everything a hand-formatted config carries that a
# canonical re-render does not reproduce
FOREIGN_JSON = (
    '{\r\n\t"escaped": "\\u0061",\r\n\t"number": 1.00,\r\n'
    '\t"deep": {"list": [1, 2]}\r\n}\r\n \t'
)

# the same file with memriver's entry in it, formatted the way its owner keeps
# it rather than the way any renderer would: install is not the last thing that
# writes these files, and uninstall meets whatever the user left behind
HAND_FORMATTED_WITH_MEMRIVER = (
    '{\r\n'
    '\t"escaped": "\\u0061",\r\n'
    '\t"number": 1.00,\r\n'
    '\t"mcpServers": {\r\n'
    '\t\t"other": {"command": "x"},\r\n'
    '\t\t"memriver": {"command": "uvx", "args": ["memriver"]}\r\n'
    '\t}\r\n'
    '}\r\n \t'
)


def test_a_json_removal_keeps_every_byte_outside_the_removed_member():
    """Escapes, number spellings, tabs, CRLF endings and the whitespace past
    the closing brace are the user's bytes, not memriver's to normalize: the
    removal is a splice of the one member's span, so everything outside it
    comes through identical."""
    removed = json_object_remove(HAND_FORMATTED_WITH_MEMRIVER,
                                 ("mcpServers", "memriver"))

    assert removed.rendered == (
        '{\r\n'
        '\t"escaped": "\\u0061",\r\n'
        '\t"number": 1.00,\r\n'
        '\t"mcpServers": {\r\n'
        '\t\t"other": {"command": "x"}\r\n'
        '\t}\r\n'
        '}\r\n \t'
    )
    assert removed.changed and not removed.takeover


def test_a_json_removal_of_a_leading_member_takes_its_comma_with_it():
    """The comma the removed member owns goes with it; the space that separated
    it from the next member is that member's own leading whitespace and stays."""
    source = ('{"mcpServers": {"memriver": {"command": "uvx"}, '
              '"other": {"command": "x"}}}')

    removed = json_object_remove(source, ("mcpServers", "memriver"))

    assert removed.rendered == '{"mcpServers": { "other": {"command": "x"}}}'


def test_a_json_removal_leaves_the_next_members_own_indentation_alone():
    """The cut ends at the removed member's own comma. Taking the next member's
    leading whitespace instead would re-indent a foreign member to whatever
    memriver's entry happened to be indented by -- here, eight spaces to one."""
    source = '{\r\n "memriver": 1,\r\n        "foreign": 2\r\n}'

    removed = json_object_remove(source, ("memriver",))

    assert removed.rendered == '{\r\n        "foreign": 2\r\n}'


def test_a_json_removal_of_a_last_member_keeps_the_whitespace_before_the_comma():
    """That whitespace sits between the previous member's value and the
    separating comma: it is the foreign member's formatting, not the removed
    member's, so the cut starts at the comma itself."""
    source = '{"foreign": 1   , "memriver": 2}'

    removed = json_object_remove(source, ("memriver",))

    assert removed.rendered == '{"foreign": 1   }'


def test_a_hook_removal_leaves_the_next_groups_own_indentation_alone():
    source = (
        '{"hooks": {"SessionStart": [\r\n'
        '  {"hooks": [{"type": "command", "command": "uvx memriver hook '
        'session-start --harness claude-code"}]},\r\n'
        '\t\t\t\t{"hooks": [{"type": "command", "command": "/opt/audit/run"}]}\r\n'
        ']}}'
    )

    removed = hook_array_identity_remove(source, "SessionStart",
                                         SESSION_START_IDENTITY)

    assert removed.rendered == (
        '{"hooks": {"SessionStart": [\r\n'
        '\t\t\t\t{"hooks": [{"type": "command", "command": "/opt/audit/run"}]}\r\n'
        ']}}'
    )


def test_a_hook_removal_of_a_last_group_keeps_the_whitespace_before_the_comma():
    source = (
        '{"hooks": {"SessionStart": ['
        '{"hooks": [{"type": "command", "command": "/opt/audit/run"}]}   , '
        '{"hooks": [{"type": "command", "command": "uvx memriver hook '
        'session-start --harness claude-code"}]}]}}'
    )

    removed = hook_array_identity_remove(source, "SessionStart",
                                         SESSION_START_IDENTITY)

    assert removed.rendered == (
        '{"hooks": {"SessionStart": ['
        '{"hooks": [{"type": "command", "command": "/opt/audit/run"}]}   ]}}'
    )


def test_a_json_removal_of_the_last_member_empties_the_container_in_place():
    """Nothing is pruned -- the container install put the entry in stays -- and
    the whitespace that only existed to hold the removed member goes with it."""
    source = '{\r\n\t"mcpServers": {\r\n\t\t"memriver": {"command": "uvx"}\r\n\t}\r\n}\r\n'

    removed = json_object_remove(source, ("mcpServers", "memriver"))

    assert removed.rendered == '{\r\n\t"mcpServers": {}\r\n}\r\n'


def test_a_json_removal_reads_braces_and_quotes_inside_strings_as_text():
    source = ('{"note": "}{ \\" \\\\ not structure", '
              '"mcpServers": {"memriver": {"command": "uvx"}}}')

    removed = json_object_remove(source, ("mcpServers", "memriver"))

    assert removed.rendered == ('{"note": "}{ \\" \\\\ not structure", '
                                '"mcpServers": {}}')


def test_a_hook_removal_keeps_every_byte_outside_the_removed_group():
    source = (
        '{\r\n'
        '\t"hooks": {\r\n'
        '\t\t"SessionStart": [\r\n'
        '\t\t\t{"hooks": [{"type": "command", "command": "/opt/audit/run"}]},\r\n'
        '\t\t\t{"hooks": [{"type": "command", "command": "uvx memriver hook '
        'session-start --harness claude-code"}]}\r\n'
        '\t\t]\r\n'
        '\t},\r\n'
        '\t"escaped": "\\u0061"\r\n'
        '}\r\n'
    )

    removed = hook_array_identity_remove(source, "SessionStart",
                                         SESSION_START_IDENTITY)

    assert removed.rendered == (
        '{\r\n'
        '\t"hooks": {\r\n'
        '\t\t"SessionStart": [\r\n'
        '\t\t\t{"hooks": [{"type": "command", "command": "/opt/audit/run"}]}\r\n'
        '\t\t]\r\n'
        '\t},\r\n'
        '\t"escaped": "\\u0061"\r\n'
        '}\r\n'
    )


def test_a_hook_removal_of_the_only_group_empties_the_event_array_in_place():
    source = ('{\r\n\t"hooks": {\r\n\t\t"SessionStart": [\r\n\t\t\t'
              '{"hooks": [{"type": "command", "command": "uvx memriver hook '
              'session-start --harness codex"}]}\r\n\t\t]\r\n\t}\r\n}\r\n')

    removed = hook_array_identity_remove(source, "SessionStart",
                                         SESSION_START_IDENTITY)

    assert removed.rendered == ('{\r\n\t"hooks": {\r\n\t\t"SessionStart": []'
                                '\r\n\t}\r\n}\r\n')


def test_a_json_removal_that_changes_nothing_returns_the_foreign_bytes_intact():
    """The one byte-level guarantee a JSON target really has: a document with
    no memriver entry in it is handed back exactly as it was read."""
    assert json_object_remove(
        FOREIGN_JSON, ("mcpServers", "memriver")).rendered == FOREIGN_JSON
    assert hook_array_identity_remove(
        FOREIGN_JSON, "SessionStart", SESSION_START_IDENTITY).rendered == FOREIGN_JSON


def test_a_whole_uninstall_run_keeps_every_byte_outside_the_removed_member(home,
                                                                           project):
    """The same guarantee through the real write transaction, not just the
    editor: a config the user formatted themselves comes back with memriver's
    member gone and every other byte -- escapes, number spellings, tabs, CRLF,
    the whitespace past the closing brace -- exactly as it was."""
    config = write(home / ".claude.json", HAND_FORMATTED_WITH_MEMRIVER)

    result = uninstall(["claude-code"], home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    assert config.read_bytes().decode("utf-8") == (
        '{\r\n'
        '\t"escaped": "\\u0061",\r\n'
        '\t"number": 1.00,\r\n'
        '\t"mcpServers": {\r\n'
        '\t\t"other": {"command": "x"}\r\n'
        '\t}\r\n'
        '}\r\n \t'
    )


# --- removal is the byte-exact inverse of the merge, over every EOF state ----

# every trailing shape a real config file turns up with: none, one, two and
# three newlines, trailing spaces, and the CRLF spellings of the same
EOF_STATES = ["", "\n", "\n\n", "\n\n\n", "  ", "\r\n", "\r\n\r\n"]


@pytest.mark.parametrize("tail", EOF_STATES)
def test_toml_table_remove_inverts_the_merge_byte_for_byte(tail):
    from memriver.install import toml_roundtrip

    source = 'model = "gpt"' + tail
    merged = toml_roundtrip(source, ("mcp_servers", "memriver"),
                            {"command": "uvx", "args": ["memriver"]})
    result = toml_table_remove(merged.rendered, ("mcp_servers", "memriver"))

    assert result.rendered == source


@pytest.mark.parametrize("tail", EOF_STATES)
def test_marker_block_remove_inverts_the_merge_byte_for_byte(tail):
    source = "notes" + tail
    merged = marker_block(source, PROTOCOL_BLOCK)
    result = marker_block_remove(merged.rendered)

    assert result.rendered == source


@pytest.mark.parametrize("source", ["", "\n", "\n\n"])
def test_marker_block_remove_inverts_the_merge_on_an_empty_or_blank_file(source):
    merged = marker_block(source, PROTOCOL_BLOCK)
    result = marker_block_remove(merged.rendered)

    assert result.rendered == source


def test_marker_block_remove_keeps_the_user_blank_runs_around_the_block():
    """Only the single newline the merge puts on each side of the block is
    the merge's to take back; every other blank line is the user's."""
    source = ("# Project\n\n\n\n"
              "<!-- memriver:begin -->\nstale\n<!-- memriver:end -->"
              "\n\n\n\ntail\n")

    result = marker_block_remove(source)

    assert result.rendered == "# Project\n\n\n\n\n\ntail\n"


def test_toml_table_remove_keeps_a_user_blank_run_above_the_removed_table():
    source = ('model = "gpt"\n\n\n\n[mcp_servers.memriver]\ncommand = "uvx"\n')

    result = toml_table_remove(source, ("mcp_servers", "memriver"))

    assert result.rendered == 'model = "gpt"\n\n\n'


# --- a separator is a newline sequence, never a single character -------------


def test_toml_table_remove_takes_back_a_crlf_separator_whole():
    """A table separated from the rest of the file by CRLF: taking back the
    LF alone would leave the carriage return behind as an orphan."""
    source = 'model = "gpt"\r\n\r\n[mcp_servers.memriver]\r\ncommand = "uvx"\r\n'

    result = toml_table_remove(source, ("mcp_servers", "memriver"))

    assert result.rendered == 'model = "gpt"\r\n'


def test_toml_table_remove_leaves_no_orphan_carriage_return_in_a_crlf_blank_run():
    source = ('model = "gpt"\r\n\r\n\r\n[mcp_servers.memriver]\r\n'
              'command = "uvx"\r\n')

    result = toml_table_remove(source, ("mcp_servers", "memriver"))

    assert result.rendered == 'model = "gpt"\r\n\r\n'


def test_marker_block_remove_takes_back_a_crlf_separator_whole():
    """The bytes outside a CRLF-separated block are the user's, down to the
    carriage returns; only the one newline sequence on each side is the
    block's own."""
    source = ("head\r\n\r\n<!-- memriver:begin -->\r\nblock\r\n"
              "<!-- memriver:end -->\r\n\r\ntail\r\n")

    result = marker_block_remove(source)

    assert result.rendered == "head\r\n\r\ntail\r\n"


def test_marker_block_remove_keeps_a_crlf_blank_run_around_the_block():
    source = ("head\r\n\r\n\r\n<!-- memriver:begin -->\r\nblock\r\n"
              "<!-- memriver:end -->\r\n\r\n\r\ntail\r\n")

    result = marker_block_remove(source)

    assert result.rendered == "head\r\n\r\n\r\n\r\ntail\r\n"


# --- a removal never fuses two lines that were separate -----------------------

# The merge appends a newline on each side of a block it adds, but it replaces
# an existing marker pair in place without adding any. A block a user pasted
# between two of their own lines therefore has exactly one newline on each
# side, and both of them are line breaks the user's text needs. Reclaiming
# both would run the two lines together.


def test_marker_block_remove_keeps_the_one_line_break_between_the_users_lines():
    source = ("before\n<!-- memriver:begin -->\nblock\n"
              "<!-- memriver:end -->\nafter\n")

    result = marker_block_remove(source)

    assert result.rendered == "before\nafter\n"


def test_marker_block_remove_keeps_the_one_crlf_line_break_between_the_users_lines():
    source = ("before\r\n<!-- memriver:begin -->\r\nblock\r\n"
              "<!-- memriver:end -->\r\nafter\r\n")

    result = marker_block_remove(source)

    assert result.rendered == "before\r\nafter\r\n"


def test_marker_block_remove_keeps_the_line_break_in_front_of_a_block_that_ends_mid_line():
    """The same fusing hazard on the other side: nothing follows the end
    marker on its own line, so the newline in front of the block is the only
    break between the user's two lines and must stay."""
    source = "a\n<!-- memriver:begin -->\nblock\n<!-- memriver:end -->b\n"

    result = marker_block_remove(source)

    assert result.rendered == "a\nb\n"


def test_marker_block_remove_keeps_the_crlf_line_break_in_front_of_a_block_that_ends_mid_line():
    source = "a\r\n<!-- memriver:begin -->\r\nblock\r\n<!-- memriver:end -->b\r\n"

    result = marker_block_remove(source)

    assert result.rendered == "a\r\nb\r\n"


def test_marker_block_remove_takes_the_line_break_of_a_block_at_the_file_start():
    """With nothing in front of the block there is no line to fuse with, so
    the newline behind it is the block's own and comes off."""
    source = "<!-- memriver:begin -->\nblock\n<!-- memriver:end -->\nafter\n"

    result = marker_block_remove(source)

    assert result.rendered == "after\n"


# --- a store installed by an earlier memriver still uninstalls cleanly -------

# Both merges below reproduce what memriver wrote before the removers existed:
# the TOML one appended the table with tomlkit's own padding and no separator
# of its own, and the marker one normalized the file's tail to a single
# newline before adding a blank line. Uninstall meets those bytes on real
# disks, so it is pinned against them and not only against its own merge.


def a_previously_merged_toml(source: str) -> str:
    servers = tomlkit.table(True)
    entry = tomlkit.table()
    entry["command"] = "uvx"
    entry["args"] = ["memriver"]
    servers["memriver"] = entry
    document = tomlkit.parse(source)
    document["mcp_servers"] = servers
    return tomlkit.dumps(document)


def a_previously_merged_marker_block(source: str) -> str:
    separator = "\n\n" if source.strip() else ""
    block = ("<!-- memriver:begin -->\n" + PROTOCOL_BLOCK.strip()
             + "\n<!-- memriver:end -->")
    return source.rstrip("\n") + separator + block + "\n"


@pytest.mark.parametrize("tail", ["", "\n", "\r\n"])
def test_removing_a_previously_merged_toml_table_restores_the_original_bytes(tail):
    source = 'model = "gpt"' + tail

    result = toml_table_remove(a_previously_merged_toml(source),
                               ("mcp_servers", "memriver"))

    assert result.rendered == source


@pytest.mark.parametrize("tail,closest", [
    ("\n\n", "\n"), ("\n\n\n", "\n\n"), ("\r\n\r\n", "\r\n"),
    ("\r\n\r\n\r\n", "\r\n\r\n"),
])
def test_removing_a_previously_merged_toml_table_gives_back_one_separator(
        tail, closest):
    """The earlier merge padded up to a blank line and rendered ``'x\\n'`` and
    ``'x\\n\\n'`` identically, so which of the trailing newlines it contributed
    is unknowable from the bytes on disk. Removal takes back exactly one
    newline sequence -- the separator it can account for -- rather than
    guessing at the run, and never leaves half of a CRLF behind."""
    source = 'model = "gpt"' + tail

    result = toml_table_remove(a_previously_merged_toml(source),
                               ("mcp_servers", "memriver"))

    assert result.rendered == 'model = "gpt"' + closest


@pytest.mark.parametrize("tail", ["\n", "\r\n", "\r\n\r\n"])
def test_removing_a_previously_merged_marker_block_restores_the_original_bytes(tail):
    source = "notes" + tail

    result = marker_block_remove(a_previously_merged_marker_block(source))

    assert result.rendered == source


@pytest.mark.parametrize("tail,closest", [("", "\n"), ("\n\n", "\n")])
def test_removing_a_previously_merged_marker_block_gives_back_one_separator(
        tail, closest):
    """The earlier merge stripped the file's own trailing newlines before
    adding its blank line, so a file that ended with none and one that ended
    with two left identical bytes. Removal takes back the one newline sequence
    it owns; inventing or withholding a second one would be a guess."""
    source = "notes" + tail

    result = marker_block_remove(a_previously_merged_marker_block(source))

    assert result.rendered == "notes" + closest


# --- Step 2: each harness's uninstall_operations() targets exactly what
#     operations() writes -------------------------------------------------


def _snapshot(target, text: str = "{}"):
    from memriver.install import Snapshot
    return Snapshot(target=target, text=text, mode=None)


HOME = Path("/home/user")


def test_claude_code_uninstall_operations_target_the_mcp_entry_and_both_hooks():
    config_target, settings_target = claude_code.targets(HOME, None, "uninstall")
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
    config_target, hooks_target = codex.targets(HOME, None, "uninstall")
    ops = codex.uninstall_operations(
        (_snapshot(config_target, ""), _snapshot(hooks_target)), {},
    )
    by_id = {op.id: op for op in ops}
    assert by_id["codex:mcp"].kind == "toml-table"
    assert by_id["codex:mcp"].key_path == ("mcp_servers", "memriver")
    assert by_id["codex:hooks-session-start"].kind == "hook-array"
    assert not any(op.id == "codex:native-memory" for op in ops)


def test_cursor_uninstall_operations_target_the_mcp_entry_and_marker_block(tmp_path):
    mcp_target, instructions_target = cursor.targets(HOME, tmp_path, "uninstall")
    ops = cursor.uninstall_operations(
        (_snapshot(mcp_target), _snapshot(instructions_target, "")), {},
    )
    by_id = {op.id: op for op in ops}
    assert by_id["cursor:mcp"].key_path == ("mcpServers", "memriver")
    assert by_id["cursor:instructions"].kind == "marker-block"


def test_kiro_uninstall_operations_target_the_mcp_entry_and_marker_block(tmp_path):
    mcp_target, instructions_target = kiro.targets(HOME, tmp_path, "uninstall")
    ops = kiro.uninstall_operations(
        (_snapshot(mcp_target), _snapshot(instructions_target, "")), {},
    )
    by_id = {op.id: op for op in ops}
    assert by_id["kiro:mcp"].key_path == ("mcpServers", "memriver")
    assert by_id["kiro:instructions"].kind == "marker-block"


def test_claude_code_uninstall_notes_report_the_untouched_native_memory_setting():
    config_target, settings_target = claude_code.targets(HOME, None, "uninstall")
    notes = claude_code.uninstall_notes(
        (_snapshot(config_target),
         _snapshot(settings_target,
                   json.dumps({"env": {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}}))),
        {},
    )
    assert notes == (claude_code.NATIVE_MEMORY_LEFT_NOTE,)


def test_claude_code_uninstall_notes_are_silent_when_nothing_was_toggled():
    config_target, settings_target = claude_code.targets(HOME, None, "uninstall")
    notes = claude_code.uninstall_notes(
        (_snapshot(config_target), _snapshot(settings_target, "{}")), {},
    )
    assert notes == ()


def test_codex_uninstall_notes_report_the_untouched_native_memory_setting():
    config_target, hooks_target = codex.targets(HOME, None, "uninstall")
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


def test_rollback_of_a_deletion_does_not_overwrite_a_file_someone_recreated(
        home, project):
    """kiro's steering file is deleted outright once uninstall empties it
    (`delete_if_emptied`) -- write 2 of this run. If another process recreates
    that path before rollback undoes a later failure (codex's config.toml,
    write 3), restoring the backup over it would silently discard whatever
    that process wrote, with no way to recover it afterwards."""
    install(["kiro", "codex"], home=home, cwd=project, yes=True)
    mcp_json = home / ".kiro" / "settings" / "mcp.json"
    steering = project / ".kiro" / "steering" / "memriver.md"
    original_mcp = mcp_json.read_bytes()
    calls: list[int] = []

    def replace(source, destination) -> None:
        calls.append(len(calls) + 1)
        if calls[-1] == 3:
            # simulates another process recreating the steering file (deleted
            # by this run's own write 2) before codex's config.toml (write 3)
            # fails
            steering.write_text("someone recreated this\n", encoding="utf-8")
            raise OSError("injected replacement failure #3")
        os.replace(source, destination)

    result = uninstall(["kiro", "codex"], home=home, cwd=project, yes=True,
                       replace=replace)

    assert result.exit_code != 0
    assert steering.read_text() == "someone recreated this\n"
    assert mcp_json.read_bytes() == original_mcp
    assert "changed after this run wrote it" in result.stdout


def test_rollback_treats_an_incomplete_deletion_as_already_undone(
        home, project, monkeypatch):
    """The steering file's rollback record is written *before* the `unlink`
    that deletes it (see `_write_target`), so an `unlink` that raises (or an
    interrupt landing between the two) leaves the file exactly as it was --
    holding the same bytes the backup does. Comparing the current bytes only
    against `written=None` would call that a foreign change and report it;
    comparing against the backup's bytes first recognizes there is nothing
    left to undo, since restoring the backup over it would be a no-op."""
    install(["kiro"], home=home, cwd=project, yes=True)
    mcp_json = home / ".kiro" / "settings" / "mcp.json"
    steering = project / ".kiro" / "steering" / "memriver.md"
    original_mcp = mcp_json.read_bytes()
    original_steering = steering.read_bytes()
    real_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self == steering:
            raise OSError("injected unlink failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    result = uninstall(["kiro"], home=home, cwd=project, yes=True)

    assert result.exit_code != 0
    assert steering.read_bytes() == original_steering
    assert mcp_json.read_bytes() == original_mcp
    assert f"{steering} changed after this run wrote it" not in result.stdout
    assert "restored" in result.stdout  # mcp.json still gets rolled back


def test_an_interrupt_the_instant_the_steering_file_is_unlinked_still_rolls_back(
        home, project, monkeypatch):
    """The steering file is deleted outright rather than rewritten. Its
    rollback record has to exist before the deletion does, or an interrupt
    landing between the two leaves a file no rollback knows to restore."""
    install(["kiro"], home=home, cwd=project, yes=True)
    steering = project / ".kiro" / "steering" / "memriver.md"
    original = steering.read_bytes()
    real_unlink = Path.unlink

    def unlink_then_interrupt(self, *args, **kwargs):
        real_unlink(self, *args, **kwargs)
        if self == steering:
            raise KeyboardInterrupt

    monkeypatch.setattr(Path, "unlink", unlink_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        uninstall(["kiro"], home=home, cwd=project, yes=True)

    monkeypatch.undo()
    assert steering.read_bytes() == original


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


def test_purge_data_reports_a_partial_removal_when_the_walk_fails(home, project,
                                                                   tmp_path,
                                                                   monkeypatch):
    """The wording is only accurate if the store really is half gone, so the
    injected failure deletes part of the tree before it raises -- exactly the
    state a real mid-walk ``PermissionError`` leaves behind."""
    import shutil

    delete_tree = shutil.rmtree
    root = tmp_path / "agent-memory"
    (root / "sessions").mkdir(parents=True)
    (root / "sessions" / "one.json").write_text("gone")
    (root / "index.db").write_text("still here")

    def half_removing_walk(fd, **kwargs):
        delete_tree(root / "sessions")
        error = OSError()
        error.strerror = "Permission denied"
        raise error

    monkeypatch.setattr("memriver_core.repository.directories._empty_directory", half_removing_walk)

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=True, env={"MEMRIVER_ROOT": str(root)})

    assert result.exit_code != 0
    assert not (root / "sessions").exists()  # genuinely half removed
    assert (root / "index.db").read_text() == "still here"
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


# --- completion notes never turn a committed removal into a traceback --------


def test_a_scalar_env_container_still_lets_the_claude_removal_report_success(
        home, project):
    """The notes run after the write transaction has committed. A shape the
    write phase accepted -- `env` holding a string rather than an object --
    must produce a conservative note or none, never an exception on top of an
    already-removed configuration."""
    write(home / ".claude.json",
          json.dumps({"mcpServers": {"memriver": {"command": "uvx"}}}))
    settings = write(home / ".claude" / "settings.json", json.dumps({
        "env": "foreign-scalar",
        "hooks": {"SessionStart": [
            hook_group("uvx memriver hook session-start --harness claude-code")]},
    }))

    result = uninstall(["claude-code"], home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    assert "uninstalled:" in result.stdout
    assert json.loads(settings.read_text())["hooks"]["SessionStart"] == []
    assert json.loads(settings.read_text())["env"] == "foreign-scalar"


def test_a_scalar_features_container_still_lets_the_codex_removal_report_success(
        home, project):
    config = write(home / ".codex" / "config.toml",
                   'features = "foreign-scalar"\n\n'
                   '[mcp_servers.memriver]\ncommand = "uvx"\n')

    result = uninstall(["codex"], home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    assert "uninstalled:" in result.stdout
    assert config.read_text() == 'features = "foreign-scalar"\n'


# --- the purge target is canonicalized, shown, and bounded -------------------


def forbid_any_deletion(monkeypatch) -> None:
    """Make any deletion walk fail the test outright, whatever it is handed."""
    def refuse(fd):
        raise AssertionError("a deletion walk was started on a protected target")

    monkeypatch.setattr("memriver_core.repository.directories._empty_directory", refuse)


def swap_during_the_walk(monkeypatch, swap, *, at: int = 1) -> None:
    """Run ``swap`` just before the ``at``-th directory of the deletion walk is
    entered, then let the real walk run.

    ``at=1`` is the confirmed root itself -- the moment every name-based check
    has already passed -- and ``at=2`` is its first subdirectory, which is as
    mid-walk as an injection gets.
    """
    from memriver_core.repository import directories

    walk = directories._empty_directory
    entered: list[int] = []

    def swapping(fd: int, **kwargs) -> None:
        entered.append(fd)
        if len(entered) == at:
            swap()
        return walk(fd, **kwargs)

    monkeypatch.setattr("memriver_core.repository.directories._empty_directory", swapping)


def storage_root_line(stdout: str) -> str:
    return next(line for line in stdout.splitlines()
                if line.startswith("memory storage root: "))


@pytest.mark.parametrize("spelling", ["/", ".", "..", "cwd", "home"])
def test_purge_data_refuses_an_overbroad_target_before_it_deletes_anything(
        home, project, monkeypatch, spelling):
    """`/`, the injected home, the current directory and any ancestor of it are
    never a memriver store, however they are spelled: an empty or mistyped
    --root/MEMRIVER_ROOT must not escalate into deleting the whole tree."""
    forbid_any_deletion(monkeypatch)
    keep = write(home / "personal.txt", "mine")
    target = {"cwd": project, "home": home}.get(spelling, Path(spelling))

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=True, root=target)

    assert result.exit_code == 1
    assert "Traceback" not in result.stdout
    assert "too broad a target" in result.stdout
    assert keep.read_text() == "mine"
    assert home.is_dir() and project.is_dir()


def test_purge_data_refuses_a_dot_dot_chain_that_canonicalizes_onto_the_home(
        home, project, monkeypatch):
    forbid_any_deletion(monkeypatch)
    spelled = home / "agent-memory" / ".." / ".." / home.name

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=True, root=spelled)

    assert result.exit_code == 1
    assert storage_root_line(result.stdout) == f"memory storage root: {home}"
    assert home.is_dir()


def test_purge_data_refuses_a_relative_target_resolved_against_the_injected_cwd(
        home, project, monkeypatch):
    forbid_any_deletion(monkeypatch)

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=True, root=Path("."))

    assert result.exit_code == 1
    assert storage_root_line(result.stdout) == f"memory storage root: {project}"
    assert project.is_dir()


def test_purge_data_shows_the_canonical_target_when_a_parent_is_a_symlink(
        home, project, tmp_path, monkeypatch):
    """A symlinked parent redirects the deletion somewhere the given spelling
    never named; the plan and the report must show where it actually lands."""
    forbid_any_deletion(monkeypatch)
    outside = tmp_path / "outside"
    (outside / "store").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=False,
                            purge_data=True, replies=["n"],
                            root=alias / "store")

    assert result.exit_code == 0
    assert storage_root_line(result.stdout) == (
        f"memory storage root: {outside / 'store'}")
    assert result.answers.prompts[-1] == (
        f"remove the entire memory store at {outside / 'store'}? [y/N] ")
    assert (outside / "store").is_dir()


def test_purge_data_refuses_a_symlinked_parent_that_redirects_onto_the_home(
        home, project, tmp_path, monkeypatch):
    """Leaf-only symlink checking passed this: `alias/store` is not itself a
    symlink, yet it canonicalizes onto the home directory."""
    forbid_any_deletion(monkeypatch)
    keep = write(home / "personal.txt", "mine")
    alias = tmp_path / "alias"
    alias.symlink_to(home.parent, target_is_directory=True)

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=True, root=alias / home.name)

    assert result.exit_code == 1
    assert storage_root_line(result.stdout) == f"memory storage root: {home}"
    assert keep.read_text() == "mine"


def test_purge_data_refuses_a_leaf_swapped_for_a_symlink_during_the_confirmation(
        home, project, tmp_path, monkeypatch):
    """The confirmation window is long enough for the target to be swapped for
    a link onto the home directory; the object that was confirmed is gone, so
    nothing is deleted."""
    forbid_any_deletion(monkeypatch)
    root = tmp_path / "agent-memory"
    root.mkdir()

    def answer(prompt: str) -> str:
        root.rmdir()
        root.symlink_to(home, target_is_directory=True)
        return "y"

    result = full_uninstall(["claude-code"], home=home, cwd=project,
                            yes=False, purge_data=True, input_fn=answer,
                            env={"MEMRIVER_ROOT": str(root)})

    assert result.exit_code == 1
    assert "Traceback" not in result.stdout
    assert "nothing was removed" in result.stdout
    assert home.is_dir()


def test_purge_data_refuses_a_replacement_directory_at_the_confirmed_path(
        home, project, tmp_path):
    """A same-path swap keeps every string equal: the shown path still resolves
    to itself, yet the directory standing there is a different object than the
    one the user confirmed. Deleting it would destroy a tree nobody agreed to."""
    root = tmp_path / "agent-memory"
    root.mkdir()
    (root / "confirmed.txt").write_text("the object the user saw")
    moved = tmp_path / "moved-away"

    def answer(prompt: str) -> str:
        root.rename(moved)
        root.mkdir()
        (root / "replacement.txt").write_text("never confirmed")
        return "y"

    result = full_uninstall(["claude-code"], home=home, cwd=project,
                            yes=False, purge_data=True, input_fn=answer,
                            env={"MEMRIVER_ROOT": str(root)})

    assert result.exit_code == 1
    assert "Traceback" not in result.stdout
    assert "nothing was removed" in result.stdout
    assert (root / "replacement.txt").read_text() == "never confirmed"
    assert (moved / "confirmed.txt").exists()


def test_purge_data_deletes_the_confirmed_directory_when_a_parent_is_swapped_last(
        home, project, tmp_path, monkeypatch):
    """A parent component replaced mid-walk redirects the *path*, not the
    directories the deletion is anchored to: the victim tree the swapped parent
    points at is never walked."""
    parent = tmp_path / "parent"
    store = parent / "agent-memory"
    (store / "sessions").mkdir(parents=True)
    (store / "sessions" / "one.json").write_text("memriver's own")
    victim_parent = tmp_path / "victim-parent"
    victim = victim_parent / "agent-memory"
    (victim / "sessions").mkdir(parents=True)
    (victim / "precious.txt").write_text("someone else's")

    def swap_the_parent() -> None:
        # the race the path-string check cannot see: with the walk already
        # under way, `parent` becomes a link onto a tree memriver never named
        parent.rename(tmp_path / "parent-moved")
        (tmp_path / "parent").symlink_to(victim_parent, target_is_directory=True)

    swap_during_the_walk(monkeypatch, swap_the_parent, at=2)

    result = full_uninstall(["claude-code"], home=home, cwd=project,
                            yes=True, purge_data=True,
                            env={"MEMRIVER_ROOT": str(store)})

    assert (victim / "precious.txt").read_text() == "someone else's"
    assert (victim / "sessions").is_dir()
    assert not (tmp_path / "parent-moved" / "agent-memory").exists()
    assert result.exit_code == 0


def test_purge_data_leaves_a_replacement_swapped_in_after_the_confirmed_open(
        home, project, tmp_path, monkeypatch):
    """The last window the path-anchored walk left open: the leaf is renamed
    away and a fresh directory takes its name once the identity check has
    already passed. The walk holds a descriptor on the confirmed object, so it
    empties that -- it never so much as opens the replacement -- and the name is
    checked once more before the emptied root itself is detached."""
    root = tmp_path / "agent-memory"
    (root / "sessions").mkdir(parents=True)
    (root / "sessions" / "one.json").write_text("the object the user saw")
    moved = tmp_path / "moved-away"

    def swap_the_leaf() -> None:
        root.rename(moved)
        root.mkdir()
        (root / "replacement.txt").write_text("never confirmed")
        (root / "keep").mkdir()

    swap_during_the_walk(monkeypatch, swap_the_leaf)

    result = full_uninstall(["claude-code"], home=home, cwd=project,
                            yes=True, purge_data=True,
                            env={"MEMRIVER_ROOT": str(root)})

    assert result.exit_code == 1
    assert "Traceback" not in result.stdout
    # the replacement is intact, byte for byte, and was never entered
    assert (root / "replacement.txt").read_text() == "never confirmed"
    assert (root / "keep").is_dir()
    # the confirmed object is what the walk emptied, and it is left standing
    assert moved.is_dir() and not any(moved.iterdir())
    assert "a different object" in result.stdout


def test_purge_data_leaves_a_replacement_swapped_in_under_the_confirmed_root(
        home, project, tmp_path, monkeypatch):
    """The same swap one level down. A child of the store is renamed away
    after the walk has already opened it, and an unrelated empty directory
    takes its name; the walk empties the object it holds a descriptor on, but
    the ``rmdir`` that detaches it can only name it -- so the name's identity
    is checked once more first, and the stranger is left standing and named."""
    root = tmp_path / "agent-memory"
    (root / "sessions").mkdir(parents=True)
    (root / "sessions" / "one.json").write_text("the object the walk opened")
    moved = tmp_path / "moved-away"

    def swap_the_child() -> None:
        (root / "sessions").rename(moved)
        (root / "sessions").mkdir()  # an unrelated, empty directory

    swap_during_the_walk(monkeypatch, swap_the_child, at=2)

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=True, env={"MEMRIVER_ROOT": str(root)})

    assert result.exit_code != 0
    assert "Traceback" not in result.stdout
    assert (root / "sessions").is_dir()  # the replacement survives
    assert moved.is_dir() and not any(moved.iterdir())  # the real child emptied
    assert f"{root / 'sessions'} was replaced" in result.stdout
    assert "was only partly removed" in result.stdout


def test_purge_data_leaves_a_replacement_that_reuses_the_freed_child_inode(
        home, project, tmp_path, monkeypatch):
    """The harder version of the swap: the original child is not just renamed
    away, it is deleted, so its inode goes back on the free list before the
    replacement is created. A filesystem that hands the same inode straight
    back (ext4 and overlayfs routinely do) would make the replacement
    indistinguishable from the child the walk confirmed -- unless the walk is
    still holding the original open, which keeps that inode allocated and out
    of reach of the ``mkdir``."""
    root = tmp_path / "agent-memory"
    (root / "sessions").mkdir(parents=True)
    (root / "sessions" / "one.json").write_text("the object the walk opened")
    moved = tmp_path / "moved-away"

    def free_the_childs_inode() -> None:
        (root / "sessions").rename(moved)
        (moved / "one.json").unlink()
        moved.rmdir()  # the confirmed inode is released here
        (root / "sessions").mkdir()  # an unrelated, empty directory

    swap_during_the_walk(monkeypatch, free_the_childs_inode, at=2)

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=True, env={"MEMRIVER_ROOT": str(root)})

    assert result.exit_code != 0
    assert "Traceback" not in result.stdout
    assert (root / "sessions").is_dir()  # the replacement survives
    assert f"{root / 'sessions'} was replaced" in result.stdout
    assert "was only partly removed" in result.stdout


def test_purge_data_removes_a_nested_tree_through_the_confirmed_directory(
        home, project, tmp_path):
    """The ordinary case the fd-anchored walk still has to get right: nested
    directories, a symlink that is unlinked rather than followed, and a report
    of success once nothing is left."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "untouched.txt").write_text("not memriver's")
    root = tmp_path / "agent-memory"
    (root / "sessions" / "2026" / "09").mkdir(parents=True)
    (root / "sessions" / "2026" / "09" / "one.json").write_text("{}")
    (root / "index.db").write_text("db")
    (root / "elsewhere").symlink_to(outside, target_is_directory=True)

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=True, env={"MEMRIVER_ROOT": str(root)})

    assert result.exit_code == 0
    assert f"removed {root}" in result.stdout
    assert not root.exists()
    assert (outside / "untouched.txt").read_text() == "not memriver's"


def test_purge_data_reports_a_symlink_loop_instead_of_raising(home, project,
                                                              tmp_path):
    """Canonicalization runs after the harness configuration is already removed;
    a loop there must be a readable refusal, never a traceback on top of a
    half-finished uninstall."""
    looping = tmp_path / "a"
    other = tmp_path / "b"
    looping.symlink_to(other)
    other.symlink_to(looping)

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=True, root=looping)

    assert result.exit_code == 1
    assert "Traceback" not in result.stdout
    assert f"cannot resolve {looping}" in result.stdout
    assert "nothing was removed" in result.stdout


def test_purge_data_reports_a_symlink_loop_above_the_store(home, project, tmp_path):
    """A loop in an ancestor, not the leaf: the store path cannot be resolved,
    which is not the same as a store that is simply absent."""
    looping = tmp_path / "a"
    other = tmp_path / "b"
    looping.symlink_to(other)
    other.symlink_to(looping)
    root = looping / "agent-memory"

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=True, root=root)

    assert result.exit_code == 1
    assert f"cannot resolve {root}" in result.stdout
    assert "nothing was removed" in result.stdout


def test_the_purge_guard_reports_a_current_directory_it_cannot_resolve(home,
                                                                       tmp_path):
    """The protected bases are resolved too, and a loop in one of them is the
    same readable refusal rather than an exception out of the guard."""
    from memriver.uninstall import _refusal_text
    from memriver_core.repository.directories import _refuse_purge_target

    looping = tmp_path / "a"
    other = tmp_path / "b"
    looping.symlink_to(other)
    other.symlink_to(looping)
    store = tmp_path / "agent-memory"
    store.mkdir()

    refusal = _refuse_purge_target(store, store, home=home, cwd=looping)

    assert (refusal.kind, refusal.path) == ("unresolvable", looping)
    assert f"cannot resolve {looping}" in _refusal_text(refusal)
    assert "nothing was removed" in _refusal_text(refusal)


def test_purge_data_removes_the_canonical_target_not_the_given_spelling(
        home, project, tmp_path):
    outside = tmp_path / "outside"
    (outside / "store").mkdir(parents=True)
    (outside / "store" / "marker.txt").write_text("data")
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=True, root=alias / "store")

    assert result.exit_code == 0
    assert f"removed {outside / 'store'}" in result.stdout
    assert not (outside / "store").exists()
    assert alias.is_symlink() and outside.is_dir()


def swap_on_first_open(monkeypatch, swap) -> None:
    """Run ``swap`` just before the first ``os.open`` the purge issues, then
    delegate to the real ``os.open``.

    Config removal on a never-installed home writes nothing, so it opens no
    descriptors; the first ``os.open`` the run reaches is the purge's own. That
    is the instant the reviewer's probe fires: canonicalization and every
    name-based guard are already behind us, and the open is about to re-traverse
    the path they vetted.
    """
    from memriver_core.repository import directories

    real_open = directories.os.open
    fired: list[bool] = []

    def swapping(path, *args, **kwargs):
        if not fired:
            fired.append(True)
            swap()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(directories.os, "open", swapping)


def test_purge_data_refuses_an_ancestor_swapped_for_a_symlink_before_the_confirmed_open(
        home, project, tmp_path, monkeypatch):
    """The reviewer's probe. Every guard runs on the canonical path *string*;
    the open then re-traverses it. With the immediate parent swapped for a
    symlink onto someone else's tree in between, a single full-path open follows
    the swapped ancestor -- O_NOFOLLOW guards only the leaf -- and empties the
    wrong directory. The component-wise no-follow walk opens each ancestor in
    turn, so the swapped-in symlink fails closed and nothing is removed."""
    safe_parent = tmp_path / "safe-parent"
    store = safe_parent / "store"
    (store / "sessions").mkdir(parents=True)
    (store / "sessions" / "one.json").write_text("memriver's own")
    victim_parent = tmp_path / "victim-parent"
    victim = victim_parent / "store"
    victim.mkdir(parents=True)
    precious = victim / "precious.txt"
    precious.write_text("someone else's home")

    def swap_the_parent() -> None:
        safe_parent.rename(tmp_path / "safe-parent-moved")
        (tmp_path / "safe-parent").symlink_to(victim_parent,
                                              target_is_directory=True)

    swap_on_first_open(monkeypatch, swap_the_parent)

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=True, root=store)

    assert result.exit_code != 0
    assert "Traceback" not in result.stdout
    assert "nothing was removed" in result.stdout
    assert precious.read_text() == "someone else's home"
    assert victim.is_dir()


def test_purge_data_refuses_a_symlinked_ancestor_anywhere_in_the_target_path(
        home, project, tmp_path, monkeypatch):
    """Not only the immediate parent: a symlink swapped in at *any* level above
    the leaf redirects a full-path open just the same. The walk opens every
    component with O_NOFOLLOW, so a grandparent turned symlink is refused too."""
    grand = tmp_path / "grand"
    store = grand / "mid" / "store"
    (store / "sessions").mkdir(parents=True)
    (store / "sessions" / "one.json").write_text("memriver's own")
    victim_grand = tmp_path / "victim-grand"
    victim = victim_grand / "mid" / "store"
    victim.mkdir(parents=True)
    precious = victim / "precious.txt"
    precious.write_text("someone else's home")

    def swap_the_grandparent() -> None:
        grand.rename(tmp_path / "grand-moved")
        (tmp_path / "grand").symlink_to(victim_grand, target_is_directory=True)

    swap_on_first_open(monkeypatch, swap_the_grandparent)

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            purge_data=True, root=store)

    assert result.exit_code != 0
    assert "Traceback" not in result.stdout
    assert "nothing was removed" in result.stdout
    assert precious.read_text() == "someone else's home"
    assert victim.is_dir()


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


@pytest.mark.parametrize("error", [
    PermissionError("exec denied"),
    OSError(8, "Exec format error"),
])
def test_an_unstartable_uv_warns_but_keeps_the_exit_code_at_zero(home, project,
                                                                  monkeypatch,
                                                                  error):
    """"Non-fatal" has to cover every way starting the process can fail, not
    just the two the first draft listed: the config (and possibly the data) is
    already gone by the time the cache clean runs."""
    def fake_run(args, **kwargs):
        raise error

    monkeypatch.setattr("memriver.uninstall.subprocess.run", fake_run)

    result = full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                            clean_uv_cache=True)

    assert result.exit_code == 0
    assert "warning" in result.stdout
    assert "uv cache clean memriver" in result.stdout
    assert "uv cache clean memriver-core" in result.stdout


def test_an_interrupt_during_the_cache_clean_is_never_swallowed(home, project,
                                                                 monkeypatch):
    def fake_run(args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("memriver.uninstall.subprocess.run", fake_run)

    with pytest.raises(KeyboardInterrupt):
        full_uninstall(["claude-code"], home=home, cwd=project, yes=True,
                       clean_uv_cache=True)


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


# --- Step 12: shared planning-failure text names uninstall, not install -----


def broken_markers(home: Path, project: Path):
    write(project / "AGENTS.md",
          "<!-- memriver:begin -->\nold\n<!-- memriver:begin -->\nmore\n")
    return ["cursor"], project


def symlinked_target(home: Path, project: Path):
    outside = write(project / "outside.json", json.dumps({"mcpServers": {}}))
    (home / ".claude.json").symlink_to(outside)
    return ["claude-code"], project


def symlinked_parent_directory(home: Path, project: Path):
    elsewhere = home.parent / "claude-elsewhere"
    elsewhere.mkdir()
    (home / ".claude").symlink_to(elsewhere, target_is_directory=True)
    return ["claude-code"], project


def undecodable_target(home: Path, project: Path):
    (home / ".claude.json").write_bytes(b"\xff")
    return ["claude-code"], project


def duplicate_json_keys(home: Path, project: Path):
    write(home / ".claude.json", '{"foreign": "first", "foreign": "second"}')
    return ["claude-code"], project


def json_number_outside_the_standard(home: Path, project: Path):
    write(home / ".claude.json", '{"foreign": 1e400}')
    return ["claude-code"], project


def deeply_nested_json(home: Path, project: Path):
    # deep enough to fail on the parse side, which a removal always reaches --
    # unlike the render side, which only a document it actually changes does;
    # past every supported interpreter's parser limit (3.14's scales with the
    # C stack)
    nested = "[" * 1_000_000 + "]" * 1_000_000
    write(home / ".claude.json", '{"foreign": ' + nested + "}")
    return ["claude-code"], project


def duplicate_hook_identities(home: Path, project: Path):
    write(home / ".claude" / "settings.json", json.dumps({"hooks": {"SessionStart": [
        hook_group("uvx memriver hook session-start --harness claude-code"),
        hook_group("uvx memriver hook session-start --harness codex"),
    ]}}))
    return ["claude-code"], project


def all_outside_a_project(home: Path, project: Path):
    elsewhere = home.parent / "elsewhere"
    elsewhere.mkdir()
    return ALL_HARNESSES, elsewhere


def kiro_outside_a_project(home: Path, project: Path):
    elsewhere = home.parent / "elsewhere-kiro"
    elsewhere.mkdir()
    return ["kiro"], elsewhere


UNINSTALL_REMEDIATIONS = [
    (broken_markers, "fix the markers and run uninstall again"),
    (symlinked_target, "(or remove it) and run uninstall again"),
    (symlinked_parent_directory, "(or remove it) and run uninstall again"),
    (undecodable_target, "then run memriver uninstall again"),
    (duplicate_json_keys, "Remove the duplicate and run uninstall again"),
    (json_number_outside_the_standard, "Fix the value and run uninstall again"),
    (deeply_nested_json, "flatten it and run uninstall again"),
    (duplicate_hook_identities, "remove all but one and run uninstall again"),
    (all_outside_a_project, "run uninstall inside a project or pass one"),
    (kiro_outside_a_project, "run uninstall inside a project or pass one"),
]


@pytest.mark.parametrize("setup,remediation", UNINSTALL_REMEDIATIONS,
                         ids=lambda value: getattr(value, "__name__", ""))
def test_a_planning_failure_names_uninstall_as_the_command_to_re_run(setup,
                                                                     remediation,
                                                                     home, project):
    """The mirror of test_install.py's own
    test_a_planning_failure_names_install_as_the_command_to_re_run: these
    messages come from code both commands share, so each has to end by naming
    the one the user actually ran."""
    harnesses, cwd = setup(home, project)

    result = uninstall(harnesses, home=home, cwd=cwd, yes=True)

    assert result.exit_code == 1
    assert result.stdout.rstrip("\n").endswith(remediation), result.stdout


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


# --- Step 12: uninstall is decoupled from the project bindings --------------


def test_config_uninstall_leaves_the_binding_alone(home, project):
    """Removing a harness configuration is not unbinding a project: the
    binding lives in the store, and only ``memriver project`` writes it."""
    store = home / "agent-memory"
    pid = _bind_new(store, project, "work")
    install(["cursor"], home=home, cwd=project)

    result = uninstall(["cursor"], home=home, cwd=project)

    assert result.exit_code == 0
    project_row = build_service(Settings(root=store), root=store).read_project(pid)
    assert (project_row.name, project_row.root) == ("work", str(project.resolve()))
