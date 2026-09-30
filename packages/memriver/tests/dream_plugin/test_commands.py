"""`memriver dream ...` over a tmp store, a fake launchctl, fake executables and a
scripted executor -- never the real harnesses, LaunchAgents or store."""

# spec §10 item 17: v5 §12 item 13 (init and uninstall) and the umbrella commands of
# §8.1 (run and its exit codes, report, no dream undo); §10 item 12's retention rule as
# seen through `report`

from __future__ import annotations

import errno
import io
import os
import plistlib
import shlex
import signal
import sqlite3
import subprocess
import sys
import tomllib
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from memriver import cli
from memriver.dream_plugin import commands as dream_commands
from memriver.dream_plugin.adapter import FAILURE_HINTS
from memriver.dream_plugin.commands import DreamTable
from memriver.dream_plugin.schedule import plist_path
from memriver.dream_plugin.transcripts import HarnessTranscripts
from memriver.executor import make_executor
from memriver.settings import DREAM_LAUNCH_AGENT_LABEL, DREAM_SCRATCH_PREFIX
from memriver.views import unsupported_store
from memriver_core import StorageFailure, StoreNeedsUpgrade
from memriver_core.bootstrap import build_services
from memriver_core.models import Create, new_id, now
from memriver_core.settings import Settings, SettingsError
from memriver_dream.lock import run_lock
from memriver_dream.protocols import ExecutorResult
from memriver_dream.settings import (
    DREAM_DB_FILENAME,
    DREAM_DIRECTORY,
    DREAM_REPORTS_DIRECTORY,
    load_dream_settings,
)
from memriver_dream.store import DreamStore, RunRow

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
        if args[0] == "bootstrap":                   # bootstrap gui/<uid> <plist>
            self.loaded.add(Path(args[2]).stem)
        else:                                        # bootout gui/<uid>/<label>
            self.loaded.discard(args[1].rpartition("/")[2])
        return 0


class Executor:
    """Every call fails the way an unreachable harness does: a per-item failure, which
    never stops a run (spec §6.1)."""

    name, harness = "fake", "fake-harness"

    def __init__(self) -> None:
        self.calls = 0

    def run(self, *, system_prompt, prompt, schema, timeout_s):
        self.calls += 1
        return ExecutorResult(error="exit")


def _initialized(store: Path, home: Path, work: Path):
    services = build_services(Settings(root=store), root=store, home=home)
    services.project.ensure_global()
    return services, services.project.init_project("demo",
                                                   services.project.plan_root(str(work)))


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
    services, project = _initialized(store, home, work)
    (store / "settings.toml").write_text("max_body_chars = 4000\n", encoding="utf-8")
    return {"tmp": tmp_path, "store": store, "home": home, "work": work, "bin": bin_dir,
            "services": services, "project": project, "launchctl": Launchctl(),
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
                                "ttl_days": 30, "schedule_at": "04:00"}
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
    code, out, _ = _run(world, root=Path(env["MEMRIVER_ROOT"]))
    assert code == 0 and secret in out           # this store's policy scan found it


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


def test_settings_broken_during_confirmation_is_refused_not_a_traceback(world):
    # a real race: settings.toml changes between the plan being shown and the
    # answer coming back, into something even the write-side parser cannot read
    settings_file = world["store"] / "settings.toml"

    def answer(prompt):
        settings_file.write_text("[dream\n", encoding="utf-8")
        return "y"

    with pytest.raises(SettingsError, match=r"^settings\.toml could not be read$"):
        _init(world, yes=False, stdin_is_tty=True, input_fn=answer)
    assert settings_file.read_text(encoding="utf-8") == "[dream\n"    # not overwritten
    assert world["launchctl"].calls == []


def test_settings_made_unreadable_during_confirmation_is_refused_not_a_traceback(world):
    settings_file = world["store"] / "settings.toml"

    def answer(prompt):
        settings_file.unlink(missing_ok=True)
        settings_file.mkdir()                   # a directory where the file was
        return "y"

    with pytest.raises(SettingsError, match=r"^settings\.toml could not be read$"):
        _init(world, yes=False, stdin_is_tty=True, input_fn=answer)
    assert world["launchctl"].calls == []


