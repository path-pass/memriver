"""End-to-end contracts for ``run_install``: preflight, consent, transaction.

Three properties are pinned here, because this is the code that rewrites files
the user already owns.

*Nothing is written before every structural check passes.* Every preflight case
snapshots the whole tree before the call and asserts it is byte- and
mode-identical afterwards, that no backup was created, and that the injected
atomic replace was never called.

*Consent is explicit.* Every changed fragment is printed before the first
prompt, every change gets its own labelled confirmation, declining one keeps
the rest, and a non-interactive stream without ``--yes`` fails instead of
guessing.

*A failed apply is recoverable.* The sibling backup is the only pre-image
(spec 10, DEFERRED-1), so an injected failure on replacement N must restore
targets 1..N-1 from the backups this run wrote and remove the targets it
created -- and never delete a backup, even when the restore itself fails.
"""

from __future__ import annotations

import errno
import io
import json
import os
import stat
from pathlib import Path

import pytest
import tomlkit
from memriver.install import (
    HARNESS_SETTING_TAKEOVER_NOTICE,
    TAKEOVER_NOTICE,
    StoreStep,
    claude_code,
    cursor,
    run_install,
)
from memriver.install.codex import (
    HOOKS_DISABLED_NOTE as CODEX_HOOKS_DISABLED_NOTE,
)
from memriver.install.codex import (
    HOOKS_DISABLED_WITHOUT_DEFINITIONS_NOTE as CODEX_NO_DEFINITIONS_NOTE,
)
from memriver.install.codex import (
    NATIVE_MEMORY_OFF_NOTE as CODEX_NATIVE_MEMORY_OFF_NOTE,
)
from memriver_core.bootstrap import build_service
from memriver_core.settings import Settings


def _bind_new(store: Path, directory: Path, name: str) -> str:
    service = build_service(Settings(root=store), root=store)
    return service.init_project(name, service.plan_root(str(directory))).id


CODEX_TRUST_TEXT = (
    "Run /hooks in Codex, review the memriver hook definitions, and trust them.\n"
    "If this reinstall changed a hook definition, Codex may require re-trust."
)

SECRET = "top-s3cr3t-value"

ALL_HARNESSES = ["claude-code", "codex", "cursor", "kiro"]


# --- harness -----------------------------------------------------------------


class ReplaceSpy:
    """The injected atomic replace, optionally failing on chosen call numbers."""

    def __init__(self, fail_at: set[int] | None = None,
                 raises: type[BaseException] = OSError) -> None:
        self.calls: list[tuple[Path, Path]] = []
        self.fail_at = fail_at or set()
        self.raises = raises

    def __call__(self, source, destination) -> None:
        self.calls.append((Path(source), Path(destination)))
        if len(self.calls) in self.fail_at:
            raise self.raises(f"injected replacement failure #{len(self.calls)}")
        os.replace(source, destination)


class Answers:
    """An ``input_fn`` that records what stdout already held at each prompt."""

    def __init__(self, replies, stdout: io.StringIO) -> None:
        self.replies = list(replies)
        self.stdout = stdout
        self.prompts: list[str] = []
        self.output_at_first_prompt: str | None = None

    def __call__(self, prompt: str) -> str:
        if self.output_at_first_prompt is None:
            self.output_at_first_prompt = self.stdout.getvalue()
        self.prompts.append(prompt)
        if not self.replies:
            raise AssertionError(f"unexpected extra prompt: {prompt!r}")
        return self.replies.pop(0)


def refuse_to_read(prompt: str) -> str:
    """A closed stdin, the way a pipe or CI runner presents itself."""
    raise EOFError(prompt)


class Run:
    def __init__(self, exit_code: int, stdout: str, stderr: str,
                 answers: Answers | None, replace: ReplaceSpy) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.answers = answers
        self.replace = replace


def install(harnesses, *, home: Path, cwd: Path, yes: bool = True,
            dry_run: bool = False, env: dict | None = None, replies=None,
            input_fn=None, replace: ReplaceSpy | None = None,
            store_step=None, stdin_is_tty=True) -> Run:
    out, err = io.StringIO(), io.StringIO()
    replace = replace if replace is not None else ReplaceSpy()
    answers = None
    if input_fn is None:
        answers = Answers(replies or [], out)
        input_fn = answers
    exit_code = run_install(
        harnesses, yes=yes, dry_run=dry_run, home=home, cwd=cwd,
        env=env if env is not None else {}, input_fn=input_fn, stdout=out, stderr=err,
        replace_file=replace, store_step=store_step, stdin_is_tty=stdin_is_tty,
    )
    return Run(exit_code, out.getvalue(), err.getvalue(), answers, replace)


def snapshot_tree(root: Path) -> dict:
    """Every path under ``root`` with its kind, bytes, and permission bits."""
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


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def backups(root: Path) -> list[Path]:
    return sorted(root.rglob("*.memriver-backup-*"))


def write(path: Path, text: str, mode: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)
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


# --- Step 1: the preflight writes nothing ------------------------------------


def malformed_json(home: Path, project: Path):
    write(home / ".claude.json", "{ this is not json")
    return ["claude-code"], project


def malformed_toml(home: Path, project: Path):
    write(home / ".codex" / "config.toml", "this = = not toml\n")
    return ["codex"], project


def duplicate_hook_identities(home: Path, project: Path):
    write(home / ".claude" / "settings.json", json.dumps({"hooks": {"SessionStart": [
        hook_group("uvx memriver hook session-start --harness claude-code"),
        hook_group("uvx memriver hook session-start --harness codex"),
    ]}}))
    return ["claude-code"], project


def broken_markers(home: Path, project: Path):
    write(project / "AGENTS.md",
          "<!-- memriver:begin -->\nold\n<!-- memriver:begin -->\nmore\n")
    return ["cursor"], project


def symlinked_target(home: Path, project: Path):
    outside = write(project.parent / "outside.json", json.dumps({"mcpServers": {}}))
    (home / ".claude.json").symlink_to(outside)
    return ["claude-code"], project


def symlinked_parent_directory(home: Path, project: Path):
    elsewhere = home.parent / "claude-elsewhere"
    elsewhere.mkdir()
    (home / ".claude").symlink_to(elsewhere, target_is_directory=True)
    return ["claude-code"], project


def json_number_outside_the_standard(home: Path, project: Path):
    # a legal JSON literal that decodes to float('inf'); re-serializing it
    # writes the non-standard token `Infinity`, which strict parsers reject
    write(home / ".claude.json", '{"foreign": 1e400}')
    return ["claude-code"], project


def json_nonstandard_constant(home: Path, project: Path):
    write(home / ".claude.json", '{"foreign": Infinity}')
    return ["claude-code"], project


def duplicate_json_keys(home: Path, project: Path):
    # the decoder keeps only the last value, so re-serializing silently drops
    # a foreign value the user was never shown and never confirmed
    write(home / ".claude.json", '{"foreign": "first", "foreign": "second"}')
    return ["claude-code"], project


def oversized_json_integer(home: Path, project: Path):
    # syntactically legal JSON that the decoder still refuses: CPython's
    # integer-string conversion limit raises a plain ValueError -- not a
    # JSONDecodeError -- from inside json.loads
    write(home / ".claude.json", '{"foreign": ' + "1" * 5000 + "}")
    return ["claude-code"], project


def deeply_nested_json(home: Path, project: Path):
    # syntactically legal JSON that the decoder still refuses: nesting past
    # the interpreter's recursion limit raises RecursionError from inside
    # json.loads -- not a ValueError, so it needs its own boundary mapping
    nested = "[" * 2000 + "]" * 2000
    write(home / ".claude.json", '{"foreign": ' + nested + "}")
    return ["claude-code"], project


def undecodable_target(home: Path, project: Path):
    # not malformed JSON -- bytes that are not text at all, so the failure is
    # in the read, before any parser is reached
    (home / ".claude.json").write_bytes(b"\xff")
    return ["claude-code"], project


def all_outside_a_project(home: Path, project: Path):
    elsewhere = home.parent / "elsewhere"
    elsewhere.mkdir()
    return ALL_HARNESSES, elsewhere


def kiro_outside_a_project(home: Path, project: Path):
    elsewhere = home.parent / "elsewhere-kiro"
    elsewhere.mkdir()
    return ["kiro"], elsewhere


PREFLIGHT_FAILURES = [
    malformed_json,
    malformed_toml,
    json_number_outside_the_standard,
    json_nonstandard_constant,
    duplicate_json_keys,
    oversized_json_integer,
    deeply_nested_json,
    undecodable_target,
    duplicate_hook_identities,
    broken_markers,
    symlinked_target,
    symlinked_parent_directory,
    all_outside_a_project,
]


@pytest.mark.parametrize("setup", PREFLIGHT_FAILURES, ids=lambda f: f.__name__)
def test_planning_failure_writes_absolutely_nothing(setup, tmp_path, home, project):
    harnesses, cwd = setup(home, project)
    before_tree = snapshot_tree(tmp_path)

    result = install(harnesses, home=home, cwd=cwd, yes=True)

    after_tree = snapshot_tree(tmp_path)
    assert result.exit_code != 0
    assert after_tree == before_tree
    assert list(tmp_path.rglob("*.memriver-backup-*")) == []
    assert result.replace.calls == []


