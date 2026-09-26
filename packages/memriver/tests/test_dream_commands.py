"""`memriver dream ...` over a tmp store, a fake launchctl, fake executables and a
scripted executor -- never the real harnesses, LaunchAgents or store."""

from __future__ import annotations

import io
import json
import os
import plistlib
import shlex
import signal
import sqlite3
import subprocess
import sys
import tomllib
from contextlib import closing
from pathlib import Path

import pytest
from memriver import dream_commands
from memriver.executors import make_executor
from memriver.launch_agent import plist_path
from memriver_core.bootstrap import build_maintenance_service, build_service
from memriver_core.models import ChangeGroup, CreateOp, new_id, now
from memriver_core.settings import Settings, SettingsError
from memriver_dream.lock import run_lock
from memriver_dream.protocols import ExecutorResult
from memriver_dream.settings import DREAM_LAUNCH_AGENT_LABEL, load_dream_settings

SECRET = "token ghp_" + "a" * 36


class Launchctl:
    """launchd in miniature: the labels of the loaded jobs, each job on its own."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.loaded: set[str] = set()

    def __call__(self, args: list[str]) -> int:
        self.calls.append(list(args))
        if args[0] == "print":                       # print gui/<uid>/<label>
            return 0 if args[1].rpartition("/")[2] in self.loaded else 113
        label = Path(args[2]).stem                   # bootout|bootstrap gui/<uid> <plist>
        if args[0] == "bootstrap":
            self.loaded.add(label)
        else:
            self.loaded.discard(label)
        return 0


class Executor:
    name, harness = "fake", "fake-harness"

    def run(self, *, system_prompt, prompt, schema, timeout_s):
        return ExecutorResult(value={"groups": []} if "groups" in schema["properties"]
                              else {"decision": "keep", "reason": "fine", "evidence": []})


def _initialized(store: Path, home: Path, work: Path):
    service = build_service(Settings(root=store), root=store, home=home)
    service.ensure_global()
    return service, service.init_project("demo", service.plan_root(str(work)))


@pytest.fixture
def world(tmp_path, monkeypatch):
    for key in [k for k in os.environ if k.startswith("MEMRIVER_")]:
        monkeypatch.delenv(key, raising=False)
    store, home, work, bin_dir = (tmp_path / name for name in ("store", "home", "work", "bin"))
    for directory in (home, work, bin_dir):
        directory.mkdir()
    for name in ("claude", "codex", "memriver"):
        (bin_dir / name).write_text("#!/bin/sh\n")
        (bin_dir / name).chmod(0o755)
    service, project = _initialized(store, home, work)
    (store / "settings.toml").write_text("max_body_chars = 4000\n", encoding="utf-8")
    return {"tmp": tmp_path, "store": store, "home": home, "work": work, "bin": bin_dir,
            "service": service, "project": project, "launchctl": Launchctl(),
            "which": lambda name: str(bin_dir / name) if name in ("claude", "codex") else None}


def _out(fn, *args, **kwargs) -> tuple[int, str]:
    out = io.StringIO()
    code = fn(*args, stdout=out, **kwargs)
    return code, out.getvalue()


def _init(world, *, platform="darwin", **options):
    values = {"executor": None, "ttl_days": None, "at": None, "yes": True,
              "root": world["store"], "stdin_is_tty": False, "input_fn": input,
              "home": world["home"], "env": {}, "which": world["which"],
              "launchctl": world["launchctl"], "platform": platform, "uid": 501,
              "memriver_path": str(world["bin"] / "memriver")}
    return _out(dream_commands.run_init, **(values | options))


def _settings(world) -> dict:
    return tomllib.loads((world["store"] / "settings.toml").read_text(encoding="utf-8"))


def test_init_writes_the_dream_table_keeps_the_rest_and_installs_the_agent(world):
    code, out = _init(world)
    assert code == 0
    written = _settings(world)
    assert written["max_body_chars"] == 4000
    assert written["dream"] == {"executor": "claude",
                                "executor_path": str(world["bin"] / "claude"),
                                "ttl_days": 90, "schedule_at": "04:00"}
    agent = plistlib.loads(plist_path(world["home"]).read_bytes())
    assert agent["ProgramArguments"] == [str(world["bin"] / "memriver"), "dream", "run",
                                         "--trigger", "schedule"]
    assert agent["StartCalendarInterval"] == {"Hour": 4, "Minute": 0}
    assert agent["EnvironmentVariables"]["PATH"] == f"{world['bin']}:/usr/bin:/bin"
    assert agent["EnvironmentVariables"]["MEMRIVER_ROOT"] == str(world["store"])
    assert agent["StandardOutPath"] == str(world["store"] / "dream" / "dream.log")
    assert [call[0] for call in world["launchctl"].calls] == ["print", "bootstrap"]
    for fragment in ("dream run", "dream.log", "memriver dream uninstall", "04:00"):
        assert fragment in out


def test_init_is_idempotent_and_replaces_the_schedule(world):
    _init(world)
    code, _ = _init(world, executor="codex", ttl_days=30, at="05:30")
    assert code == 0
    assert _settings(world)["dream"] == {"executor": "codex",
                                         "executor_path": str(world["bin"] / "codex"),
                                         "ttl_days": 30, "schedule_at": "05:30"}
    assert plistlib.loads(plist_path(world["home"]).read_bytes())[
        "StartCalendarInterval"] == {"Hour": 5, "Minute": 30}
    assert "bootout" in [call[0] for call in world["launchctl"].calls]
    code, _ = _init(world, executor="codex", ttl_days=30, at="05:30")
    assert code == 0 and _settings(world)["dream"]["ttl_days"] == 30


def test_a_relative_root_is_stored_absolute_and_a_run_from_elsewhere_uses_that_store(
        world, monkeypatch):
    monkeypatch.chdir(world["tmp"])
    code, _ = _init(world, root=Path("store"))
    assert code == 0
    env = plistlib.loads(plist_path(world["home"]).read_bytes())["EnvironmentVariables"]
    assert env["MEMRIVER_ROOT"] == str(world["store"])
    secret = _plant(world, SECRET)
    monkeypatch.chdir(world["work"])                     # launchd's cwd is not the user's
    code, _, _ = _run(world, root=Path(env["MEMRIVER_ROOT"]))
    assert code == 0
    assert world["service"].show(secret, include_deleted=True).deleted_at is not None


def _fresh_dir(tmp_path: Path, name: str) -> Path:
    directory = tmp_path / name
    directory.mkdir()
    return directory


def test_init_elsewhere_prints_a_command_that_works_with_a_spaced_root(tmp_path, world):
    spaced, home = tmp_path / "my store", world["home"]
    _initialized(spaced, home, _fresh_dir(tmp_path, "w2"))    # a second store, with a space
    stand_in = tmp_path / "memriver-stand-in"
    stand_in.write_text(f"#!{sys.executable}\nimport os\nprint(os.environ['MEMRIVER_ROOT'])\n",
                        encoding="utf-8")
    stand_in.chmod(0o755)
    code, out = _init(world, platform="linux", root=spaced, memriver_path=str(stand_in))
    assert code == 0 and not plist_path(home).exists() and world["launchctl"].calls == []
    assert "schedule this command daily at 04:00" in out
    command = shlex.split(out.strip().splitlines()[-1])
    assert command[:2] == ["env", f"MEMRIVER_ROOT={spaced}"]
    ran = subprocess.run(command, capture_output=True, text=True, check=True)
    assert ran.stdout.strip() == str(spaced)


@pytest.mark.parametrize(("options", "fragment"), [
    ({"which": lambda name: None}, "neither claude nor codex is on PATH"),
    ({"executor": "codex", "which": lambda name: None}, "codex is not on PATH"),
    ({"at": "25:00"}, "refused: invalid value given for schedule_at; nothing was written"),
    ({"yes": False}, "stdin is not a terminal"),
])
def test_init_refusals_write_nothing(world, options, fragment):
    code, out = _init(world, **options)
    assert code == 2 and fragment in out
    assert "dream" not in _settings(world) and not plist_path(world["home"]).exists()


def test_init_refuses_an_uninitialized_store(world, tmp_path):
    code, out = _init(world, root=tmp_path / "empty")
    assert code == 2 and "run memriver install first" in out


def test_init_refuses_a_memriver_inside_uvs_cache(world, tmp_path):
    cached = tmp_path / "cache" / "archive-v0" / "abc" / "bin" / "memriver"
    cached.parent.mkdir(parents=True)
    cached.write_text("#!/bin/sh\n")
    cached.chmod(0o755)
    code, out = _init(world, memriver_path=str(cached))
    assert code == 2 and "uv tool install memriver" in out
    assert not plist_path(world["home"]).exists()


def test_init_refuses_an_invalid_key_it_does_not_own(world):
    before = "max_body_chars = 4000\n[dream]\nuncertain_limit = 0\n"
    (world["store"] / "settings.toml").write_text(before, encoding="utf-8")
    # the file's own bad value is the settings error cli.main prints on stderr
    with pytest.raises(SettingsError, match=r"^settings\.toml is invalid: field "
                                             r"dream\.uncertain_limit$"):
        _init(world)
    assert (world["store"] / "settings.toml").read_text(encoding="utf-8") == before


def test_init_replaces_every_case_variant_of_the_keys_it_owns(world):
    # the run matches keys case-insensitively, first spelling winning: an upper-case
    # key left beside the one init writes would silently keep the old value
    (world["store"] / "settings.toml").write_text(
        "max_body_chars = 4000\n[dream]\nEXECUTOR = 'claude'\nEXECUTOR_PATH = '/old/claude'\n"
        "TTL_DAYS = 90\nSchedule_At = '04:00'\nnot_a_key = 1\n", encoding="utf-8")
    code, _ = _init(world, executor="codex", ttl_days=30, at="05:30")
    assert code == 0
    dream = load_dream_settings(world["store"])
    assert (dream.executor, dream.executor_path, dream.ttl_days, dream.schedule_at) == (
        "codex", str(world["bin"] / "codex"), 30, "05:30")
    assert set(_settings(world)["dream"]) == {"executor", "executor_path", "ttl_days",
                                              "schedule_at", "not_a_key"}
    assert plistlib.loads(plist_path(world["home"]).read_bytes())[
        "StartCalendarInterval"] == {"Hour": 5, "Minute": 30}


def test_init_replaces_an_invalid_upper_case_key_it_owns(world):
    (world["store"] / "settings.toml").write_text(
        "max_body_chars = 4000\n[dream]\nEXECUTOR = 'gpt'\n", encoding="utf-8")
    with pytest.raises(SettingsError, match="field dream.executor"):
        load_dream_settings(world["store"])
    assert _init(world)[0] == 0
    assert load_dream_settings(world["store"]).executor == "claude"
    assert "EXECUTOR" not in _settings(world)["dream"]


def test_init_keeps_an_unknown_key_which_every_reader_ignores(world):
    (world["store"] / "settings.toml").write_text(
        "max_body_chars = 4000\n[dream]\nnot_a_key = 1\n", encoding="utf-8")
    assert _init(world)[0] == 0
    assert _settings(world)["dream"]["not_a_key"] == 1
    assert load_dream_settings(world["store"]).executor == "claude"


def test_init_repairs_an_invalid_key_it_owns(world):
    # an invalid [dream] table stops `dream run`; init rewrites the keys it owns
    (world["store"] / "settings.toml").write_text(
        'max_body_chars = 4000\n[dream]\nexecutor_path = "relative"\n', encoding="utf-8")
    with pytest.raises(SettingsError):
        load_dream_settings(world["store"])
    assert _init(world)[0] == 0
    assert load_dream_settings(world["store"]).executor_path == str(world["bin"] / "claude")


def test_after_init_the_table_loads_and_a_run_works(world):
    (world["store"] / "settings.toml").write_text(
        "max_body_chars = 4000\n[dream]\nidle_minutes = 30\n", encoding="utf-8")
    assert _init(world)[0] == 0
    assert load_dream_settings(world["store"]).idle_minutes == 30
    assert _run(world)[0] == 0


def test_uninstall_removes_the_agent_and_keeps_settings(world):
    _init(world)
    code, out = _out(dream_commands.run_uninstall, home=world["home"],
                     launchctl=world["launchctl"], uid=501, platform="darwin")
    assert code == 0 and "removed the schedule" in out
    assert not plist_path(world["home"]).exists() and "dream" in _settings(world)
    code, out = _out(dream_commands.run_uninstall, home=world["home"],
                     launchctl=world["launchctl"], uid=501, platform="darwin")
    assert code == 0 and "no schedule installed" in out


def test_an_install_launchd_cannot_confirm_claims_no_restore(world):
    _init(world)

    class Unsure(Launchctl):
        """bootout works, but launchd cannot answer the print that follows it."""

        def __call__(self, args):
            if args[0] == "print" and self.calls and self.calls[-1][0] == "bootout":
                self.calls.append(list(args))
                return 5
            return super().__call__(args)

    unsure = Unsure()
    unsure.loaded = {DREAM_LAUNCH_AGENT_LABEL}
    code, out = _init(world, launchctl=unsure, at="05:00")
    assert code == 1 and "launchctl print" in out
    assert "put back" not in out and "restored" not in out


OVERRIDES = ('\n[dream.codex_overrides]\n"model_provider" = "foundry"\n'
             '"model_providers.foundry.base_url" = "https://example.invalid/openai/v1"\n'
             '"model_providers.foundry.env_key" = "DREAM_TEST_PROVIDER_KEY"\n'
             '"model_providers.foundry.wire_api" = "responses"\n')


def _add_overrides(world, text: str = OVERRIDES) -> None:
    path = world["store"] / "settings.toml"
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def test_init_with_codex_refuses_a_provider_variable_missing_here_then_keeps_the_table(
        world):
    _init(world)
    _add_overrides(world)
    before, calls = _settings(world), len(world["launchctl"].calls)
    code, out = _init(world, executor="codex")
    assert code == 2 and "DREAM_TEST_PROVIDER_KEY" in out
    assert _settings(world) == before and len(world["launchctl"].calls) == calls
    code, out = _init(world, executor="codex", env={"DREAM_TEST_PROVIDER_KEY": "synthetic"})
    assert code == 0 and "provider variables: DREAM_TEST_PROVIDER_KEY" in out
    assert "synthetic" not in out
    assert _settings(world)["dream"]["codex_overrides"]["model_provider"] == "foundry"
    # the provider variable stays out of the schedule: exactly these three
    assert plistlib.loads(plist_path(world["home"]).read_bytes())["EnvironmentVariables"] == {
        "HOME": str(world["home"]), "PATH": f"{world['bin']}:/usr/bin:/bin",
        "MEMRIVER_ROOT": str(world["store"])}
    # settings -> executor factory -> argv, the real path a run takes
    dream = load_dream_settings(world["store"])
    argv = make_executor(dream, env={}).argv(files=Path("/files"))
    assert 'model_providers.foundry.env_key="DREAM_TEST_PROVIDER_KEY"' in argv
    assert "synthetic" not in " ".join(argv)


def test_init_names_a_refused_override_key_and_never_its_value(world):
    _init(world)
    _add_overrides(world, '\n[dream.codex_overrides]\n'
                          '"model_providers.x.experimental_bearer_token" = "sk-synthetic"\n')
    with pytest.raises(SettingsError) as caught:
        _init(world)
    assert str(caught.value) == "settings.toml is invalid: field dream.codex_overrides"
    assert "sk-synthetic" not in str(caught.value)


def test_run_with_codex_refuses_a_missing_provider_variable_and_builds_no_executor(
        world, monkeypatch):
    _init(world, executor="codex")
    _add_overrides(world)
    built: list = []

    def factory(dream):
        built.append(dream)
        return Executor()

    monkeypatch.delenv("DREAM_TEST_PROVIDER_KEY", raising=False)
    code, _, err = _run(world, executor_factory=factory)
    assert code == 1 and "DREAM_TEST_PROVIDER_KEY" in err and built == []
    monkeypatch.setenv("DREAM_TEST_PROVIDER_KEY", "synthetic")
    code, _, err = _run(world, executor_factory=factory)
    assert code == 0 and built[0].codex_overrides["model_provider"] == "foundry"
    assert "synthetic" not in err


def test_a_separate_agent_label_leaves_the_default_agent_alone(world):
    _init(world)
    default = plist_path(world["home"]).read_bytes()
    code, _ = _init(world, label="test.memriver.dream")
    assert code == 0 and plist_path(world["home"], "test.memriver.dream").exists()
    assert world["launchctl"].loaded == {DREAM_LAUNCH_AGENT_LABEL, "test.memriver.dream"}
    code, _ = _out(dream_commands.run_uninstall, home=world["home"],
                   launchctl=world["launchctl"], uid=501, platform="darwin",
                   label="test.memriver.dream")
    assert code == 0 and plist_path(world["home"]).read_bytes() == default
    assert world["launchctl"].loaded == {DREAM_LAUNCH_AGENT_LABEL}


def _plant(world, body: str, *, description: str = "cue", project_id=None) -> str:
    memory_id, stamp = new_id(), now()
    with closing(sqlite3.connect(world["store"] / "memriver.db")) as conn, conn:
        conn.execute("INSERT INTO memories (id, project_id, type, source_harness, "
                     "source_method, trust, sync, description, body, created, updated, "
                     "version) VALUES (?, ?, 'project', 't', 'agent', 'agent', 1, ?, ?, ?, ?, 1)",
                     (memory_id, project_id or world["project"].id, description, body, stamp,
                      stamp))
    return memory_id


def _run(world, **options):
    out, err = io.StringIO(), io.StringIO()
    values = {"phase": None, "trigger": "manual", "root": world["store"], "stdout": out,
              "stderr": err, "executor_factory": lambda dream: Executor(), "transcripts": None}
    code = dream_commands.run_run(**(values | options))
    return code, out.getvalue(), err.getvalue()


def test_run_without_an_executor_is_a_secret_sweep_and_prints_its_report(world):
    in_body = _plant(world, "deploy with " + SECRET, description="deploy notes")
    in_cue = _plant(world, "fact", description=SECRET)
    code, out, err = _run(world)
    assert (code, err) == (0, "")
    assert "no executor configured" in out and "secrets" in out
    assert in_body in out and in_cue in out and "deploy notes" in out
    assert "rule: github-pat" in out and "(cue withheld)" in out
    assert out.count("memriver dream undo ") == 2
    assert "ghp_" not in out


def test_run_with_an_executor_runs_every_phase(world):
    _init(world)
    _plant(world, "a fact")
    code, out, _ = _run(world)
    assert code == 0 and "consolidation: done" in out and "TTL retirement" in out


def test_run_with_a_phase_but_no_executor_exits_1(world):
    code, _, err = _run(world, phase="retire")
    assert code == 1 and "--phase needs an executor" in err


def test_run_with_an_invalid_dream_table_raises_the_settings_error(world):
    # cli.main prints it (test_an_invalid_dream_table_stops_dream_run_naming_the_field)
    (world["store"] / "settings.toml").write_text("[dream]\nexecutor = 'gpt'\n",
                                                  encoding="utf-8")
    with pytest.raises(SettingsError, match="field dream.executor"):
        _run(world)


def test_run_skipped_for_the_lock_exits_0(world):
    with run_lock(world["store"]):
        code, out, _ = _run(world)
    assert code == 0 and out.count("skipped: locked") == 1


def test_a_store_failure_exits_1(world, monkeypatch):
    from memriver_core import StorageFailure

    def broken(*args, **kwargs):
        raise StorageFailure

    monkeypatch.setattr(dream_commands, "run_dream", broken)
    code, _, err = _run(world)
    assert code == 1 and "could not be read or written" in err


def _report(world, run_id=None, list_count=None):
    out = io.StringIO()
    code = dream_commands.run_report(run_id, list_count=list_count, root=world["store"],
                                     stdout=out)
    return code, out.getvalue()


def test_report_lists_the_latest_run_a_named_run_and_the_run_list(world):
    _plant(world, "deploy with " + SECRET)
    _run(world)
    _run(world)
    code, out = _report(world)
    assert code == 0 and out.startswith("run ")
    first = build_maintenance_service(Settings(root=world["store"])).runs(10)[-1]
    code, out = _report(world, first.run_id)
    assert code == 0 and "rule: github-pat" in out and "ghp_" not in out
    code, out = _report(world, list_count=10)
    assert code == 0 and len(out.strip().splitlines()) == 2
    assert _report(world, "zzzzzzzzzz")[0] == 2


def test_a_run_killed_after_committing_a_group_still_reports_it_with_its_undo(world):
    maintenance = build_maintenance_service(Settings(root=world["store"]))
    a, b = _plant(world, "uv manages python"), _plant(world, "python uses uv")
    killed = maintenance.start_run("schedule", "codex", now())
    change_id = maintenance.apply_group(ChangeGroup(
        run_id=killed, kind="merge", project_id=world["project"].id, reason="same fact",
        harness="codex", ops=(CreateOp(world["project"].id, "project", "tooling", "Use uv.",
                                       ((a, 1), (b, 1))),)))
    _run(world)                                   # the next run marks the killed one failed
    code, out = _report(world, killed)
    assert code == 0 and "failed" in out and "counts unknown" in out
    assert f"change {change_id}" in out and f"memriver dream undo {change_id}" in out


def _undo(world, change_id, **options):
    values = {"yes": True, "root": world["store"], "stdin_is_tty": False, "input_fn": input}
    return _out(dream_commands.run_undo, change_id, **(values | options))


def test_undo_restores_a_change_and_refuses_a_conflict_or_an_unknown_id(world):
    maintenance = build_maintenance_service(Settings(root=world["store"]))
    a, b = _plant(world, "uv manages python"), _plant(world, "python uses uv")
    change_id = maintenance.apply_group(ChangeGroup(
        run_id="r", kind="merge", project_id=world["project"].id, reason="same fact",
        harness="codex", ops=(CreateOp(world["project"].id, "project", "tooling", "Use uv.",
                                       ((a, 1), (b, 1))),)))
    merged = maintenance.change(change_id).rows[0].id
    context = world["service"].open_project_context(world["project"].root)
    world["service"].update(merged, "Use uv 0.5.", context, expected_version=1)
    code, out = _undo(world, change_id)
    assert code == 2 and merged in out and "nothing was undone" in out
    assert _undo(world, "zzzzzzzzzz")[0] == 2
    secret = _plant(world, SECRET)
    _run(world)
    (quarantine,) = [c for c in maintenance.changes(10) if c.kind == "secret"]
    code, out = _undo(world, quarantine.change_id)
    assert code == 0 and f"undone {quarantine.change_id}" in out
    assert world["service"].show(secret).deleted_at is None


def test_a_fifo_planted_at_a_temporary_name_never_blocks_init(world):
    os.mkfifo(world["store"] / ".settings.toml.tmp")

    def stuck(signum, frame):
        raise TimeoutError

    previous = signal.signal(signal.SIGALRM, stuck)
    signal.alarm(5)
    try:
        code, _ = _init(world)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    assert code == 0 and _settings(world)["dream"]["executor"] == "claude"


def _python_in(directory: Path) -> str:
    """A fake interpreter with a memriver console script beside it."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("python", "memriver"):
        (directory / name).write_text("#!/bin/sh\n")
        (directory / name).chmod(0o755)
    return str(directory / "python")