def test_init_replaces_every_case_variant_of_the_keys_it_owns(world):
    # the run matches keys case-insensitively, first spelling winning: an upper-case
    # key left beside the one init writes would silently keep the old value
    (world["store"] / "settings.toml").write_text(
        "max_body_chars = 4000\n[dream]\nEXECUTOR = 'claude'\nEXECUTOR_PATH = '/old/claude'\n"
        "TTL_DAYS = 90\nSchedule_At = '04:00'\nnot_a_key = 1\n", encoding="utf-8")
    code, _ = _init(world, executor="codex", ttl_days=30, at="05:30")
    assert code == 0
    dream = load_dream_settings(world["store"], model=DreamTable)
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
        load_dream_settings(world["store"], model=DreamTable)
    assert _init(world)[0] == 0
    assert load_dream_settings(world["store"], model=DreamTable).executor == "claude"
    assert "EXECUTOR" not in _settings(world)["dream"]


def test_init_salvages_a_valid_owned_key_when_the_table_fails_to_load(world):
    # the table as a whole is unusable (executor is invalid), but ttl_days validates
    # on its own: init keeps it instead of silently resetting it to the default
    (world["store"] / "settings.toml").write_text(
        "max_body_chars = 4000\n[dream]\nEXECUTOR = 'gpt'\nttl_days = 30\n",
        encoding="utf-8")
    with pytest.raises(SettingsError):
        load_dream_settings(world["store"], model=DreamTable)
    assert _init(world)[0] == 0
    dream = load_dream_settings(world["store"], model=DreamTable)
    assert (dream.executor, dream.ttl_days) == ("claude", 30)


def test_init_salvages_owned_keys_across_case_with_yes(world):
    (world["store"] / "settings.toml").write_text(
        "max_body_chars = 4000\n[dream]\nexecutor = 'codex'\nTTL_DAYS = 30\n"
        "schedule_at = 'bad'\n", encoding="utf-8")
    with pytest.raises(SettingsError):
        load_dream_settings(world["store"], model=DreamTable)
    assert _init(world, yes=True)[0] == 0
    dream = load_dream_settings(world["store"], model=DreamTable)
    assert (dream.executor, dream.ttl_days, dream.schedule_at) == ("codex", 30, "04:00")


def test_init_keeps_an_unknown_key_which_every_reader_ignores(world):
    (world["store"] / "settings.toml").write_text(
        "max_body_chars = 4000\n[dream]\nnot_a_key = 1\n", encoding="utf-8")
    assert _init(world)[0] == 0
    assert _settings(world)["dream"]["not_a_key"] == 1
    assert load_dream_settings(world["store"], model=DreamTable).executor == "claude"


def test_init_repairs_an_invalid_key_it_owns(world):
    # an invalid [dream] table stops `dream run`; init rewrites the keys it owns
    (world["store"] / "settings.toml").write_text(
        'max_body_chars = 4000\n[dream]\nexecutor_path = "relative"\n', encoding="utf-8")
    with pytest.raises(SettingsError):
        load_dream_settings(world["store"], model=DreamTable)
    assert _init(world)[0] == 0
    assert load_dream_settings(world["store"], model=DreamTable).executor_path == str(world["bin"] / "claude")


def test_after_init_the_table_loads_and_a_run_works(world):
    # idle_minutes was a [dream] key of an earlier build (spec §7: removed): an
    # unknown key now, kept in the file and ignored by every reader
    (world["store"] / "settings.toml").write_text(
        "max_body_chars = 4000\n[dream]\nidle_minutes = 30\n", encoding="utf-8")
    assert _init(world)[0] == 0
    dream = load_dream_settings(world["store"], model=DreamTable)
    assert dream.executor == "claude" and not hasattr(dream, "idle_minutes")
    assert _settings(world)["dream"]["idle_minutes"] == 30
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
    dream = load_dream_settings(world["store"], model=DreamTable)
    argv = make_executor(dream, env={}, scratch_prefix=DREAM_SCRATCH_PREFIX).argv(files=Path("/files"))
    assert 'model_providers.foundry.env_key="DREAM_TEST_PROVIDER_KEY"' in argv
    assert "synthetic" not in " ".join(argv)


def test_init_names_only_the_codex_overrides_field_never_the_key_or_value(world):
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


def _plant(world, body: str, *, description: str = "cue") -> str:
    """A memory written straight into the store as an imported memory: one version
    with no change and no content-policy check -- the only way a secret gets in."""
    memory_id, stamp = new_id(), now()
    with closing(sqlite3.connect(world["store"] / "memriver.db")) as conn, conn:
        conn.execute("INSERT INTO memories (id, project_id, source_harness, source_method, "
                     "created, version, type, trust, sync, description, body, updated) "
                     "VALUES (?, ?, 't', 'agent', ?, 1, 'project', 'agent', 1, ?, ?, ?)",
                     (memory_id, world["project"].id, stamp, description, body, stamp))
        conn.execute("INSERT INTO memory_versions (memory_id, version, type, trust, sync, "
                     "description, body, deleted, change_id) "
                     "VALUES (?, 1, 'project', 'agent', 1, ?, ?, 0, NULL)",
                     (memory_id, description, body))
    return memory_id