INSTALL_REMEDIATIONS = [
    (broken_markers, "fix the markers and run install again"),
    (symlinked_target, "(or remove it) and run install again"),
    (symlinked_parent_directory, "(or remove it) and run install again"),
    (undecodable_target, "then run memriver install again"),
    (duplicate_json_keys, "Remove the duplicate and run install again"),
    (json_number_outside_the_standard, "Fix the value and run install again"),
    (json_nonstandard_constant, "Fix the value and run install again"),
    (deeply_nested_json, "flatten it and run install again"),
    (duplicate_hook_identities, "remove all but one and run install again"),
    (all_outside_a_project, "run install inside a project or pass one"),
    (kiro_outside_a_project, "run install inside a project or pass one"),
]


@pytest.mark.parametrize("setup,remediation", INSTALL_REMEDIATIONS,
                         ids=lambda value: getattr(value, "__name__", ""))
def test_a_planning_failure_names_install_as_the_command_to_re_run(setup,
                                                                   remediation,
                                                                   home, project):
    """Every one of these messages ends by telling the user what to run next.
    A bare ``memriver`` is the compatibility ``serve`` path, not a retry, so
    the sentence has to name the command they actually ran."""
    harnesses, cwd = setup(home, project)

    result = install(harnesses, home=home, cwd=cwd, yes=True)

    assert result.exit_code == 1
    assert result.stderr.rstrip("\n").endswith(remediation), result.stderr


@pytest.mark.parametrize(
    "setup", [undecodable_target, oversized_json_integer, deeply_nested_json],
    ids=lambda f: f.__name__)
def test_an_unreadable_target_is_a_planning_failure_not_a_traceback(setup, home,
                                                                    project):
    """A read that fails is a planning failure like a parse that fails.

    `exists/is_file/read_text/stat` raise UnicodeDecodeError, PermissionError
    and other OSError -- none of which the PlanningError-only boundary in
    run_install catches, so they used to reach the user as a traceback
    carrying absolute source paths. `json.loads` on a syntactically legal but
    too-deeply-nested document raises RecursionError instead, which is not a
    ValueError either.
    """
    harnesses, cwd = setup(home, project)

    result = install(harnesses, home=home, cwd=cwd, yes=True)

    assert result.exit_code != 0
    assert result.stderr.startswith("memriver install: ")
    assert "Traceback" not in result.stderr
    assert "codec" not in result.stderr  # no underlying exception text


def test_deeply_nested_json_is_one_line_not_a_recursion_traceback(home, project):
    harnesses, cwd = deeply_nested_json(home, project)
    before_tree = snapshot_tree(home)

    result = install(harnesses, home=home, cwd=cwd, yes=True)

    assert result.exit_code == 1
    assert result.stderr == (
        "memriver install: file nests too deeply for memriver to parse; "
        "flatten it and run install again\n"
    )
    assert snapshot_tree(home) == before_tree
    assert backups(home) == []
    assert result.replace.calls == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_an_unreadable_target_leaks_neither_traceback_nor_old_values(tmp_path, home,
                                                                    project):
    target = write(home / ".claude.json", json.dumps({"apiKey": SECRET}), mode=0o000)
    before_tree = snapshot_tree(project)
    try:
        result = install(["claude-code"], home=home, cwd=project, yes=True)
    finally:
        target.chmod(0o600)

    assert result.exit_code != 0
    assert "Traceback" not in result.stderr
    assert SECRET not in result.stderr
    assert "Permission denied" not in result.stderr
    assert snapshot_tree(project) == before_tree
    assert backups(tmp_path) == []
    assert result.replace.calls == []


def test_a_planning_failure_reports_the_reason_rather_than_a_fake_change(home,
                                                                        project):
    write(home / ".claude.json", "{ this is not json")

    result = install(["claude-code"], home=home, cwd=project)

    assert result.exit_code != 0
    assert "not valid JSON" in result.stderr
    assert "installed" not in result.stderr


def test_incompatible_duplicate_target_declarations_are_rejected(tmp_path, home,
                                                                 project,
                                                                 monkeypatch):
    """The same path claimed as user-level by one harness and project-level by
    another is an unresolvable classification, not a merge."""
    claude_config, _ = claude_code.targets(home, None, "install")
    real_targets = cursor.targets

    def clashing_targets(home_dir, project_root, command_name):
        mcp, instructions = real_targets(home_dir, project_root, command_name)
        return type(mcp)(path=claude_config.path, user_level=False,
                         rollback_instruction=mcp.rollback_instruction), instructions

    monkeypatch.setattr(cursor, "targets", clashing_targets)
    write(claude_config.path, "{}")
    before_tree = snapshot_tree(tmp_path)

    result = install(["claude-code", "cursor"], home=home, cwd=project)

    assert result.exit_code != 0
    assert str(claude_config.path) in result.stderr
    assert snapshot_tree(tmp_path) == before_tree
    assert result.replace.calls == []


def test_an_unknown_harness_name_fails_before_any_target_is_read(tmp_path, home,
                                                                project):
    before_tree = snapshot_tree(tmp_path)

    result = install(["not-a-harness"], home=home, cwd=project)

    assert result.exit_code != 0
    assert "not-a-harness" in result.stderr
    assert snapshot_tree(tmp_path) == before_tree


# --- Step 2: diffs, confirmation, dry run ------------------------------------


def test_every_planned_change_is_printed_before_the_first_prompt(home, project):
    result = install(["claude-code"], home=home, cwd=project, yes=False,
                     replies=["y", "y", "y", "y"])

    shown = result.answers.output_at_first_prompt
    assert shown is not None
    for label in ("register memriver MCP server", "install the session-start hook",
                  "install the stop hook", "disable built-in auto memory"):
        assert label in shown


def test_each_change_gets_its_own_labelled_confirmation(home, project):
    result = install(["claude-code"], home=home, cwd=project, yes=False,
                     replies=["y", "y", "y", "y"])

    assert len(result.answers.prompts) == 4
    joined = " ".join(result.answers.prompts)
    assert "claude-code: register memriver MCP server -> ~/.claude.json" in joined
    assert "disable built-in auto memory" in joined


def test_every_confirmation_names_its_harness_and_its_target(home, project):
    """`--all` asks four times to "register memriver MCP server"; without the
    harness and the file each prompt writes, the four are indistinguishable."""
    result = install(ALL_HARNESSES, home=home, cwd=project, yes=False,
                     replies=["n"] * 12)

    prompts = result.answers.prompts
    mcp = [p for p in prompts if "register memriver MCP server" in p]
    assert len(mcp) == 4
    assert len(set(mcp)) == 4
    for harness in ALL_HARNESSES:
        assert any(p.startswith(f"apply: {harness}: ") for p in prompts)
    for prompt in prompts:
        assert " -> " in prompt
    assert any("-> ~/.claude.json?" in p for p in mcp)
    # a user-level path is shown home-relative, never as somebody's real home
    assert not any(str(home) in p for p in prompts)


def test_a_takeover_is_labelled_and_confirmed_without_showing_the_old_value(home,
                                                                           project):
    write(home / ".claude.json", json.dumps({
        "apiKey": SECRET,
        "mcpServers": {"memriver": {"command": "old", "args": ["--token", SECRET]}},
    }))

    result = install(["claude-code"], home=home, cwd=project, yes=False,
                     replies=["y", "y", "y", "y"])

    assert result.exit_code == 0
    assert TAKEOVER_NOTICE in result.stdout
    assert SECRET not in result.stdout
    assert json.loads((home / ".claude.json").read_text())["apiKey"] == SECRET


