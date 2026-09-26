"""`memriver dream`: set up, run, report on and undo the maintenance run.

Every data rule is the core's (MaintenanceService) and every pipeline rule
memriver_dream's; this module resolves executables and the store root,
writes the [dream] table, installs the schedule and words the output. It
never prints a memory body, a summary, a prompt or a secret: a cue is shown
only when the text it comes from passes the content policy.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO

import tomlkit
from memriver_core import MemoryNotFound, StorageFailure, UndoConflict
from memriver_core.bootstrap import build_maintenance_service, build_service
from memriver_core.models import Change, DreamRun
from memriver_core.models import now as _now
from memriver_core.settings import (
    SETTINGS_FILENAME,
    Settings,
    SettingsError,
    load_settings,
)
from memriver_dream import run_dream
from memriver_dream.run import MODEL_PHASES
from memriver_dream.settings import (
    DEFAULT_DREAM_SCHEDULE_AT,
    DEFAULT_DREAM_TTL_DAYS,
    DREAM_DIRECTORY,
    DREAM_LAUNCH_AGENT_LABEL,
    DREAM_LOG_FILENAME,
    DREAM_TOOL_OUTPUT_CHARS,
    DreamSettings,
    load_dream_settings,
)
from pydantic import ValidationError

from . import launch_agent, views
from .executors import make_executor, missing_env
from .install import replace_atomically
from .project_commands import _confirm
from .project_context import visible
from .transcripts import HarnessTranscripts

STORE_FAILURE = "memriver dream: the memory store could not be read or written\n"
_PHASE_TITLES = {"secrets": "secrets (safety re-scan)", "summarize": "session summaries",
                 "consolidate": "consolidation", "retire": "TTL retirement"}
_PHASE_OF_KIND = {"secret": "secrets", "merge": "consolidate", "rewrite": "consolidate",
                  "extract": "consolidate", "unsafe": "consolidate", "retire": "retire"}
UV_CACHE_REFUSAL = ("refused: the memriver running this command lives in uv's cache (uvx), "
                    "which uv may delete at any time; install it persistently "
                    "(uv tool install memriver) and run memriver dream init from there\n")


def _services(settings: Settings):
    return (build_service(settings, root=settings.root),
            build_maintenance_service(settings, root=settings.root))


def _store_ready(settings: Settings) -> bool:
    try:
        return build_service(settings, root=settings.root).global_project_id() is not None
    except StorageFailure:
        return False


def _in_uv_cache(path: str, env: Mapping[str, str], home: Path) -> bool:
    """Whether `path` runs from an environment uv treats as disposable cache (uvx)."""
    real = Path(os.path.realpath(path))
    cache = Path(env.get("UV_CACHE_DIR") or home / ".cache" / "uv")
    return real.is_relative_to(os.path.realpath(cache)) or "archive-v0" in real.parts


def _memriver_path(env: Mapping[str, str], home: Path, executable: str,
                   which: Callable[[str], str | None]) -> str | None:
    """A persistent memriver entry point: the console script beside the running
    interpreter, else the one on PATH. None when this memriver runs from uv's cache:
    a memriver found elsewhere may be another version, so it is never scheduled
    instead."""
    # the environment's bin directory: the interpreter itself links out of the cache
    if _in_uv_cache(os.path.dirname(executable), env, home):
        return None
    beside = Path(executable).parent / "memriver"
    if beside.is_file():
        return str(beside)
    found = which("memriver")
    return os.path.abspath(found) if found else None


def _existing_table(path: Path) -> dict:
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8")).get("dream")
    except FileNotFoundError:
        return {}
    return dict(table) if isinstance(table, dict) else {}


def _write_dream_table(path: Path, values: dict) -> None:
    """Set the [dream] keys init owns, keeping every other line of the file."""
    try:
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        document = tomlkit.document()
    table = document.get("dream")
    if not isinstance(table, Mapping):
        table = tomlkit.table()
        document["dream"] = table
    for key, value in values.items():
        table[key] = value
    replace_atomically(path, tomlkit.dumps(document).encode("utf-8"), 0o600, os.replace)


def _invalid_key(error: Mapping) -> str:
    """The offending key; for codex_overrides the validator's fixed reason, which names
    the inner key and never its value."""
    if error["loc"][0] == "codex_overrides" and error["type"] == "value_error":
        return str(error["ctx"]["error"])
    return str(error["loc"][0])


def _agent_env(home: Path, memriver: str, executor_path: str, root: Path) -> dict[str, str]:
    path: list[str] = []
    for directory in (os.path.dirname(memriver), os.path.dirname(executor_path), "/usr/bin",
                      "/bin"):
        if directory not in path:
            path.append(directory)
    # always the absolute root init chose: launchd's working directory is not the user's
    return {"HOME": str(home), "PATH": ":".join(path), "MEMRIVER_ROOT": str(root)}


def run_init(*, executor: str | None, ttl_days: int | None, at: str | None, yes: bool,
             root: Path | None, stdin_is_tty: bool, input_fn: Callable[[str], str],
             stdout: IO[str], home: Path, env: Mapping[str, str],
             which: Callable[[str], str | None] = shutil.which,
             launchctl: launch_agent.Launchctl = launch_agent.run_launchctl,
             platform: str = sys.platform, uid: int | None = None,
             memriver_path: str | None = None, executable: str = sys.executable,
             label: str = DREAM_LAUNCH_AGENT_LABEL) -> int:
    # an unusable settings.toml or MEMRIVER_* value raises SettingsError: cli.main
    # prints its one line
    settings = load_settings(root_override=root)
    if not _store_ready(settings):
        stdout.write("refused: the memory store is not initialized; run memriver install first\n")
        return 2
    store = Path(os.path.abspath(settings.root))
    try:
        current = load_dream_settings(store)
    except SettingsError:
        # init rewrites the keys it owns: the table as it will stand is checked below
        current = None
    name = executor or (current.executor if current else None) or next(
        (candidate for candidate in ("claude", "codex") if which(candidate)), None)
    if name is None:
        stdout.write("refused: neither claude nor codex is on PATH; install one, or pass "
                     "--executor\n")
        return 2
    found = which(name)
    if found is None:
        stdout.write(f"refused: {name} is not on PATH\n")
        return 2
    memriver = memriver_path or _memriver_path(env, home, executable, which)
    if memriver is None or _in_uv_cache(memriver, env, home):
        stdout.write(UV_CACHE_REFUSAL)
        return 2
    values = {"executor": name, "executor_path": os.path.abspath(found),
              "ttl_days": ttl_days or (current.ttl_days if current else DEFAULT_DREAM_TTL_DAYS),
              "schedule_at": at or (current.schedule_at if current
                                    else DEFAULT_DREAM_SCHEDULE_AT)}
    settings_file = store / SETTINGS_FILENAME
    try:
        # the whole table as it will stand: a bad key init does not own is never kept
        table = DreamSettings(**(_existing_table(settings_file) | values))
    except ValidationError as err:
        keys = sorted({_invalid_key(error) for error in err.errors() if error["loc"]})
        stdout.write(f"refused: the [dream] table in {SETTINGS_FILENAME} has invalid keys: "
                     f"{', '.join(keys)}; fix or remove them, then run memriver dream init "
                     "again\n")
        return 2
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        stdout.write(f"refused: {SETTINGS_FILENAME} could not be read; fix it first\n")
        return 2
    variables = sorted({value for key, value in table.codex_overrides.items()
                        if key.endswith(".env_key")}) if name == "codex" else []
    missing = missing_env(table.codex_overrides, env) if name == "codex" else []
    if missing:
        # a run without them would reach no provider or Codex's default one
        stdout.write(f"refused: the Codex provider in [dream.codex_overrides] needs "
                     f"{', '.join(missing)} set in this environment; nothing was written\n")
        return 2
    log_path = store / DREAM_DIRECTORY / DREAM_LOG_FILENAME
    command = [memriver, "dream", "run", "--trigger", "schedule"]
    where = "LaunchAgent" if platform == "darwin" else "you add it to your scheduler"
    plan = (f"memriver dream init\n"
            f"  settings: {visible(str(settings_file))} [dream]\n"
            f"  executor: {name} ({visible(values['executor_path'])})\n"
            f"  ttl: {values['ttl_days']} days, longer for memories that are read\n"
            f"  schedule: daily at {values['schedule_at']} ({where})\n"
            f"  command: {visible(shlex.join(command))}\n"
            f"  log: {visible(str(log_path))}\n"
            + (f"  provider variables: {', '.join(variables)} -- not written to the "
               "schedule; make them visible to it, or the scheduled run refuses\n"
               if variables else "")
            + "  stop it with: memriver dream uninstall\n")
    code = _confirm(plan, yes=yes, stdin_is_tty=stdin_is_tty, input_fn=input_fn, stdout=stdout)
    if code is not None:
        return code
    try:
        _write_dream_table(settings_file, values)
    except OSError:
        stdout.write(f"refused: {SETTINGS_FILENAME} could not be written; nothing was "
                     "changed\n")
        return 1
    try:
        (store / DREAM_DIRECTORY).mkdir(mode=0o700, exist_ok=True)
    except OSError:
        stdout.write(f"refused: {visible(str(store / DREAM_DIRECTORY))} could not be "
                     "created; the settings were written, the schedule was not installed\n")
        return 1
    if platform != "darwin":
        # `env` makes the assignment a word of its own, so a quoted root with a space works
        line = shlex.join(["env", f"MEMRIVER_ROOT={store}", *command])
        stdout.write(f"settings written; schedule this command daily at "
                     f"{values['schedule_at']}:\n{visible(line)}\n")
        return 0
    plist = launch_agent.render(program=command, schedule_at=values["schedule_at"],
                                env=_agent_env(home, memriver, values["executor_path"], store),
                                log_path=log_path, label=label)
    try:
        launch_agent.install(home=home, plist=plist, uid=os.getuid() if uid is None else uid,
                             launchctl=launchctl, label=label)
    except launch_agent.RestoreFailed:
        stdout.write("refused: the schedule could not be replaced, and the previous one "
                     "could not be restored; check "
                     f"{visible(str(launch_agent.plist_path(home, label)))} and "
                     f"launchctl print gui/<uid>/{label}; the settings were written\n")
        return 1
    except (launch_agent.LaunchctlFailed, OSError):
        # no claim about the previous schedule: launchd may not have confirmed its state
        stdout.write("refused: the schedule could not be installed, or launchd could not "
                     "confirm its state; check "
                     f"{visible(str(launch_agent.plist_path(home, label)))} and "
                     f"launchctl print gui/<uid>/{label}; the settings were written\n")
        return 1
    stdout.write(f"installed the schedule: "
                 f"{visible(str(launch_agent.plist_path(home, label)))}\n")
    return 0


def run_run(*, phase: str | None, trigger: str, root: Path | None, stdout: IO[str],
            stderr: IO[str], executor_factory=None, transcripts=None) -> int:
    # an unusable settings.toml, [dream] table or MEMRIVER_* value raises
    # SettingsError: cli.main prints its one line
    settings = load_settings(root_override=root)
    dream = load_dream_settings(settings.root)
    if phase is not None and dream is None:
        stderr.write("memriver dream: --phase needs an executor; run memriver dream init\n")
        return 1
    if dream is not None and dream.executor == "codex":
        missing = missing_env(dream.codex_overrides, os.environ)
        if missing:
            # never Codex's default provider instead (a scheduled job sees only its own
            # environment)
            stderr.write(f"memriver dream: the Codex provider in [dream.codex_overrides] "
                         f"needs {', '.join(missing)} in the environment; nothing was run\n")
            return 1
    try:
        service, maintenance = _services(settings)
        if maintenance.global_project_id() is None:
            stderr.write("memriver dream: the memory store is not initialized; run memriver "
                         "install\n")
            return 1
        executor = sources = None
        if dream is not None:
            executor = (executor_factory or (lambda table: make_executor(
                table, env=os.environ)))(dream)
            sources = transcripts or HarnessTranscripts(tool_output_chars=DREAM_TOOL_OUTPUT_CHARS)
        report = run_dream(maintenance, executor, sources, settings.root, dream, _now(),
                           trigger=trigger,
                           phases=(phase,) if phase else MODEL_PHASES,
                           log=lambda line: stdout.write(f"{_now()} {line}\n"))
        if report.status == "skipped":         # run_dream logged "skipped: locked"
            return 0
        recorded = maintenance.run(report.run_id)
        stdout.write(render_run(recorded, service=service, maintenance=maintenance)
                     if recorded else "")
    except StorageFailure:
        stderr.write(STORE_FAILURE)
        return 1
    return 0


def _cue(service, maintenance, memory_id: str) -> str:
    try:
        memory = service.show(memory_id, include_deleted=True)
    except (MemoryNotFound, StorageFailure):
        return "(gone)"
    # a quarantined memory's cue can be the secret itself
    if not maintenance.text_passes_policy(views.cue_source(memory)):
        return "(cue withheld)"
    return views.cue(memory)


def _change_lines(change: Change, service, maintenance, global_id: str | None) -> list[str]:
    where = "global" if change.project_id == global_id else change.project_id
    undone = "  (undone)" if change.undone_at else ""
    lines = [f"  {change.kind}  {where}  change {change.change_id}{undone}"]
    lines += [f"    {row.id}  {_cue(service, maintenance, row.id)}" for row in change.rows]
    lines.append(f"    {'rule' if change.kind == 'secret' else 'reason'}: {change.reason}")
    lines.append(f"    undo: memriver dream undo {change.change_id}")
    return lines


def render_run(run: DreamRun, *, service, maintenance) -> str:
    executor = f"executor {run.executor}" if run.executor else "no executor configured"
    lines = [f"run {run.run_id}  {run.started_at}  {run.trigger}  {run.status}  {executor}"]
    if not run.report and run.status in ("failed", "running"):
        lines.append("interrupted: counts unknown; the changes it committed are listed below")
    global_id = maintenance.global_project_id()
    # the change log is the one record of what a run changed, even a run that died
    by_phase: dict[str, list[Change]] = {}
    for change in maintenance.changes_of_run(run.run_id):
        by_phase.setdefault(_PHASE_OF_KIND[change.kind], []).append(change)
    for name, title in _PHASE_TITLES.items():
        phase = run.report.get(name)
        changes = by_phase.get(name, [])
        if not isinstance(phase, dict) and not changes:
            continue
        if isinstance(phase, dict):
            lines.append(f"{title}: done {phase.get('done', 0)}, failed "
                         f"{phase.get('failed', 0)}, skipped {phase.get('skipped', 0)}")
            outcomes = phase.get("outcomes") or {}
            if outcomes:
                lines.append("  outcomes: " + ", ".join(f"{outcome} {count}" for outcome, count
                                                        in sorted(outcomes.items())))
        else:
            lines.append(f"{title}:")
        for change in changes:
            lines += _change_lines(change, service, maintenance, global_id)
        for item in (phase.get("items") or ()) if isinstance(phase, dict) else ():
            if "decision" in item:          # session outcomes are shown as counts
                lines.append(f"  {item['decision']}  {item['memory_id']}  "
                             f"{_cue(service, maintenance, item['memory_id'])}")
    # every stored field is neutralized, whatever wrote it
    return "".join(f"{visible(line)}\n" for line in lines)


def run_report(run_id: str | None, *, list_count: int | None, root: Path | None,
               stdout: IO[str]) -> int:
    settings = load_settings(root_override=root)
    try:
        service, maintenance = _services(settings)
        if list_count is not None:
            runs = maintenance.runs(list_count)
            for run in runs:
                counts = ", ".join(f"{name} {phase.get('done', 0)}/{phase.get('failed', 0)}/"
                                   f"{phase.get('skipped', 0)}"
                                   for name, phase in run.report.items()
                                   if isinstance(phase, dict))
                stdout.write(visible(f"{run.run_id}  {run.started_at}  {run.trigger}  "
                                     f"{run.status}  {counts}") + "\n")
            if not runs:
                stdout.write("(no dream runs yet)\n")
            return 0
        run = maintenance.run(run_id) if run_id else next(iter(maintenance.runs(1)), None)
        if run is None:
            stdout.write(f"no such run: {visible(run_id[:255])}\n" if run_id
                         else "(no dream runs yet)\n")
            return 2 if run_id else 0
        stdout.write(render_run(run, service=service, maintenance=maintenance))
    except StorageFailure:
        stdout.write("refused: the memory store could not be read\n")
        return 2
    return 0


def run_undo(change_id: str, *, yes: bool, root: Path | None, stdin_is_tty: bool,
             input_fn: Callable[[str], str], stdout: IO[str]) -> int:
    settings = load_settings(root_override=root)
    try:
        service, maintenance = _services(settings)
        change = maintenance.change(change_id)
        if change is None:
            stdout.write(f"no such change: {visible(change_id[:255])}\n")
            return 2
        where = "global" if change.project_id == maintenance.global_project_id() \
            else change.project_id
        plan = (f"memriver dream undo: {change.change_id} ({change.kind} in {where})\n"
                + "".join(f"  {row.id}  {_cue(service, maintenance, row.id)}\n"
                          for row in change.rows))
        code = _confirm(plan, yes=yes, stdin_is_tty=stdin_is_tty, input_fn=input_fn,
                        stdout=stdout)
        if code is not None:
            return code
        result = maintenance.undo(change_id)
    except UndoConflict as err:
        stdout.write("refused: these memories changed since the change was applied; nothing "
                     f"was undone: {', '.join(err.ids)}\n")
        return 2
    except StorageFailure:
        stdout.write("refused: the memory store could not be read or written\n")
        return 2
    if result.status == "already-undone":
        stdout.write(f"{change_id} was already undone\n")
        return 0
    stdout.write(f"undone {change_id}: {', '.join(result.ids)}\n")
    return 0


def run_uninstall(*, home: Path, stdout: IO[str],
                  launchctl: launch_agent.Launchctl = launch_agent.run_launchctl,
                  uid: int | None = None, platform: str = sys.platform,
                  label: str = DREAM_LAUNCH_AGENT_LABEL) -> int:
    if platform != "darwin":
        stdout.write("memriver installs no schedule on this platform; remove the entry you "
                     "added to your scheduler\n")
        return 0
    try:
        removed = launch_agent.uninstall(home=home, uid=os.getuid() if uid is None else uid,
                                         launchctl=launchctl, label=label)
    except launch_agent.LaunchctlFailed:
        stdout.write("refused: launchd did not confirm the schedule is unloaded; the plist "
                     "was kept\n")
        return 1
    except OSError:
        stdout.write("refused: the schedule could not be removed: launchctl could not be "
                     "run, or the plist could not be deleted; check "
                     f"{visible(str(launch_agent.plist_path(home, label)))} and "
                     f"launchctl print gui/<uid>/{label}\n")
        return 1
    stdout.write("removed the schedule; settings and data are kept\n" if removed
                 else "no schedule installed\n")
    return 0