def _run(world, **options):
    out, err = io.StringIO(), io.StringIO()
    values = {"phase": None, "trigger": "manual", "root": world["store"], "stdout": out,
              "stderr": err, "executor_factory": lambda dream: Executor(), "transcripts": None}
    code = dream_commands.run_run(**(values | options))
    return code, out.getvalue(), err.getvalue()


def _dream_store(world) -> DreamStore:
    return DreamStore(world["store"] / DREAM_DIRECTORY / DREAM_DB_FILENAME)


def _report_file(world, run: RunRow) -> Path:
    return world["store"] / DREAM_DIRECTORY / DREAM_REPORTS_DIRECTORY / run.report_file


def _skipped(run_id: str, now: str, trigger: str) -> RunRow:
    return RunRow(run_id=run_id, started_at=now, finished_at=now, trigger=trigger,
                  status="skipped", report_file=f"{run_id}.txt")


def test_run_without_an_executor_scans_and_prints_its_report(world):
    # §8.1 / v5 §9.1: no [dream] table -> the policy scan only, exit 0; the printed
    # report is the report file, naming the hit by id and rule, never the secret
    secret = _plant(world, "deploy with " + SECRET, description="deploy notes")
    code, out, err = _run(world)
    assert (code, err) == (0, "")
    (run,) = _dream_store(world).runs(10)
    assert (run.status, run.trigger) == ("completed", "manual")
    assert out.splitlines() == _report_file(world, run).read_text(
        encoding="utf-8").splitlines()
    assert run.run_id in out and secret in out and "github-pat" in out
    assert f"memriver delete {secret} --hard" in out and "ghp_" not in out


def test_a_scheduled_run_prints_one_line_not_its_report(world):
    # under the schedule stdout is dream.log, which retention never prunes
    _plant(world, "deploy with " + SECRET, description="deploy notes")
    code, out, err = _run(world, trigger="schedule")
    (run,) = _dream_store(world).runs(10)
    assert (code, err) == (0, "")
    assert out == f"run {run.run_id} completed; memriver dream report {run.run_id}\n"


def test_run_exits_1_when_the_finished_run_report_cannot_be_read_back(world, monkeypatch):
    # the run itself completes -- scanned, stored, its report written -- only the
    # final read-back that prints it fails; the row and report file are not undone
    reports = world["store"] / DREAM_DIRECTORY / DREAM_REPORTS_DIRECTORY
    original_read_text = Path.read_text

    def flaky(self, *args, **kwargs):
        if self.parent == reports:
            raise OSError(errno.EIO, "Input/output error")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky)
    code, out, err = _run(world)
    assert (code, err) == (1, dream_commands.DREAM_FAILURE)
    assert "could not be read" in out and "Input/output error" not in out
    (run,) = _dream_store(world).runs(10)
    assert run.status == "completed" and _report_file(world, run).exists()


def test_run_with_an_executor_calls_it_and_still_completes(world):
    # every executor call fails here: per-item failures never stop a run (§6.1)
    _init(world)
    _plant(world, "uv manages the python versions of this repo")
    _plant(world, "python versions in this repo come from uv")
    executor = Executor()
    code, out, err = _run(world, executor_factory=lambda dream: executor)
    assert (code, err) == (0, "") and executor.calls > 0
    (run,) = _dream_store(world).runs(10)
    assert run.status == "completed" and run.run_id in out


def test_run_hands_the_settings_the_phase_and_the_trigger_to_run_dream(world, monkeypatch):
    _init(world)
    seen: list[dict] = []

    def fake_run_dream(services, executor, transcripts, settings, *, root, now, trigger,
                       phases, failure_hints):
        seen.append({"executor": executor, "transcripts": transcripts, "settings": settings,
                     "root": root, "trigger": trigger, "phases": phases,
                     "failure_hints": failure_hints,
                     "global": services.project.global_project_id()})
        return _skipped("r", now, trigger)

    monkeypatch.setattr(dream_commands, "run_dream", fake_run_dream)
    for phase in ("summarize", "consolidate", "extract", "retire"):
        assert _run(world, phase=phase, trigger="schedule")[0] == 0
    assert _run(world)[0] == 0
    assert [call["phases"] for call in seen] == [{"summarize"}, {"consolidate"}, {"extract"},
                                                 {"retire"}, None]
    assert [call["trigger"] for call in seen] == ["schedule"] * 4 + ["manual"]
    first = seen[0]
    assert isinstance(first["executor"], Executor)
    assert isinstance(first["transcripts"], HarnessTranscripts)
    assert first["settings"].executor == "claude" and first["root"] == world["store"]
    assert first["failure_hints"] is FAILURE_HINTS
    assert first["global"] is not None


