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

import io
import json
import os
import stat
from pathlib import Path

import pytest
import tomlkit
from memriver.install import TAKEOVER_NOTICE, claude_code, cursor, run_install

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
    def __init__(self, exit_code: int, stdout: str, answers: Answers | None,
                 replace: ReplaceSpy) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.answers = answers
        self.replace = replace


def install(harnesses, *, home: Path, cwd: Path, yes: bool = True,
            dry_run: bool = False, env: dict | None = None, replies=None,
            input_fn=None, replace: ReplaceSpy | None = None) -> Run:
    out = io.StringIO()
    replace = replace if replace is not None else ReplaceSpy()
    answers = None
    if input_fn is None:
        answers = Answers(replies or [], out)
        input_fn = answers
    exit_code = run_install(
        harnesses, yes=yes, dry_run=dry_run, home=home, cwd=cwd,
        env=env if env is not None else {}, input_fn=input_fn, stdout=out,
        replace_file=replace,
    )
    return Run(exit_code, out.getvalue(), answers, replace)


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


def all_outside_a_project(home: Path, project: Path):
    elsewhere = home.parent / "elsewhere"
    elsewhere.mkdir()
    return ALL_HARNESSES, elsewhere


PREFLIGHT_FAILURES = [
    malformed_json,
    malformed_toml,
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


def test_a_planning_failure_reports_the_reason_rather_than_a_fake_change(home,
                                                                        project):
    write(home / ".claude.json", "{ this is not json")

    result = install(["claude-code"], home=home, cwd=project)

    assert result.exit_code != 0
    assert "not valid JSON" in result.stdout
    assert "installed" not in result.stdout


def test_incompatible_duplicate_target_declarations_are_rejected(tmp_path, home,
                                                                 project,
                                                                 monkeypatch):
    """The same path claimed as user-level by one harness and project-level by
    another is an unresolvable classification, not a merge."""
    claude_config, _ = claude_code.targets(home, None)
    real_targets = cursor.targets

    def clashing_targets(home_dir, project_root):
        mcp, instructions = real_targets(home_dir, project_root)
        return type(mcp)(path=claude_config.path, user_level=False,
                         rollback_instruction=mcp.rollback_instruction), instructions

    monkeypatch.setattr(cursor, "targets", clashing_targets)
    write(claude_config.path, "{}")
    before_tree = snapshot_tree(tmp_path)

    result = install(["claude-code", "cursor"], home=home, cwd=project)

    assert result.exit_code != 0
    assert str(claude_config.path) in result.stdout
    assert snapshot_tree(tmp_path) == before_tree
    assert result.replace.calls == []


def test_an_unknown_harness_name_fails_before_any_target_is_read(tmp_path, home,
                                                                project):
    before_tree = snapshot_tree(tmp_path)

    result = install(["not-a-harness"], home=home, cwd=project)

    assert result.exit_code != 0
    assert "not-a-harness" in result.stdout
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
    assert "register memriver MCP server" in joined
    assert "disable built-in auto memory" in joined


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
    assert "--yes" in result.stdout
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


def test_a_reinstall_that_changes_nothing_reports_it_and_prompts_for_nothing(home,
                                                                            project):
    install(["claude-code"], home=home, cwd=project, yes=True)
    before_tree = snapshot_tree(home)

    result = install(["claude-code"], home=home, cwd=project, yes=False,
                     env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
                     input_fn=refuse_to_read)

    assert result.exit_code == 0
    assert "already up to date" in result.stdout
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
    assert "restored" in result.stdout and str(claude_json) in result.stdout
    assert "removed" in result.stdout


def test_an_interrupt_rolls_the_run_back_and_still_propagates(home, project):
    """Ctrl-C between replacements must not leave a half-applied tree behind."""
    claude_json = write(home / ".claude.json", json.dumps({"apiKey": SECRET}),
                        mode=0o644)
    codex_toml = write(home / ".codex" / "config.toml", 'model = "gpt"\n')
    original_json, original_toml = claude_json.read_bytes(), codex_toml.read_bytes()
    out = io.StringIO()
    replace = ReplaceSpy(fail_at={3}, raises=KeyboardInterrupt)

    with pytest.raises(KeyboardInterrupt):
        run_install(["claude-code", "codex"], yes=True, dry_run=False, home=home,
                    cwd=project, env={}, input_fn=refuse_to_read, stdout=out,
                    replace_file=replace)

    assert claude_json.read_bytes() == original_json
    assert mode_of(claude_json) == 0o644
    assert not (home / ".claude" / "settings.json").exists()
    assert codex_toml.read_bytes() == original_toml
    assert "restored" in out.getvalue() and "removed" in out.getvalue()
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
    assert str(claude_json) in result.stdout
    assert "could not" in result.stdout.lower()
    assert backup.read_text() == json.dumps({"apiKey": SECRET})
    assert SECRET not in result.stdout


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


def test_codex_success_states_the_trust_step(home, project):
    result = install(["codex"], home=home, cwd=project, yes=True)

    assert result.exit_code == 0
    assert CODEX_TRUST_TEXT in result.stdout


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