def test_init_schedules_the_memriver_beside_a_persistent_interpreter(world):
    tools = world["tmp"] / "tools" / "memriver" / "bin"
    code, _ = _init(world, memriver_path=None, executable=_python_in(tools))
    assert code == 0
    assert plistlib.loads(plist_path(world["home"]).read_bytes())["ProgramArguments"][0] == \
        str(tools / "memriver")


def test_init_from_uvs_cache_refuses_even_with_a_persistent_memriver_on_path(world):
    cached = _python_in(world["tmp"] / "cache" / "archive-v0" / "abc" / "bin")
    persistent = str(world["bin"] / "memriver")

    def which(name):
        return persistent if name == "memriver" else world["which"](name)

    code, out = _init(world, memriver_path=None, executable=cached, which=which)
    assert code == 2 and "uv tool install memriver" in out
    assert "dream" not in _settings(world) and not plist_path(world["home"]).exists()
    assert world["launchctl"].calls == []


def test_init_that_cannot_write_its_settings_says_nothing_was_written(world, monkeypatch):
    def full_disk(path, values):
        raise OSError("no space left on device")

    monkeypatch.setattr(dream_commands, "_write_dream_table", full_disk)
    code, out = _init(world)
    assert code == 1 and "could not be written" in out and "no space" not in out
    assert "settings were written" not in out and world["launchctl"].calls == []