def test_run_without_a_dream_table_has_no_executor_and_no_transcripts(world, monkeypatch):
    seen: list[tuple] = []

    def fake_run_dream(services, executor, transcripts, settings, **kwargs):
        seen.append((executor, transcripts, settings, kwargs["phases"]))
        return _skipped("r", kwargs["now"], kwargs["trigger"])

    monkeypatch.setattr(dream_commands, "run_dream", fake_run_dream)
    assert _run(world)[0] == 0
    assert seen == [(None, None, None, None)]


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
    with run_lock(world["store"]) as held:
        assert held
        code, out, err = _run(world)
    assert (code, out, err) == (0, dream_commands.SKIPPED, "")


def test_a_store_failure_exits_1(world, monkeypatch):
    def broken(*args, **kwargs):
        raise StorageFailure

    monkeypatch.setattr(dream_commands, "run_dream", broken)
    code, _, err = _run(world)
    assert code == 1 and err == dream_commands.STORE_FAILURE


def _old_store(*args, **kwargs):
    """build_services over a store below schema v4: its first operation refuses (T3)."""
    def refuse():
        raise StoreNeedsUpgrade(3)

    return SimpleNamespace(project=SimpleNamespace(global_project_id=refuse))


def test_a_store_below_v4_stops_run_and_init(world, monkeypatch):
    # spec §9: a store below the schema this memriver needs is refused wherever it is
    # read; every entry point below exits 1 / 2 with the same wording, never "upgrade"
    monkeypatch.setattr(dream_commands, "build_services", _old_store)
    hint = unsupported_store(StoreNeedsUpgrade(3))
    code, out, err = _run(world)
    assert (code, out, err) == (1, "", f"memriver dream: {hint}\n")
    code, out = _init(world)
    assert code == 1 and out == f"refused: {hint}\n"
    assert "upgrade" not in out
    assert "dream" not in _settings(world) and not plist_path(world["home"]).exists()


def test_run_on_an_uninitialized_store_exits_1(world, tmp_path):
    code, _, err = _run(world, root=tmp_path / "empty")
    assert code == 1 and "run memriver install" in err


def _corrupt_dream_db(world) -> None:
    (world["store"] / DREAM_DIRECTORY).mkdir(exist_ok=True)
    (world["store"] / DREAM_DIRECTORY / DREAM_DB_FILENAME).write_bytes(b"not a database" * 64)


def test_a_corrupt_dream_store_stops_run_with_the_fixed_line_and_no_traceback(world, capsys):
    # dream.db holding non-SQLite bytes: DreamStore raises sqlite3.DatabaseError before
    # any run row can exist
    _corrupt_dream_db(world)
    assert _run(world) == (1, "", dream_commands.DREAM_FAILURE)
    code = cli.main(["dream", "run", "--root", str(world["store"])])
    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (1, "", dream_commands.DREAM_FAILURE)


@pytest.mark.skipif(os.geteuid() == 0, reason="needs non-root permission semantics")
def test_a_report_file_that_cannot_be_created_exits_1_and_leaves_the_row_running(world):
    reports = world["store"] / DREAM_DIRECTORY / DREAM_REPORTS_DIRECTORY
    reports.mkdir(parents=True)
    reports.chmod(0o500)                       # the run's report file cannot be created
    try:
        code, out, err = _run(world)
    finally:
        reports.chmod(0o700)
    assert (code, out, err) == (1, "", dream_commands.DREAM_FAILURE)
    (row,) = _dream_store(world).runs(10)
    # the whole reports directory is unwritable, so neither the completion nor the
    # failure footer can be written either: the row is left "running", for the next
    # run's crash recovery to mark it failed and close its report (memriver_dream's
    # own contract; see test_a_run_that_cannot_record_its_own_failure_leaves_the_row_running)
    assert row.status == "running"


def test_a_dream_store_failure_during_the_run_records_it_failed_and_exits_1(world,
                                                                           monkeypatch):
    # the store fails after the run started (retention's listing, inside the run): the
    # run is recorded failed and the exception's text never reaches the output
    def locked(self, before):
        raise sqlite3.OperationalError("database is locked SENTINEL")

    monkeypatch.setattr(DreamStore, "runs_before", locked)
    code, out, err = _run(world)
    assert (code, err) == (1, dream_commands.DREAM_FAILURE)
    assert "SENTINEL" not in out + err
    (run,) = _dream_store(world).runs(10)
    assert run.status == "failed" and run.finished_at is not None