def test_the_native_memory_takeover_does_not_call_it_a_memriver_entry(home, project):
    """spec 5.3 wants this one prompt clearly labelled, and
    ``env.CLAUDE_CODE_DISABLE_AUTO_MEMORY`` is Claude Code's setting, not a
    memriver entry -- the only takeover in this run must say so."""
    write(home / ".claude" / "settings.json",
          json.dumps({"env": {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0"}}))

    result = install(["claude-code"], home=home, cwd=project, yes=False,
                     replies=["y", "y", "y", "y"])

    assert result.exit_code == 0
    assert HARNESS_SETTING_TAKEOVER_NOTICE in result.stdout
    assert TAKEOVER_NOTICE not in result.stdout


def mutate_at_last_prompt(path: Path, mutate) -> tuple[callable, list]:
    """An ``input_fn`` that accepts everything and, once the last change has
    been confirmed, lets another process rewrite ``path`` -- exactly the window
    between the planning snapshot and the first write."""
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 4:  # the last claude-code confirmation
            mutate(path)
        return "y"

    return answer, prompts


def test_a_target_rewritten_between_planning_and_apply_aborts_the_run(home,
                                                                     project,
                                                                     tmp_path):
    """Another agent rewriting ~/.claude/settings.json while the user answers
    prompts must not be overwritten from a snapshot taken before it wrote."""
    config = write(home / ".claude.json", json.dumps({"mcpServers": {}}))
    settings = home / ".claude" / "settings.json"
    write(settings, json.dumps({"hooks": {}}))
    before_config = config.read_bytes()
    concurrent = json.dumps({"hooks": {}, "addedByAnotherAgent": True})

    answer, prompts = mutate_at_last_prompt(
        settings, lambda path: path.write_text(concurrent, encoding="utf-8"))
    result = install(["claude-code"], home=home, cwd=project, yes=False,
                     input_fn=answer)

    assert len(prompts) == 4
    assert result.exit_code == 1
    assert "file changed since planning" in result.stderr
    abort = next(line for line in result.stderr.splitlines()
                 if "file changed since planning" in line)
    # the abort line abbreviates like the prompts do; the rollback report below
    # it still prints the absolute backup paths the user needs
    assert abort.endswith("~/.claude/settings.json: file changed since planning; "
                          "nothing further was written -- re-run memriver install")
    # the concurrent write survives untouched, and the target written before it
    # is rolled back from this run's backup
    assert settings.read_text() == concurrent
    assert config.read_bytes() == before_config
    assert [p.name.split(".memriver-backup-")[0] for p in backups(tmp_path)] == [
        ".claude.json"]


def test_a_target_created_between_planning_and_apply_aborts_the_run(home, project,
                                                                    tmp_path):
    """Planning saw no file; by apply time one exists. Writing would destroy it."""
    settings = home / ".claude" / "settings.json"
    concurrent = json.dumps({"hooks": {}})

    answer, prompts = mutate_at_last_prompt(
        settings, lambda path: write(path, concurrent))
    result = install(["claude-code"], home=home, cwd=project, yes=False,
                     input_fn=answer)

    assert len(prompts) == 4
    assert result.exit_code == 1
    assert "file changed since planning" in result.stderr
    assert settings.read_text() == concurrent
    # ~/.claude.json was created by this run and is removed again on rollback
    assert not (home / ".claude.json").exists()
    assert backups(tmp_path) == []


def test_a_permission_change_between_planning_and_apply_aborts_the_run(home,
                                                                       project):
    """The mode is part of what planning read: a target whose permissions moved
    under us is not the file the plan was made against."""
    write(home / ".claude.json", json.dumps({"mcpServers": {}}))
    settings = write(home / ".claude" / "settings.json", json.dumps({"hooks": {}}),
                     mode=0o600)

    answer, prompts = mutate_at_last_prompt(settings,
                                            lambda path: path.chmod(0o644))
    result = install(["claude-code"], home=home, cwd=project, yes=False,
                     input_fn=answer)

    assert len(prompts) == 4
    assert result.exit_code == 1
    assert "file changed since planning" in result.stderr
    assert mode_of(settings) == 0o644


def test_declining_the_native_memory_change_keeps_the_accepted_ones(home, project):
    result = install(["claude-code"], home=home, cwd=project, yes=False,
                     replies=["y", "y", "y", "n"])

    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert result.exit_code == 0
    assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY" not in json.dumps(settings.get("env", {}))
    assert len(settings["hooks"]["SessionStart"]) == 1
    assert json.loads((home / ".claude.json").read_text())["mcpServers"]["memriver"]


def test_declining_everything_writes_nothing(tmp_path, home, project):
    before_tree = snapshot_tree(tmp_path)

    result = install(["claude-code"], home=home, cwd=project, yes=False,
                     replies=["n", "n", "n", "n"])

    assert result.exit_code == 0
    assert snapshot_tree(tmp_path) == before_tree
    assert result.replace.calls == []


def test_declining_every_codex_change_still_states_trust_and_native_memory(home,
                                                                           project):
    """Declining is a normal completion, not a reason to drop the read-only
    tail. The hooks pre-installed here may still be untrusted, so the user
    needs the /hooks step; spec 5.3 also owes them the native-memory verdict.
    Only the MCP registration is left to decline, so `nothing accepted` cannot
    be read as `nothing was installed, hence nothing to trust`."""
    write(home / ".codex" / "hooks.json", json.dumps({"hooks": {
        "SessionStart": [
            hook_group("uvx memriver hook session-start --harness codex")],
        "Stop": [hook_group("uvx memriver hook stop --harness codex")],
    }}))

    result = install(["codex"], home=home, cwd=project, yes=False, replies=["n"])

    assert result.exit_code == 0
    assert len(result.answers.prompts) == 1
    assert "nothing accepted; no file was changed." in result.stdout
    assert CODEX_TRUST_TEXT in result.stdout
    assert CODEX_NATIVE_MEMORY_OFF_NOTE in result.stdout


def test_yes_accepts_every_change_including_native_memory(home, project):
    result = install(["claude-code"], home=home, cwd=project, yes=True)

    settings = json.loads((home / ".claude" / "settings.json").read_text())
    assert result.exit_code == 0
    assert settings["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert result.answers.prompts == []


def test_non_interactive_input_without_yes_fails_before_any_write(tmp_path, home,
                                                                  project):
    before_tree = snapshot_tree(tmp_path)

    result = install(["claude-code"], home=home, cwd=project, yes=False,
                     input_fn=refuse_to_read)

    assert result.exit_code != 0
    assert "--yes" in result.stderr
    assert snapshot_tree(tmp_path) == before_tree
    assert result.replace.calls == []


def test_dry_run_renders_the_plan_and_the_trust_note_but_creates_nothing(tmp_path,
                                                                        home,
                                                                        project):
    before_tree = snapshot_tree(tmp_path)

    result = install(["codex"], home=home, cwd=project, yes=False, dry_run=True,
                     input_fn=refuse_to_read)

    assert result.exit_code == 0
    assert "register memriver MCP server" in result.stdout
    assert CODEX_TRUST_TEXT in result.stdout
    assert snapshot_tree(tmp_path) == before_tree
    assert backups(tmp_path) == []
    assert result.replace.calls == []
    assert not (home / ".codex").exists()


# --- the configured commands have to be resolvable at harness start ----------


def test_install_says_so_when_uvx_is_not_on_path(home, project, monkeypatch):
    """Every hook and MCP entry install writes runs ``uvx memriver``, and the
    harness resolves that string itself, long after install has exited. A
    machine with no ``uvx`` on PATH gets a config that looks installed and a
    harness that silently cannot start it, so the report says so."""
    monkeypatch.setattr("shutil.which", lambda name: None)

    result = install(["claude-code"], home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    assert "uvx memriver" in result.stdout
    assert "not found on PATH" in result.stdout


def test_install_is_silent_about_uvx_when_it_is_on_path(home, project, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/somewhere/bin/uvx")

    result = install(["claude-code"], home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    assert "not found on PATH" not in result.stdout


def test_a_reinstall_that_changes_nothing_reports_it_and_prompts_for_nothing(home,
                                                                            project):
    install(["claude-code"], home=home, cwd=project, yes=True)
    before_tree = snapshot_tree(home)

    result = install(["claude-code"], home=home, cwd=project, yes=False,
                     env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
                     input_fn=refuse_to_read)

    assert result.exit_code == 0
    assert "already up to date" in result.stdout
    assert CODEX_TRUST_TEXT not in result.stdout  # no Codex, no Codex note
    assert snapshot_tree(home) == before_tree


@pytest.mark.parametrize("config_text", ["", "[features]\nmemories = false\n"])
def test_codex_native_memory_left_alone_is_reported_not_passed_over(home, project,
                                                                   config_text):
    """spec 5.3: unset or off means "do nothing **and say so**".

    Planning simply omits the operation, which is correct -- there is nothing
    to write -- but silence leaves the user unable to tell a checked
    non-conflict from a check that never ran. It is a read-only note, never a
    confirmable operation.
    """
    if config_text:
        write(home / ".codex" / "config.toml", config_text)

    result = install(["codex"], home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    assert CODEX_NATIVE_MEMORY_OFF_NOTE in result.stdout
    assert "disable built-in auto memory" not in result.stdout


def test_codex_native_memory_that_is_on_is_an_operation_not_a_note(home, project):
    write(home / ".codex" / "config.toml", "[features]\nmemories = true\n")

    result = install(["codex"], home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    assert "disable built-in auto memory" in result.stdout
    assert CODEX_NATIVE_MEMORY_OFF_NOTE not in result.stdout
    assert tomlkit.parse(
        (home / ".codex" / "config.toml").read_text())["features"]["memories"] is False


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_codex_reinstall_that_changes_nothing_still_states_the_trust_step(
        home, project, dry_run):
    """The trust step is not a property of writing; it is a property of Codex.

    Codex hooks are unmanaged and must be reviewed through /hooks before they
    run at all. A user who missed the note the first time reaches for the
    installer again -- and "already up to date" without the note tells them
    nothing is left to do, when the step that makes the hooks work is.
    """
    install(["codex"], home=home, cwd=project, yes=True)
    before_tree = snapshot_tree(home)

    result = install(["codex"], home=home, cwd=project, yes=False,
                     dry_run=dry_run, input_fn=refuse_to_read)

    assert result.exit_code == 0
    assert "already up to date" in result.stdout
    assert CODEX_TRUST_TEXT in result.stdout
    assert snapshot_tree(home) == before_tree


# --- Step 3: the transaction --------------------------------------------------


def test_every_existing_changed_target_gets_one_backup_of_the_whole_file(home,
                                                                        project):
    original = json.dumps({"apiKey": SECRET, "mcpServers": {}})
    write(home / ".claude.json", original)
    write(home / ".claude" / "settings.json", json.dumps({"hooks": {}}))

    result = install(["claude-code"], home=home, cwd=project, yes=True)

    made = backups(home)
    assert result.exit_code == 0
    assert len(made) == 2
    backup = next(b for b in made if b.name.startswith(".claude.json"))
    assert backup.read_text(encoding="utf-8") == original


def test_the_backup_is_written_before_the_replacement(home, project):
    write(home / ".claude.json", json.dumps({"mcpServers": {}}))
    replace = ReplaceSpy(fail_at={1})

    install(["claude-code"], home=home, cwd=project, yes=True, replace=replace)

    assert len(backups(home)) == 1  # created even though the replace never landed


def test_user_config_backup_and_new_user_config_are_owner_only(home, project):
    write(home / ".claude.json", json.dumps({"mcpServers": {}}), mode=0o644)

    result = install(["claude-code"], home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    assert mode_of(backups(home)[0]) == 0o600
    assert mode_of(home / ".claude.json") == 0o644  # rewrite preserves the old mode
    assert mode_of(home / ".claude" / "settings.json") == 0o600  # new user config


def test_project_document_backup_preserves_the_source_mode(home, project):
    write(project / "AGENTS.md", "# notes\n", mode=0o640)

    result = install(["cursor"], home=home, cwd=project, yes=True)

    backup = backups(project)[0]
    assert result.exit_code == 0
    assert mode_of(backup) == 0o640
    assert mode_of(project / "AGENTS.md") == 0o640


def test_a_new_project_document_uses_the_process_umask(home, project):
    mask = os.umask(0)
    os.umask(mask)

    result = install(["cursor"], home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    assert mode_of(project / "AGENTS.md") == 0o666 & ~mask


def test_exclusive_creation_never_overwrites_an_existing_backup(tmp_path, home,
                                                               project,
                                                               monkeypatch):
    monkeypatch.setattr("memriver.install._utc_timestamp", lambda: "FIXED")
    original = json.dumps({"mcpServers": {}})
    write(home / ".claude.json", original)
    occupied = write(home / ".claude.json.memriver-backup-FIXED", "someone else's\n")

    result = install(["claude-code"], home=home, cwd=project, yes=True)

    assert result.exit_code != 0
    assert occupied.read_text() == "someone else's\n"
    assert (home / ".claude.json").read_text() == original
    assert result.replace.calls == []


def test_failure_restores_earlier_targets_and_removes_created_ones(home, project):
    claude_json = write(home / ".claude.json", json.dumps({"apiKey": SECRET}),
                        mode=0o644)
    codex_toml = write(home / ".codex" / "config.toml", 'model = "gpt"\n')
    original_json, original_toml = claude_json.read_bytes(), codex_toml.read_bytes()
    # writes are (1) ~/.claude.json, (2) the new ~/.claude/settings.json,
    # (3) ~/.codex/config.toml -- fail on the third
    replace = ReplaceSpy(fail_at={3})

    result = install(["claude-code", "codex"], home=home, cwd=project, yes=True,
                     replace=replace)

    assert result.exit_code != 0
    assert claude_json.read_bytes() == original_json
    assert mode_of(claude_json) == 0o644
    assert not (home / ".claude" / "settings.json").exists()
    assert codex_toml.read_bytes() == original_toml
    assert len(backups(home)) == 2  # ~/.claude.json and ~/.codex/config.toml
    assert "restored" in result.stderr and str(claude_json) in result.stderr
    assert "removed" in result.stderr


def test_rollback_removes_the_directories_this_run_created(home, project):
    """Apply is a transaction, so a failed run leaves the tree it found. The
    created files were removed but the parents `_write_target` had to make on
    the way -- ~/.kiro/settings, .kiro/steering -- stayed behind as empty
    directories nobody asked for."""
    before_home, before_project = snapshot_tree(home), snapshot_tree(project)
    # writes are (1) ~/.kiro/settings/mcp.json, (2) .kiro/steering/memriver.md
    replace = ReplaceSpy(fail_at={2})

    result = install(["kiro"], home=home, cwd=project, yes=True, replace=replace)

    assert result.exit_code != 0
    assert snapshot_tree(home) == before_home
    assert snapshot_tree(project) == before_project
    assert backups(home) == [] and backups(project) == []


def test_rollback_keeps_a_directory_that_was_already_there(home, project):
    """Only what this run created comes out: a parent that already existed, or
    one still holding somebody else's file, is not memriver's to remove."""
    write(home / ".kiro" / "settings" / "someone-else.json", "{}")
    before_home, before_project = snapshot_tree(home), snapshot_tree(project)

    result = install(["kiro"], home=home, cwd=project, yes=True,
                     replace=ReplaceSpy(fail_at={2}))

    assert result.exit_code != 0
    assert snapshot_tree(home) == before_home
    assert snapshot_tree(project) == before_project


def test_a_directory_creation_that_fails_part_way_leaves_nothing_behind(
        monkeypatch, home, project):
    """`mkdir` is not atomic across levels. Making `.kiro/steering` can create
    `.kiro` and then fail on the level below it, and the directories a run
    guessed it would create are not the ones it did create: the guess was made
    before the call, and the call itself ran outside the cleanup boundary, so
    the half-built tree stayed."""
    real_mkdir = Path.mkdir
    before_home, before_project = snapshot_tree(home), snapshot_tree(project)

    def partial_mkdir(self, *args, **kwargs):
        if self.name == "steering":
            real_mkdir(self.parent, exist_ok=True)  # the level that did succeed
            raise OSError("injected mkdir failure below .kiro")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", partial_mkdir)

    result = install(["kiro"], home=home, cwd=project, yes=True)

    assert result.exit_code != 0
    assert snapshot_tree(project) == before_project
    assert snapshot_tree(home) == before_home


def test_rollback_leaves_a_directory_another_actor_created(monkeypatch, home,
                                                           project):
    """Emptiness is not ownership. Another process creating `~/.kiro` while
    this run was on its way to the same directory leaves a directory this run
    did not make; `rmdir` succeeding on it only proves nobody has put a file
    there yet."""
    real_mkdir = Path.mkdir
    contested = home / ".kiro"

    def losing_mkdir(self, *args, **kwargs):
        if not contested.exists() and contested in (self, *self.parents):
            real_mkdir(contested)  # the other actor gets there first
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", losing_mkdir)

    result = install(["kiro"], home=home, cwd=project, yes=True,
                     replace=ReplaceSpy(fail_at={2}))

    assert result.exit_code != 0
    assert contested.is_dir()
    assert snapshot_tree(contested) == {}  # only what this run made came out


def test_rollback_climbs_past_a_created_directory_another_actor_removed(home,
                                                                         project):
    """`_make_dirs` records a directory by its identity, not just its path, but
    a *missing* path is not evidence against the parent above it: another
    actor taking the empty leaf this run made does not stop the leaf's own
    parent -- also made by this run and now genuinely empty -- from being
    cleaned up too."""
    settings = home / ".kiro" / "settings"
    kiro = home / ".kiro"
    calls: list[Path] = []

    def replace_file(source: Path, destination: Path) -> None:
        calls.append(Path(destination))
        if len(calls) == 1:
            Path(source).unlink()  # memriver's own in-flight temp file
            settings.rmdir()  # another actor takes the directory this run made
            raise OSError("injected replacement failure")
        os.replace(source, destination)

    result = install(["kiro"], home=home, cwd=project, yes=True,
                     replace=replace_file)

    assert result.exit_code != 0
    assert not settings.exists()
    assert not kiro.exists()  # the missing child does not stop the climb


def test_rollback_leaves_a_directory_whose_path_was_recycled_by_another_actor(
        home, project):
    """A path recorded as created can still exist at rollback time and yet no
    longer be the directory this run made: another actor deleted and
    recreated it with a fresh inode in the same window. `rmdir` proves only
    that today's occupant is empty, never that it is the one memriver made, so
    a path whose identity no longer matches -- and everything above it, now
    provably non-empty -- is left alone.

    Staging that needs the recreated directory to get a *new* inode, and
    ext4 and overlayfs routinely hand the freed one straight back. Holding a
    descriptor on the original across the `rmdir` is what makes the outcome
    the same everywhere: an open descriptor keeps that inode allocated, so the
    `mkdir` below cannot be given it and the two directories stay
    distinguishable on every filesystem."""
    settings = home / ".kiro" / "settings"
    kiro = home / ".kiro"
    calls: list[Path] = []
    original: list[int] = []

    def replace_file(source: Path, destination: Path) -> None:
        calls.append(Path(destination))
        if len(calls) == 1:
            Path(source).unlink()  # memriver's own in-flight temp file
            held = os.open(settings, os.O_RDONLY | os.O_DIRECTORY)
            try:
                original.append(settings.lstat().st_ino)
                settings.rmdir()
                settings.mkdir()  # a different actor's directory, same path
            finally:
                os.close(held)
            raise OSError("injected replacement failure")
        os.replace(source, destination)

    result = install(["kiro"], home=home, cwd=project, yes=True,
                     replace=replace_file)

    assert settings.lstat().st_ino != original[0]  # the recycle really happened
    assert result.exit_code != 0
    assert settings.is_dir()  # the replacement survives
    assert kiro.is_dir()  # holding a stranger's directory, not provably empty


def test_rollback_removes_a_created_directory_whose_identity_stat_failed(
        monkeypatch, home, project):
    """The identity `lstat` right after `mkdir` is its own fallible filesystem
    call, distinct from the `mkdir` that already succeeded. If it raises, the
    directory must not silently drop out of `_make_dirs`'s bookkeeping and
    leak -- it is still this run's to clean up, verified or not."""
    settings = home / ".kiro" / "settings"
    kiro = home / ".kiro"
    real_lstat = Path.lstat

    def failing_lstat(self, *args, **kwargs):
        if self == settings and self.exists():
            raise OSError("injected identity-stat failure")
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", failing_lstat)

    result = install(["kiro"], home=home, cwd=project, yes=True,
                     replace=ReplaceSpy(fail_at={1}))

    assert result.exit_code != 0
    assert not settings.exists()
    assert not kiro.exists()


def test_an_ordinary_identity_stat_failure_degrades_and_the_install_still_completes(
        monkeypatch, home, project):
    """An `OSError` from the post-`mkdir` identity `lstat` is a degraded but
    unremarkable outcome, not a reason to give up on the rest of the install:
    the directory keeps its unverified (`dev`/`ino` still `None`) placeholder
    record and the run proceeds to completion, unlike a `KeyboardInterrupt` or
    any other `BaseException`, which is not caught here at all."""
    settings = home / ".kiro" / "settings"
    real_lstat = Path.lstat

    def failing_lstat(self, *args, **kwargs):
        if self == settings and self.exists():
            raise OSError("injected identity-stat failure")
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", failing_lstat)

    result = install(["kiro"], home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    assert settings.is_dir()
    assert (settings / "mcp.json").exists()
    assert (project / ".kiro" / "steering" / "memriver.md").exists()


def test_a_later_failure_still_rolls_back_a_directory_with_a_placeholder_record(
        monkeypatch, home, project):
    """The placeholder left by a degraded identity `lstat` is not exempt from
    ordinary transaction rollback: a *different*, later write failing must
    still unwind the earlier, already-completed write through `_roll_back`,
    and `_remove_created_dirs` must still take back that write's directory
    even though its record was never verified.

    The mock fails only the one `lstat` call this is actually about --
    `_make_dirs`'s post-`mkdir` identity capture for `settings`, the instant
    it starts existing -- and behaves normally after: rollback's own
    symlink-component check also calls `lstat` on this same directory later,
    and it must see the truth (an ordinary directory, not a link) rather than
    inherit a permanently degraded stat that has nothing to do with it."""
    settings = home / ".kiro" / "settings"
    kiro = home / ".kiro"
    real_lstat = Path.lstat
    degraded = {"done": False}

    def failing_lstat(self, *args, **kwargs):
        if self == settings and self.exists() and not degraded["done"]:
            degraded["done"] = True
            raise OSError("injected identity-stat failure")
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", failing_lstat)

    # writes are (1) ~/.kiro/settings/mcp.json -- completes despite the
    # degraded identity stat -- then (2) .kiro/steering/memriver.md, which
    # fails and rolls the whole transaction, including write (1), back
    result = install(["kiro"], home=home, cwd=project, yes=True,
                     replace=ReplaceSpy(fail_at={2}))

    assert result.exit_code != 0
    assert not settings.exists()
    assert not kiro.exists()


def test_a_keyboard_interrupt_between_mkdir_and_lstat_still_gets_rolled_back(
        monkeypatch, home, project):
    """A directory is recorded the instant `mkdir` returns, before the
    identity `lstat` right after it ever runs -- so a `KeyboardInterrupt`
    landing in that gap (not just an `OSError` from `lstat` itself) still
    finds the directory already in `created` and rolls it back, instead of
    leaking it because the interrupt struck before the record existed."""
    settings = home / ".kiro" / "settings"
    kiro = home / ".kiro"
    real_lstat = Path.lstat

    def interrupting_lstat(self, *args, **kwargs):
        if self == settings and self.exists():
            raise KeyboardInterrupt()
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", interrupting_lstat)

    with pytest.raises(KeyboardInterrupt):
        install(["kiro"], home=home, cwd=project, yes=True)

    assert not settings.exists()
    assert not kiro.exists()


def test_an_interrupt_rolls_the_run_back_and_still_propagates(home, project):
    """Ctrl-C between replacements must not leave a half-applied tree behind."""
    claude_json = write(home / ".claude.json", json.dumps({"apiKey": SECRET}),
                        mode=0o644)
    codex_toml = write(home / ".codex" / "config.toml", 'model = "gpt"\n')
    original_json, original_toml = claude_json.read_bytes(), codex_toml.read_bytes()
    out, err = io.StringIO(), io.StringIO()
    replace = ReplaceSpy(fail_at={3}, raises=KeyboardInterrupt)

    with pytest.raises(KeyboardInterrupt):
        run_install(["claude-code", "codex"], yes=True, dry_run=False, home=home,
                    cwd=project, env={}, input_fn=refuse_to_read, stdout=out,
                    stderr=err, replace_file=replace)

    assert claude_json.read_bytes() == original_json
    assert mode_of(claude_json) == 0o644
    assert not (home / ".claude" / "settings.json").exists()
    assert codex_toml.read_bytes() == original_toml
    assert "restored" in err.getvalue() and "removed" in err.getvalue()
    assert "memriver install failed:" not in out.getvalue()
    assert len(backups(home)) == 2


def test_a_failed_rollback_reports_the_exact_paths_and_keeps_the_backups(home,
                                                                        project):
    claude_json = write(home / ".claude.json", json.dumps({"apiKey": SECRET}))
    write(home / ".codex" / "config.toml", 'model = "gpt"\n')
    # 3 fails the apply; 4 is the restore of ~/.claude.json during rollback
    replace = ReplaceSpy(fail_at={3, 4})

    result = install(["claude-code", "codex"], home=home, cwd=project, yes=True,
                     replace=replace)

    backup = next(b for b in backups(home) if b.name.startswith(".claude.json"))
    assert result.exit_code != 0
    assert str(claude_json) in result.stderr
    assert "could not" in result.stderr.lower()
    assert backup.read_text() == json.dumps({"apiKey": SECRET})
    assert SECRET not in result.stderr


def test_rollback_leaves_a_file_another_process_edited_during_the_failed_run(
        home, project):
    """A later target failing must not let rollback stomp a concurrent edit to
    an earlier target: `_roll_back` restoring the backup unconditionally would
    discard whatever another process wrote to that file in the window between
    this run's write and the rollback, and the backup -- taken before this run
    touched the file -- has no way to hold that edit either."""
    claude_json = write(home / ".claude.json", json.dumps({"original": True}))
    original_bytes = claude_json.read_bytes()
    calls: list[int] = []

    def replace(source, destination) -> None:
        calls.append(len(calls) + 1)
        if calls[-1] == 2:
            # simulates another process editing ~/.claude.json in the window
            # between this run's own write (call 1) and the second target's
            # replacement, which then fails
            claude_json.write_text(
                json.dumps({"original": True,
                            "concurrent_foreign_edit": "must survive"}),
                encoding="utf-8")
            raise OSError("injected replacement failure #2")
        os.replace(source, destination)

    result = install(["claude-code"], home=home, cwd=project, yes=True,
                     replace=replace)

    assert result.exit_code == 1
    assert json.loads(claude_json.read_text())["concurrent_foreign_edit"] == (
        "must survive"
    )
    backup = backups(home)[0]
    assert backup.read_bytes() == original_bytes
    assert "concurrent_foreign_edit" not in backup.read_text()
    assert str(claude_json) in result.stderr
    assert "changed after this run wrote it" in result.stderr


def test_rollback_does_not_delete_a_created_file_someone_modified_afterward(
        home, project):
    """The mirror case for a target this run created (no backup exists): a
    later target failing must not let rollback delete that file once another
    process has written to it in the meantime -- there is nothing to restore
    it to, so deleting it would simply lose the concurrent edit."""
    claude_json = write(home / ".claude.json", json.dumps({"original": True}))
    original_json = claude_json.read_bytes()
    write(home / ".codex" / "config.toml", 'model = "gpt"\n')
    settings_json = home / ".claude" / "settings.json"
    calls: list[int] = []

    def replace(source, destination) -> None:
        calls.append(len(calls) + 1)
        if calls[-1] == 3:
            # ~/.claude/settings.json (call 2) was created fresh by this run;
            # this simulates another process writing to it before the third
            # target (codex's config.toml) fails
            settings_json.write_text('{"tampered": true}', encoding="utf-8")
            raise OSError("injected replacement failure #3")
        os.replace(source, destination)

    result = install(["claude-code", "codex"], home=home, cwd=project, yes=True,
                     replace=replace)

    assert result.exit_code != 0
    assert settings_json.exists()
    assert json.loads(settings_json.read_text()) == {"tampered": True}
    assert claude_json.read_bytes() == original_json
    assert str(settings_json) in result.stderr
    assert "changed after this run wrote it" in result.stderr


def test_rollback_refuses_to_touch_a_path_swapped_for_a_symlink(home, project):
    """`_roll_back` must not follow a symlink into place of the target it is
    about to restore: a symlink could point anywhere, and even one pointing
    at a file with byte-identical content is a different path this run never
    wrote to. Reading through it (or restoring/removing it) would touch or
    hide whatever it actually resolves to -- and a FIFO in its place would
    hang the read outright, so the check has to happen before any read, via
    `lstat` rather than `stat`, the same way `_refuse_symlinks` does."""
    claude_json = write(home / ".claude.json", json.dumps({"original": True}))
    elsewhere = home / "elsewhere.json"
    calls: list[int] = []

    def replace(source, destination) -> None:
        calls.append(len(calls) + 1)
        if calls[-1] == 2:
            # swaps ~/.claude.json for a symlink to a file with byte-identical
            # content, in the window between this run's own write (call 1)
            # and the second target's replacement, which then fails
            elsewhere.write_bytes(claude_json.read_bytes())
            claude_json.unlink()
            claude_json.symlink_to(elsewhere)
            raise OSError("injected replacement failure #2")
        os.replace(source, destination)

    result = install(["claude-code"], home=home, cwd=project, yes=True,
                     replace=replace)

    assert result.exit_code == 1
    assert claude_json.is_symlink()
    assert claude_json.resolve() == elsewhere.resolve()
    assert str(claude_json) in result.stderr
    assert "changed after this run wrote it" in result.stderr


def test_rollback_treats_a_created_file_already_deleted_as_undone(home, project):
    """The mirror of the modified-in-place case: once a file this run created
    is already gone by the time rollback reaches it, the state rollback wants
    already holds -- nothing to restore (no backup exists) and nothing to
    remove (it is already absent) -- so this is reported as nothing, not as a
    foreign change, and the directory this run made for it is still reclaimed
    like any other completed rollback."""
    claude_json = write(home / ".claude.json", json.dumps({"original": True}))
    original_json = claude_json.read_bytes()
    write(home / ".codex" / "config.toml", 'model = "gpt"\n')
    settings_json = home / ".claude" / "settings.json"
    calls: list[int] = []

    def replace(source, destination) -> None:
        calls.append(len(calls) + 1)
        if calls[-1] == 3:
            # ~/.claude/settings.json (call 2) was created fresh by this run,
            # including its ~/.claude parent; this simulates another process
            # (or a cleanup of its own) removing it before the third target
            # (codex's config.toml) fails
            settings_json.unlink()
            raise OSError("injected replacement failure #3")
        os.replace(source, destination)

    result = install(["claude-code", "codex"], home=home, cwd=project, yes=True,
                     replace=replace)

    assert result.exit_code != 0
    assert not settings_json.exists()
    assert not settings_json.parent.exists()
    assert claude_json.read_bytes() == original_json
    assert str(settings_json) not in result.stderr
    assert "changed after this run wrote it" not in result.stderr


def test_rollback_refuses_to_touch_a_target_reached_through_a_swapped_parent(
        home, project, tmp_path):
    """A symlink does not have to replace the target's own leaf to redirect
    rollback -- swapping a *parent* directory the leaf path travels through
    does the same thing, and comparing only the leaf's own bytes (even by
    `lstat`) cannot see it: reading or writing `~/.claude/settings.json`
    still resolves `~/.claude` first. Only re-running the same component walk
    `_refuse_symlinks` already does before this run's own write -- this time
    right before rollback touches anything -- catches a parent swapped in
    afterwards. Without it, a byte-identical copy left at the link's target
    makes the stale bytes-comparison alone believe nothing changed, and
    `path.unlink()` (this write created ~/.claude/settings.json, so it has no
    backup) deletes through the link -- someone else's file, not this run's."""
    claude_json = write(home / ".claude.json", json.dumps({"original": True}))
    original_json = claude_json.read_bytes()
    settings_json = home / ".claude" / "settings.json"
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    calls: list[int] = []

    def replace(source, destination) -> None:
        calls.append(len(calls) + 1)
        if calls[-1] == 3:
            # ~/.claude/settings.json (call 2) already holds this run's own
            # bytes; copy them to a foreign location, move the real ~/.claude
            # out of the way, and put a symlink to the foreign directory in
            # its place, before the third target (codex's config.toml) fails
            (foreign / "settings.json").write_bytes(settings_json.read_bytes())
            (home / ".claude").rename(home / ".claude.real")
            (home / ".claude").symlink_to(foreign)
            raise OSError("injected replacement failure #3")
        os.replace(source, destination)

    result = install(["claude-code", "codex"], home=home, cwd=project, yes=True,
                     replace=replace)

    assert result.exit_code != 0
    assert (home / ".claude").is_symlink()
    assert (foreign / "settings.json").exists()  # not deleted through the link
    assert claude_json.read_bytes() == original_json
    assert str(settings_json) in result.stderr
    assert "changed after this run wrote it" in result.stderr


def test_rollback_does_not_restore_a_backup_through_a_swapped_parent(
        home, project, tmp_path):
    """The rewrite half of the same defect: a target this run rewrote (so a
    backup exists) must not have that backup restored *through* a parent
    swapped for a symlink after this run's own write landed -- that would
    silently overwrite whatever the link now points at instead of the file
    this run actually touched."""
    claude_json = write(home / ".claude.json", json.dumps({"original": True}))
    original_json = claude_json.read_bytes()
    codex_toml = write(home / ".codex" / "config.toml", 'model = "gpt"\n')
    original_toml = codex_toml.read_bytes()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    calls: list[int] = []
    destinations: list[Path] = []
    rewritten_bytes: list[bytes] = []

    def replace(source, destination) -> None:
        calls.append(len(calls) + 1)
        destinations.append(Path(destination))
        if calls[-1] == 4:
            # ~/.codex/config.toml (call 3) already holds this run's own
            # rewritten bytes, and its sibling backup this run wrote still
            # holds the pre-run bytes; copy both to a foreign location (so a
            # restore that follows the link can still find "its" backup),
            # move the real ~/.codex out of the way, and put a symlink to the
            # foreign directory in its place, before the fourth target
            # (codex's hooks.json) fails
            codex_dir = home / ".codex"
            backup = next(codex_dir.glob("config.toml.memriver-backup-*"))
            rewritten_bytes.append(codex_toml.read_bytes())
            (foreign / "config.toml").write_bytes(rewritten_bytes[0])
            (foreign / backup.name).write_bytes(backup.read_bytes())
            codex_dir.rename(home / ".codex.real")
            codex_dir.symlink_to(foreign)
            raise OSError("injected replacement failure #4")
        os.replace(source, destination)

    result = install(["claude-code", "codex"], home=home, cwd=project, yes=True,
                     replace=replace)

    assert result.exit_code != 0
    assert (home / ".codex").is_symlink()
    assert rewritten_bytes[0] != original_toml  # this run did change it
    # not restored through the link: the backup's pre-run bytes never landed
    # on the foreign file the link now points at
    assert (foreign / "config.toml").read_bytes() == rewritten_bytes[0]
    # the only replace_file call after the injected failure restores
    # ~/.claude.json (untouched); none targets ~/.codex/config.toml, which
    # would only be reachable by writing through the symlink
    assert destinations[4:] == [claude_json]
    assert claude_json.read_bytes() == original_json
    assert str(codex_toml) in result.stderr
    assert "changed after this run wrote it" in result.stderr


def test_rollback_reports_could_not_recover_when_the_link_check_itself_fails(
        home, project, monkeypatch, tmp_path):
    """A transient `OSError` from the symlink-component check's own `lstat`
    -- as opposed to that check actually finding a link -- must not be read
    as "no link here, safe to act": failing open would defeat the very check
    this guards, since a parent really has been swapped for a symlink at this
    point and the leaf alone (even by `lstat`) cannot see it. It is reported
    as an ordinary recovery failure instead, exactly like any other
    unexpected error hit while undoing a write, and the file -- now reachable
    only through the link -- is left exactly as it is. Being inside the
    per-write loop, this failing one write does not stop any other write in
    the same run from rolling back normally."""
    claude_json = write(home / ".claude.json", json.dumps({"original": True}))
    original_json = claude_json.read_bytes()
    settings_json = home / ".claude" / "settings.json"
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    calls: list[int] = []

    def replace(source, destination) -> None:
        calls.append(len(calls) + 1)
        if calls[-1] == 3:
            # swaps ~/.claude for a symlink to a foreign directory holding a
            # byte-identical copy of settings.json (call 2), before the third
            # target (codex's config.toml) fails
            (foreign / "settings.json").write_bytes(settings_json.read_bytes())
            (home / ".claude").rename(home / ".claude.real")
            (home / ".claude").symlink_to(foreign)
            raise OSError("injected replacement failure #3")
        os.replace(source, destination)

    claude_dir = home / ".claude"
    real_lstat = Path.lstat
    fired = {"done": False}

    def flaky_lstat(self, *args, **kwargs):
        # fires exactly once, and only once ~/.claude is genuinely a symlink
        # -- i.e. the first time rollback's own component check looks at it,
        # never during planning or this run's own write, when it is still an
        # ordinary directory (or does not exist yet)
        if self == claude_dir and not fired["done"] and os.path.islink(self):
            fired["done"] = True
            raise OSError(errno.EIO, "injected transient I/O error")
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", flaky_lstat)

    result = install(["claude-code", "codex"], home=home, cwd=project, yes=True,
                     replace=replace)

    assert result.exit_code != 0
    assert (foreign / "settings.json").exists()  # not deleted through the link
    assert "COULD NOT recover" in result.stderr
    assert str(settings_json) in result.stderr
    # a different write in the same run still rolls back normally
    assert claude_json.read_bytes() == original_json


def test_success_reports_backup_paths_and_restore_commands_never_contents(home,
                                                                         project):
    write(home / ".claude.json", json.dumps({"apiKey": SECRET}))

    result = install(["claude-code"], home=home, cwd=project, yes=True)

    backup = backups(home)[0]
    assert result.exit_code == 0
    assert str(backup) in result.stdout
    assert f"cp -p -- {backup} {home / '.claude.json'}" in result.stdout
    assert SECRET not in result.stdout


def test_restore_commands_quote_paths_that_need_quoting(tmp_path, project):
    home = tmp_path / "home dir"
    home.mkdir()
    write(home / ".claude.json", json.dumps({"mcpServers": {}}))

    result = install(["claude-code"], home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    assert f"'{backups(home)[0]}'" in result.stdout


def test_a_new_target_reports_the_managed_file_to_remove_instead_of_a_backup(home,
                                                                            project):
    result = install(["kiro"], home=home, cwd=project, yes=True)

    steering = project / ".kiro" / "steering" / "memriver.md"
    assert result.exit_code == 0
    assert steering.exists() and backups(project) == []
    assert "remove .kiro/steering/memriver.md" in result.stdout


def test_a_crlf_target_keeps_every_foreign_newline_byte_for_byte(home, project):
    """Reading a snapshot in universal-newline mode turned every CRLF in the
    file into an LF, so accepting one managed change rewrote lines the summary
    never showed. Only the managed region may differ after an install."""
    agents = project / "AGENTS.md"
    agents.write_bytes(b"# foreign\r\nkeep\r\n")
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_bytes(b'model = "gpt"\r\nforeign = "keep"\r\n')

    result = install(["codex", "cursor"], home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    assert agents.read_bytes().startswith(b"# foreign\r\nkeep\r\n")
    assert config.read_bytes().startswith(b'model = "gpt"\r\nforeign = "keep"\r\n')


def test_codex_success_states_the_trust_step(home, project):
    result = install(["codex"], home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    assert CODEX_TRUST_TEXT in result.stdout
    assert CODEX_HOOKS_DISABLED_NOTE not in result.stdout


def test_codex_says_so_when_the_hooks_feature_is_switched_off(home, project):
    """`features.hooks = false` disables every Codex hook at the feature level,
    so the definitions this run writes never fire and /hooks cannot re-enable
    them. Reporting `installed:` and the trust step alone is a silent partial
    install; memriver says what is off rather than flipping the user's choice."""
    write(home / ".codex" / "config.toml",
          "[features]\nhooks = false\nmemories = false\n")

    result = install(["codex"], home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    assert CODEX_HOOKS_DISABLED_NOTE in result.stdout


def test_codex_hooks_disabled_note_never_claims_a_declined_hook_was_installed(
        home, project):
    """The note was planned from the config alone, but consent is per change:
    accepting the MCP registration and declining both hooks leaves
    `~/.codex/hooks.json` without a single memriver definition. Telling that
    user to enable `features.hooks` and trust the hooks via /hooks is a
    remediation for hooks nobody installed."""
    write(home / ".codex" / "config.toml",
          "[features]\nhooks = false\nmemories = false\n")

    result = install(["codex"], home=home, cwd=project, yes=False,
                     replies=["y", "n", "n"])

    assert result.exit_code == 0
    assert not (home / ".codex" / "hooks.json").exists()
    assert CODEX_HOOKS_DISABLED_NOTE not in result.stdout
    assert CODEX_NO_DEFINITIONS_NOTE in result.stdout


def test_codex_hooks_disabled_note_stands_when_the_definitions_already_exist(
        home, project):
    """The other half of the same question: a run that changes no hook leaves
    the definitions an earlier run wrote, so the feature flag really is the
    only thing between the user and a working hook."""
    write(home / ".codex" / "config.toml",
          "[features]\nhooks = false\nmemories = false\n")
    write(home / ".codex" / "hooks.json", json.dumps({"hooks": {
        "SessionStart": [hook_group(
            "uvx memriver hook session-start --harness codex")],
        "Stop": [hook_group("uvx memriver hook stop --harness codex")],
    }}))

    result = install(["codex"], home=home, cwd=project, yes=False,
                     replies=["n"])

    assert result.exit_code == 0
    assert CODEX_HOOKS_DISABLED_NOTE in result.stdout
    assert CODEX_NO_DEFINITIONS_NOTE not in result.stdout


def test_codex_hooks_disabled_note_does_not_claim_completeness_for_one_accepted_hook(
        home, project):
    """Confirmation is per hook, so a user can accept SessionStart and decline
    Stop. The wrong-but-not-silent claim was that hooks.json holds *no*
    memriver definition at all -- untrue here, it holds exactly one."""
    write(home / ".codex" / "config.toml",
          "[features]\nhooks = false\nmemories = false\n")

    result = install(["codex"], home=home, cwd=project, yes=False,
                     replies=["n", "y", "n"])

    assert result.exit_code == 0
    hooks = json.loads((home / ".codex" / "hooks.json").read_text())["hooks"]
    assert "SessionStart" in hooks and "Stop" not in hooks
    assert CODEX_HOOKS_DISABLED_NOTE not in result.stdout
    assert CODEX_NO_DEFINITIONS_NOTE in result.stdout
    assert "holds no memriver hook definition" not in result.stdout


def test_codex_hooks_disabled_note_does_not_claim_completeness_for_the_other_hook(
        home, project):
    """The other half of the same per-hook confirmation: Stop accepted,
    SessionStart declined."""
    write(home / ".codex" / "config.toml",
          "[features]\nhooks = false\nmemories = false\n")

    result = install(["codex"], home=home, cwd=project, yes=False,
                     replies=["n", "n", "y"])

    assert result.exit_code == 0
    hooks = json.loads((home / ".codex" / "hooks.json").read_text())["hooks"]
    assert "Stop" in hooks and "SessionStart" not in hooks
    assert CODEX_HOOKS_DISABLED_NOTE not in result.stdout
    assert CODEX_NO_DEFINITIONS_NOTE in result.stdout
    assert "holds no memriver hook definition" not in result.stdout


def test_codex_hooks_disabled_note_does_not_claim_completeness_when_a_stale_takeover_is_declined(
        home, project):
    """A pre-existing memriver Stop definition that no longer matches what
    this version would install is a takeover, confirmed like any other change.
    Declining it leaves the stale definition in place -- not absent, and not
    the complete expected pair either -- so the note may claim neither
    "installed" nor "no definition at all"."""
    write(home / ".codex" / "config.toml",
          "[features]\nhooks = false\nmemories = false\n")
    write(home / ".codex" / "hooks.json", json.dumps({"hooks": {
        "SessionStart": [hook_group(
            "uvx memriver hook session-start --harness codex")],
        "Stop": [hook_group(
            "uvx memriver hook stop --harness codex --stale-extra-flag")],
    }}))

    result = install(["codex"], home=home, cwd=project, yes=False,
                     replies=["y", "n"])

    assert result.exit_code == 0
    stop_command = json.loads(
        (home / ".codex" / "hooks.json").read_text(),
    )["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert stop_command.endswith("--stale-extra-flag")  # declined, unchanged
    assert CODEX_HOOKS_DISABLED_NOTE not in result.stdout
    assert CODEX_NO_DEFINITIONS_NOTE in result.stdout
    assert "holds no memriver hook definition" not in result.stdout


def test_installing_all_four_harnesses_writes_every_target(home, project):
    result = install(ALL_HARNESSES, home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    for path in (home / ".claude.json", home / ".claude" / "settings.json",
                 home / ".codex" / "config.toml", home / ".codex" / "hooks.json",
                 home / ".cursor" / "mcp.json", project / "AGENTS.md",
                 home / ".kiro" / "settings" / "mcp.json",
                 project / ".kiro" / "steering" / "memriver.md"):
        assert path.exists(), path
    assert json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"]
    assert tomlkit.parse((home / ".codex" / "config.toml").read_text())["mcp_servers"]


# --- install is decoupled from the project bindings ---------------------------
#
# The static file still lands on the nearest git root, and the bound directories
# still decide the project identity the MCP server reports. Neither reads the other:
# these four cases cross the two axes (registered or not, git root or not) and
# pin that the outcomes stay independent.

STATIC_FILE = {"cursor": Path("AGENTS.md"),
               "kiro": Path(".kiro") / "steering" / "memriver.md"}


def _server_header(store: Path, directory: Path) -> str:
    """The first line of ``memory_index`` from a real server bound to ``directory``."""
    import asyncio

    from fastmcp import Client
    from memriver.server import build_server

    server = build_server(root=store, project_dir=directory)

    async def probe():
        async with Client(server) as c:
            return (await c.call_tool("memory_index", {})).data.splitlines()[0]

    return asyncio.run(probe())


@pytest.mark.parametrize("harness", ["cursor", "kiro"])
def test_install_in_unregistered_repo_lands_on_git_root_and_registers_nothing(
        home, project, harness):
    store = home / "agent-memory"
    (project / "src").mkdir()

    result = install([harness], home=home, cwd=project / "src")

    assert result.exit_code == 0
    assert (project / STATIC_FILE[harness]).exists()
    assert not (store / "memriver.db").exists()
    assert _server_header(store, project / "src").startswith("project: none")


@pytest.mark.parametrize("harness", ["cursor", "kiro"])
def test_install_under_registered_parent_lands_in_each_repo(home, tmp_path, harness):
    parent = tmp_path / "work"
    (parent / "frontend" / ".git").mkdir(parents=True)
    (parent / "backend" / ".git").mkdir(parents=True)
    store = home / "agent-memory"
    pid = _bind_new(store, parent, "work")

    result = install([harness], home=home, cwd=parent / "frontend")

    assert result.exit_code == 0
    assert (parent / "frontend" / STATIC_FILE[harness]).exists()
    assert not (parent / STATIC_FILE[harness]).exists()
    assert _server_header(store, parent / "frontend").startswith(f"project: work [{pid}]")


@pytest.mark.parametrize("harness", ["cursor", "kiro"])
def test_registered_child_keeps_git_root_install_and_its_own_identity(home, tmp_path,
                                                                      harness):
    parent = tmp_path / "work"
    (parent / "frontend" / ".git").mkdir(parents=True)
    (parent / "frontend" / "src").mkdir()
    store = home / "agent-memory"
    _bind_new(store, parent, "work")
    child = _bind_new(store, parent / "frontend", "frontend")

    result = install([harness], home=home, cwd=parent / "frontend" / "src")

    assert result.exit_code == 0
    assert (parent / "frontend" / STATIC_FILE[harness]).exists()
    assert _server_header(store, parent / "frontend" / "src").startswith(f"project: frontend [{child}]")


@pytest.mark.parametrize("harness", ["cursor", "kiro"])
def test_install_from_registered_non_git_parent_keeps_the_no_git_root_outcome(
        home, tmp_path, harness):
    parent = tmp_path / "work"
    parent.mkdir()
    store = home / "agent-memory"
    _bind_new(store, parent, "work")

    result = install([harness], home=home, cwd=parent)

    # being registered is not being a repository: the same refusal the
    # unregistered no-git-root case gets, and nothing written
    assert result.exit_code == 1
    assert result.stderr.rstrip("\n").endswith(
        "run install inside a project or pass one"), result.stderr
    assert not (parent / STATIC_FILE[harness]).exists()


# --- the memory store is required to continue installation --------------------

STORE_FAILURE_TEXT = ("\nmemriver install: the memory store could not be initialized; "
                      "no harness configuration was applied. Run memriver doctor.\n")

class FakeStore:
    """A StoreStep whose apply records when it ran and what existed then."""

    def __init__(self, home: Path, fail: bool = False) -> None:
        self.home = home
        self.fail = fail
        self.applied_with: list[dict] = []

    def step(self) -> StoreStep:
        return StoreStep(summary="memory store (required): create the global project in /store",
                         label="memory store in /store", apply=self.apply)

    def apply(self) -> str:
        self.applied_with.append(snapshot_tree(self.home))
        if self.fail:
            raise OSError("disk full at /secret/path")
        return "memory store: ready (global project aaaaaaaaaa)"


def _store_first(prompt: str) -> str:
    return "y" if prompt.startswith("initialize memory store") else "n"


def test_the_store_change_is_shown_with_the_plan_before_the_first_prompt(home, project):
    store = FakeStore(home)
    result = install(["claude-code"], home=home, cwd=project, yes=False,
                     input_fn=_store_first, store_step=store.step())
    assert result.stdout.index("memory store (required): create the global project in /store") \
        < result.stdout.index("memory store: ready")
    assert len(store.applied_with) == 1
    assert result.stderr == ""


def test_the_store_prompt_comes_first_and_makes_cancellation_explicit(home, project):
    result = install(["claude-code"], home=home, cwd=project, yes=False, replies=["n"],
                     store_step=FakeStore(home).step())
    assert result.answers.prompts == ["initialize memory store in /store and continue installation? [y/N] "]
    assert "memory store (required): create the global project in /store" in \
        result.answers.output_at_first_prompt


def test_the_store_runs_before_any_harness_file_is_written(home, project):
    before = snapshot_tree(home)
    store = FakeStore(home)
    result = install(["claude-code"], home=home, cwd=project, yes=True,
                     store_step=store.step())
    assert result.exit_code == 0
    assert store.applied_with == [before]          # nothing under home changed yet
    assert snapshot_tree(home) != before           # the harness changes landed after


def test_dry_run_shows_the_store_change_and_writes_nothing(home, project):
    before = snapshot_tree(home)
    store = FakeStore(home)
    result = install(["claude-code"], home=home, cwd=project, dry_run=True,
                     store_step=store.step())
    assert "memory store (required): create the global project in /store" in result.stdout
    assert store.applied_with == [] and snapshot_tree(home) == before


def test_declining_the_store_applies_no_harness_change(home, project):
    before = snapshot_tree(home)
    store = FakeStore(home)
    result = install(["claude-code"], home=home, cwd=project, yes=False, replies=["n"],
                     store_step=store.step())
    assert result.exit_code == 1
    assert "installation cancelled; memory store not initialized; no file was changed." in result.stdout
    assert result.stderr == ""
    assert store.applied_with == [] and snapshot_tree(home) == before


def test_piped_input_never_initializes_the_store_but_yes_does(home, project):
    before = snapshot_tree(home)
    store = FakeStore(home)
    result = install(["claude-code"], home=home, cwd=project, yes=False, replies=[],
                     store_step=store.step(), stdin_is_tty=False)
    assert result.exit_code == 1 and "re-run with --yes" in result.stderr
    assert "re-run with --yes" not in result.stdout
    assert store.applied_with == [] and snapshot_tree(home) == before
    assert result.answers.prompts == []
    result = install(["claude-code"], home=home, cwd=project, yes=True,
                     store_step=store.step(), stdin_is_tty=False)
    assert result.exit_code == 0 and len(store.applied_with) == 1


def test_a_failing_store_applies_no_harness_change(home, project):
    before = snapshot_tree(home)
    result = install(["claude-code"], home=home, cwd=project, yes=True,
                     store_step=FakeStore(home, fail=True).step())
    assert result.exit_code == 1
    assert result.stderr == STORE_FAILURE_TEXT
    assert "could not be initialized" not in result.stdout
    assert "/secret/path" not in result.stdout + result.stderr
    assert snapshot_tree(home) == before


def test_a_planning_failure_in_the_accepted_edits_leaves_no_store(home, project, monkeypatch):
    import memriver.install as install_module

    def refuse(*_args, **_kwargs):
        raise install_module.PlanningError("target changed shape")

    monkeypatch.setattr(install_module, "_rendered", refuse)
    store = FakeStore(home)
    result = install(["claude-code"], home=home, cwd=project, yes=True,
                     store_step=store.step())
    assert result.exit_code == 1 and store.applied_with == []
    assert "target changed shape" in result.stderr
    assert "target changed shape" not in result.stdout


def test_store_accepted_but_every_harness_change_declined(home, project):
    before = snapshot_tree(home)
    store = FakeStore(home)
    result = install(["claude-code"], home=home, cwd=project, yes=False,
                     input_fn=_store_first, store_step=store.step())
    assert result.exit_code == 0 and len(store.applied_with) == 1
    assert "no harness change accepted; no harness file was changed." in result.stdout
    assert snapshot_tree(home) == before


def test_only_the_store_pending_is_still_a_change_that_needs_consent(home, project):
    install(["claude-code"], home=home, cwd=project, yes=True)     # harness up to date
    store = FakeStore(home)
    result = install(["claude-code"], home=home, cwd=project, yes=False, replies=["y"],
                     store_step=store.step())
    assert "already up to date" not in result.stdout
    assert result.answers.prompts == ["initialize memory store in /store and continue installation? [y/N] "]
    assert result.exit_code == 0 and len(store.applied_with) == 1


def test_without_a_store_step_an_up_to_date_install_asks_nothing(home, project):
    install(["claude-code"], home=home, cwd=project, yes=True)
    result = install(["claude-code"], home=home, cwd=project, yes=False, replies=[])
    assert "already up to date" in result.stdout and result.answers.prompts == []


@pytest.mark.parametrize("failure", ["planning", "apply"])
def test_install_failures_use_stderr_without_a_store_step(home, project, failure):
    if failure == "planning":
        write(home / ".claude.json", "{not json")
        result = install(["claude-code"], home=home, cwd=project)
        marker = "memriver install:"
        assert result.stdout == ""
    else:
        result = install(["claude-code"], home=home, cwd=project,
                         replace=ReplaceSpy(fail_at={1}))
        marker = "memriver install failed:"
        assert "backups were kept" in result.stderr
        assert "backups were kept" not in result.stdout
    assert result.exit_code == 1
    assert marker in result.stderr and marker not in result.stdout


def test_success_without_a_store_step_keeps_stderr_empty(home, project):
    result = install(["claude-code"], home=home, cwd=project)
    assert result.exit_code == 0 and result.stdout
    assert result.stderr == ""