def test_init_that_cannot_make_the_dream_directory_says_the_settings_were_written(world):
    (world["store"] / "dream").write_text("not a directory", encoding="utf-8")
    code, out = _init(world)
    assert code == 1 and "the settings were written" in out
    assert _settings(world)["dream"]["executor"] == "claude"
    assert world["launchctl"].calls == []


def test_uninstall_that_cannot_run_launchctl_fails_and_keeps_the_plist(world):
    _init(world)

    def missing(args):
        raise FileNotFoundError("/bin/launchctl")

    code, out = _out(dream_commands.run_uninstall, home=world["home"], launchctl=missing,
                     uid=501, platform="darwin")
    assert code == 1 and "could not be removed" in out
    assert plist_path(world["home"]).exists()


def test_uninstall_that_cannot_delete_the_plist_fails(world):
    _init(world)
    world["launchctl"].loaded.clear()                  # not loaded: only the file is left
    agents = plist_path(world["home"]).parent
    agents.chmod(0o500)
    try:
        code, out = _out(dream_commands.run_uninstall, home=world["home"],
                         launchctl=world["launchctl"], uid=501, platform="darwin")
    finally:
        agents.chmod(0o700)
    assert code == 1 and "could not be removed" in out
    assert plist_path(world["home"]).exists()