def _ago(**delta) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _stored_run(world, text: str, *, status: str = "completed",
                started_at: str | None = None) -> RunRow:
    """A run row and its report file, as memriver_dream leaves them."""
    run_id, started = new_id(), started_at or now()
    run = RunRow(run_id=run_id, started_at=started, finished_at=None, trigger="manual",
                 status="running", report_file=f"{run_id}.txt")
    _report_file(world, run).parent.mkdir(parents=True, exist_ok=True)
    _report_file(world, run).write_text(text, encoding="utf-8")
    store = _dream_store(world)
    store.start_run(run)
    if status != "running":
        store.finish_run(run_id, status=status, finished_at=started)
    return run


def _report(world, run_id=None, list_count=None):
    out = io.StringIO()
    code = dream_commands.run_report(run_id, list_count=list_count, root=world["store"],
                                     stdout=out)
    return code, out.getvalue()


def test_report_prints_the_latest_or_a_named_report_file_and_lists_the_runs(world):
    first = _stored_run(world, "first report\n", started_at=_ago(minutes=5))
    second = _stored_run(world, "second report\n")
    assert _report(world) == (0, "second report\n")
    assert _report(world, first.run_id) == (0, "first report\n")
    code, out = _report(world, list_count=10)
    assert code == 0
    assert [line.split() for line in out.splitlines()] == [
        [run.run_id, run.started_at, "manual", "completed"] for run in (second, first)]
    assert _report(world, list_count=1)[1].split()[0] == second.run_id
    code, out = _report(world, "zzzzzzzzzz")
    assert code == 2 and "no such run: zzzzzzzzzz" in out


def test_report_against_a_store_stamped_with_an_unknown_version_exits_1(world):
    (world["store"] / DREAM_DIRECTORY).mkdir(exist_ok=True)
    with closing(sqlite3.connect(
            world["store"] / DREAM_DIRECTORY / DREAM_DB_FILENAME)) as conn, conn:
        conn.execute("PRAGMA user_version = 2")
    assert _report(world) == (1, dream_commands.DREAM_FAILURE)


def test_report_with_no_runs_says_so(world):
    assert _report(world) == (0, "(no dream runs yet)\n")
    assert _report(world, list_count=10) == (0, "(no dream runs yet)\n")


def test_a_running_run_is_shown_interrupted_only_while_no_run_holds_the_lock(world):
    # §8.1: a run still "running" without the lock held is shown as interrupted
    text = "run r\napplying merge aaaaaaaaaa bbbbbbbbbb\n"
    run = _stored_run(world, text, status="running")
    assert _report(world, run.run_id) == (0, text + dream_commands.INTERRUPTED)
    assert _report(world, list_count=10)[1].split()[-1] == "interrupted"
    with run_lock(world["store"]) as held:        # a live run holds it
        assert held
        assert _report(world, run.run_id) == (0, text)
        assert _report(world, list_count=10)[1].split()[-1] == "running"


def test_report_neutralizes_invisible_characters_and_never_splits_on_them(world):
    # a line separator inside a line must not become a line of its own
    run = _stored_run(world, f"run r{chr(27)}[2J\nreason: x{chr(0x2028)}forged line\n")
    code, out = _report(world, run.run_id)
    assert code == 0 and chr(27) not in out and chr(0x2028) not in out
    assert out == "run r [2J\nreason: x forged line\n"


def test_report_applies_retention_first_unless_a_run_holds_the_lock(world):
    # §5: runs and report files older than report_retention_days (30) are deleted
    # before a report, under the run lock
    old = _stored_run(world, "old report\n", started_at=_ago(days=60))
    kept = _stored_run(world, "kept report\n", started_at=_ago(days=1))
    with run_lock(world["store"]):
        _report(world, list_count=10)
    assert _dream_store(world).run(old.run_id) is not None
    code, out = _report(world, list_count=10)
    assert code == 0 and [line.split()[0] for line in out.splitlines()] == [kept.run_id]
    assert _dream_store(world).run(old.run_id) is None
    assert not _report_file(world, old).exists() and _report_file(world, kept).exists()


def test_report_on_a_corrupt_dream_store_exits_1_with_the_fixed_line(world):
    _corrupt_dream_db(world)
    assert _report(world) == (1, dream_commands.DREAM_FAILURE)


def test_report_refuses_a_store_below_v4_before_any_retention_runs(world):
    # a real store, not a stubbed build_services (the run/init test uses that): the
    # schema check must run before the dream lock, dream.db or retention touch anything
    old = _stored_run(world, "old report\n", started_at=_ago(days=60))
    with closing(sqlite3.connect(world["store"] / "memriver.db")) as conn, conn:
        conn.execute("PRAGMA user_version = 3")
    hint = f"memriver dream: {unsupported_store(StoreNeedsUpgrade(3))}\n"
    for options in ({}, {"run_id": old.run_id}, {"list_count": 10}):
        assert _report(world, **options) == (1, hint)
    assert _dream_store(world).run(old.run_id) is not None
    assert _report_file(world, old).exists()


def test_report_still_shows_dream_reports_when_the_core_store_cannot_be_read(world):
    old = _stored_run(world, "old report\n", started_at=_ago(days=1))
    (world["store"] / "memriver.db").write_bytes(b"not a database")
    code, out = _report(world, run_id=old.run_id)
    assert code == 0 and "old report" in out


@pytest.mark.parametrize(("argv", "handler", "expected"), [
    (["dream", "init", "--executor", "codex", "--ttl-days", "30", "--at", "05:30", "--yes"],
     "_dream_init", {"executor": "codex", "ttl_days": 30, "at": "05:30", "yes": True,
                     "agent_label": None}),
    (["dream", "init", "--agent-label", "test.memriver.dream"], "_dream_init",
     {"agent_label": "test.memriver.dream"}),
    (["dream", "run"], "_dream_run", {"phase": None, "trigger": "manual", "root": None}),
    (["dream", "run", "--phase", "extract", "--trigger", "schedule"], "_dream_run",
     {"phase": "extract", "trigger": "schedule"}),
    (["dream", "report"], "_dream_report", {"run_id": None, "list_count": None}),
    (["dream", "report", "--list"], "_dream_report", {"list_count": 10}),
    (["dream", "report", "--list", "3"], "_dream_report", {"list_count": 3}),
    (["dream", "report", "aaaaaaaaaa"], "_dream_report", {"run_id": "aaaaaaaaaa"}),
    (["dream", "uninstall"], "_dream_uninstall", {"agent_label": None}),
])
def test_dream_subcommands_parse(argv, handler, expected):
    args = cli._build_parser().parse_args(argv)
    assert args.handler is getattr(cli, handler)
    assert {key: getattr(args, key) for key in expected} == expected


@pytest.mark.parametrize("argv", [
    ["dream", "undo", "aaaaaaaaaa"],                  # undo is `memriver undo` (§8.2)
    ["dream", "run", "--phase", "secrets"],
    ["dream", "run", "--phase", "recheck"],
    ["dream", "uninstall", "--agent-label", "a b"],
])
def test_dream_refuses_what_it_does_not_offer(argv, capsys):
    with pytest.raises(SystemExit) as caught:
        cli._build_parser().parse_args(argv)
    assert caught.value.code == 2 and capsys.readouterr().err


def test_dream_help_lists_the_commands_and_hides_the_internal_options(capsys):
    def help_of(*argv: str) -> str:
        with pytest.raises(SystemExit):
            cli._build_parser().parse_args([*argv, "--help"])
        return capsys.readouterr().out

    out = help_of("dream")
    for command in ("init", "run", "report", "uninstall"):
        assert command in out
    assert "undo" not in out
    run_help = help_of("dream", "run")
    assert "--trigger" not in run_help and "{summarize,consolidate,extract,retire}" in run_help
    assert "--agent-label" not in help_of("dream", "init")

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


def test_init_writes_through_a_symlinked_settings_toml_and_keeps_the_link(world):
    real = world["tmp"] / "real-settings.toml"
    real.write_text("max_body_chars = 4000\n", encoding="utf-8")
    settings_path = world["store"] / "settings.toml"
    settings_path.unlink()
    settings_path.symlink_to(real)
    assert _init(world)[0] == 0
    assert settings_path.is_symlink() and settings_path.resolve() == real.resolve()
    written = tomllib.loads(real.read_text(encoding="utf-8"))
    assert written["max_body_chars"] == 4000 and written["dream"]["executor"] == "claude"
    assert load_dream_settings(world["store"], model=DreamTable).executor == "claude"


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


@pytest.mark.parametrize("command", [["init", "--yes"], ["run"], ["report"]])
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


def _break_raw_table_read(monkeypatch, target: Path, action):
    """Make `Path.read_text` misbehave for `target` alone, as init's raw [dream]-table
    read could independently of `load_settings`'s own read of the same file: the two
    go through different lower-level calls (`Path.open('rb')` + `tomllib.load` there,
    `Path.read_text` + `tomllib.loads` here), so the first can succeed while the
    second still fails."""
    original = Path.read_text

    def patched(self, *args, **kwargs):
        return action() if self == target else original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", patched)