def test_report_neutralizes_the_stored_run_fields(world):
    _run(world)
    # core validates the id, times, trigger and status it reads; these are free text
    report = {f"secrets{chr(27)}[2J": {"done": 1, "failed": 0, "skipped": 0},
              "retire": {"done": 1, "outcomes": {f"kept{chr(27)}[2J": 1},
                         "items": [{"decision": f"keep{chr(27)}[2J",
                                    "memory_id": f"m{chr(10)}forged line"}]}}
    with closing(sqlite3.connect(world["store"] / "memriver.db")) as conn, conn:
        conn.execute("UPDATE dream_runs SET executor = ?, report = ?",
                     (f"x{chr(10)}forged line",
                      json.dumps(report, ensure_ascii=False, separators=(",", ":"))))
    for list_count in (None, 10):
        code, out = _report(world, list_count=list_count)
        assert code == 0 and out.startswith("run " if list_count is None else "")
        assert chr(27) not in out and "\nforged line" not in out


@pytest.mark.parametrize("command", [["init", "--yes"], ["run"], ["report"],
                                     ["undo", "aaaaaaaaaa", "--yes"]])
@pytest.mark.parametrize(("env", "text", "line"), [
    ({"MEMRIVER_MAX_BODY_CHARS": "SENTINEL-VALUE"}, "",
     "memriver: environment variable MEMRIVER_MAX_BODY_CHARS is invalid\n"),
    ({}, 'max_body_chars = "SENTINEL-VALUE"\n',
     "memriver: settings.toml is invalid: field max_body_chars\n"),
    ({}, "max_body_chars = = 1\n", "memriver: settings.toml could not be read\n"),
])
def test_an_unusable_setting_stops_every_dream_command_with_one_named_line(
        world, monkeypatch, capsys, command, env, text, line):
    from memriver.cli import main

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    if text:
        (world["store"] / "settings.toml").write_text(text, encoding="utf-8")
    code = main(["dream", *command, "--root", str(world["store"])])
    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (1, "", line)


def test_an_invalid_dream_table_stops_dream_run_naming_the_field(world, capsys):
    from memriver.cli import main

    (world["store"] / "settings.toml").write_text(
        '[dream]\nexecutor = "claude"\nexecutor_path = "/opt/claude"\nttl_days = 0\n',
        encoding="utf-8")
    code = main(["dream", "run", "--root", str(world["store"])])
    captured = capsys.readouterr()
    assert (code, captured.out) == (1, "")
    assert captured.err == "memriver: settings.toml is invalid: field dream.ttl_days\n"