@pytest.mark.parametrize("action", [
    lambda: (_ for _ in ()).throw(OSError(errno.EIO, "Input/output error")),
    lambda: "not [ valid = toml",
    lambda: (_ for _ in ()).throw(
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")),
], ids=["eio", "bad-toml", "bad-utf8"])
def test_init_maps_a_raw_table_read_failure_to_the_settings_error(
        world, monkeypatch, capsys, action):
    from memriver.cli import main

    settings_path = world["store"] / "settings.toml"
    before = settings_path.read_bytes()
    _break_raw_table_read(monkeypatch, settings_path, action)
    code = main(["dream", "init", "--yes", "--root", str(world["store"])])
    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (
        1, "", "memriver: settings.toml could not be read\n")
    assert settings_path.read_bytes() == before


def test_an_invalid_dream_table_stops_dream_run_naming_the_field(world, capsys):
    from memriver.cli import main

    (world["store"] / "settings.toml").write_text(
        '[dream]\nexecutor = "claude"\nexecutor_path = "/opt/claude"\nttl_days = 0\n',
        encoding="utf-8")
    code = main(["dream", "run", "--root", str(world["store"])])
    captured = capsys.readouterr()
    assert (code, captured.out) == (1, "")
    assert captured.err == "memriver: settings.toml is invalid: field dream.ttl_days\n"


def _spy_services(monkeypatch) -> list:
    seen: list = []
    real = dream_commands.build_services

    def spy(settings, *, root=None, home=None, classifier=None):
        seen.append(classifier)
        return real(settings, root=root, home=home, classifier=classifier)

    monkeypatch.setattr(dream_commands, "build_services", spy)
    return seen


def test_dream_run_builds_the_configured_classifier(world, monkeypatch):
    (world["store"] / "settings.toml").write_text(
        'max_body_chars = 4000\n[classifier]\nexecutor = "jev"\n'
        'api_key_env = "MEMRIVER_TEST_UNSET_KEY"\n', encoding="utf-8")
    seen = _spy_services(monkeypatch)
    code, _, _ = _run(world)
    assert code == 0 and seen and seen[0] is not None


# --- the composed [dream] table (DreamTable) --------------------------------------

# refused by the whitelist, by the shape (1 is no boolean), and by the whitelist again
# for a well-shaped value
BAD_OVERRIDES = [
    '\n[dream.codex_overrides]\n"features.hooks" = "sk-synthetic"\n',
    ('\n[dream.codex_overrides]\n"model_provider" = "foundry"\n'
     '"model_providers.foundry.requires_openai_auth" = 1\n'),
    '\n[dream.codex_overrides]\n"model" = " "\n',
]


@pytest.mark.parametrize("text", BAD_OVERRIDES)
def test_a_refused_codex_override_stops_init_run_and_report_naming_only_the_field(world,
                                                                                  text):
    _init(world, executor="codex")
    _add_overrides(world, text)
    before = _settings(world)
    for call in (lambda: _init(world, executor="codex"), lambda: _run(world),
                 lambda: _report(world)):
        with pytest.raises(SettingsError) as caught:
            call()
        assert str(caught.value) == "settings.toml is invalid: field dream.codex_overrides"
        assert caught.value.__cause__ is None and "sk-synthetic" not in str(caught.value)
    assert _settings(world) == before


OVERRIDE = '[dream.codex_overrides]\n"features.hooks" = false\n'


def _write_dream(world, lines: str = "", overrides: str = OVERRIDE) -> None:
    """A [dream] table written by hand: codex at the fake executable, `lines`, then the
    overrides subtable."""
    (world["store"] / "settings.toml").write_text(
        f'max_body_chars = 4000\n[dream]\nexecutor = "codex"\n'
        f'executor_path = "{world["bin"] / "codex"}"\n{lines}{overrides}', encoding="utf-8")


# A declared change (spec §9, item 6): the executor keys now come first in the line,
# so a refused override is named before dream's own keys (c005153 named it after
# context_budget_tokens, before claude_settings). The field set, the channel and the
# exit code are c005153's. init rewrites the keys it owns (ttl_days here), so only the
# others remain for it.
@pytest.mark.parametrize(("lines", "load_fields", "init_fields"), [
    ("", "dream.codex_overrides", "dream.codex_overrides"),
    ("ttl_days = -1\n", "dream.codex_overrides, dream.ttl_days", "dream.codex_overrides"),
    ("report_retention_days = 0\n", "dream.codex_overrides, dream.report_retention_days",
     "dream.codex_overrides, dream.report_retention_days"),
    ('report_retention_days = 0\ncontext_budget_tokens = 5\nclaude_settings = "rel"\n',
     ("dream.claude_settings, dream.codex_overrides, dream.report_retention_days, "
      "dream.context_budget_tokens"),
     ("dream.claude_settings, dream.codex_overrides, dream.report_retention_days, "
      "dream.context_budget_tokens")),
])
def test_a_refused_override_is_named_with_every_other_bad_field_in_one_line(
        world, lines, load_fields, init_fields):
    _write_dream(world, lines)
    before = _settings(world)
    for call, fields in ((lambda: _run(world), load_fields),
                         (lambda: _report(world), load_fields),
                         (lambda: _init(world, executor="codex"), init_fields)):
        with pytest.raises(SettingsError) as caught:
            call()
        assert str(caught.value) == f"settings.toml is invalid: field {fields}"
    assert _settings(world) == before


@pytest.mark.parametrize(("options", "lines", "overrides", "expected"), [
    ({"ttl_days": -1}, "", OVERRIDE,
     "settings.toml is invalid: field dream.codex_overrides"),
    ({"at": "25:00"}, "", OVERRIDE,
     "settings.toml is invalid: field dream.codex_overrides"),
    # the declared order change again: c005153 printed
    # "dream.report_retention_days, dream.codex_overrides"
    ({"at": "25:00"}, "report_retention_days = 0\n", OVERRIDE,
     "settings.toml is invalid: field dream.codex_overrides, dream.report_retention_days"),
    ({"at": "25:00"}, "", "",                       # no bad file value: the given one is named
     (2, "refused: invalid value given for schedule_at; nothing was written\n")),
])
def test_init_names_a_bad_file_value_before_a_bad_given_one(world, options, lines, overrides,
                                                            expected):
    _write_dream(world, lines, overrides)
    before = _settings(world)
    if isinstance(expected, str):
        # the settings error cli.main prints (exit 1), never init's own refusal on stdout
        with pytest.raises(SettingsError) as caught:
            _init(world, executor="codex", **options)
        assert str(caught.value) == expected
    else:
        assert _init(world, executor="codex", **options) == expected
    assert _settings(world) == before


def test_the_cli_prints_the_whole_line_and_exits_1(world, capsys):
    _write_dream(world, "ttl_days = -1\n")
    code = cli.main(["dream", "run", "--root", str(world["store"])])
    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (
        1, "", ("memriver: settings.toml is invalid: field dream.codex_overrides, "
               "dream.ttl_days\n"))


def test_a_jev_executor_in_the_dream_table_is_refused(world, capsys):
    # dream needs generated text: jev answers only bounded decisions
    (world["store"] / "settings.toml").write_text(
        '[dream]\nexecutor = "jev"\nexecutor_path = "/opt/bin/jev"\n', encoding="utf-8")
    code = cli.main(["dream", "run", "--root", str(world["store"])])
    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (
        1, "", "memriver: settings.toml is invalid: field dream.executor\n")


def test_the_dream_model_reaches_the_executor_argv(world):
    (world["store"] / "settings.toml").write_text(
        f'[dream]\nexecutor = "claude"\nexecutor_path = "{world["bin"] / "claude"}"\n'
        'model = "haiku"\n', encoding="utf-8")
    dream = load_dream_settings(world["store"], model=DreamTable)
    executor = make_executor(dream, env={}, scratch_prefix=DREAM_SCRATCH_PREFIX)
    assert executor.argv(system_prompt="S", schema={"type": "object"})[-2:] == [
        "--model", "haiku"]


class NotLoggedIn:
    """Every call fails the way a harness that is not logged in does."""

    name, harness = "fake", "fake-harness"

    def run(self, *, system_prompt, prompt, schema, timeout_s):
        return ExecutorResult(error="login")


def test_the_first_login_failure_of_a_run_carries_memrivers_hint(world):
    second = world["tmp"] / "second"
    second.mkdir()
    services = world["services"]
    other = services.project.init_project("second", services.project.plan_root(str(second)))
    for project_id, body in ((world["project"].id, "a demo fact"), (other.id, "a second fact")):
        services.memory.apply([Create(project_id=project_id, type="project",
                                      description="cue", body=body)], changed_by="test")
    _init(world)
    code, out, _ = _run(world, executor_factory=lambda dream: NotLoggedIn())
    line = "executor fake: login failure — " + FAILURE_HINTS["login"]
    assert code == 0 and out.count(line + "\n") == 1
