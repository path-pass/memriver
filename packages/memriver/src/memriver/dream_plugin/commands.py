"""`memriver dream`: set up, run and report on the offline maintenance run.

Every data rule is core's and every pipeline rule memriver_dream's; this module
resolves executables and the store root, writes the [dream] table, installs the
schedule, runs dream and prints its report files. It never prints a memory body, a
summary, a prompt or a secret: a report file holds none (memriver_dream checks every
line before writing it), and every printed line has its invisible characters
neutralized.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sqlite3
import sys
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO, Literal

import tomlkit
from memriver_core import StorageFailure, StoreNeedsUpgrade
from memriver_core.bootstrap import build_services
from memriver_core.models import now as _now
from memriver_core.settings import (
    SETTINGS_FILENAME,
    Settings,
    SettingsError,
    load_settings,
    validation_fields,
)
from memriver_dream import (
    DEFAULT_DREAM_REPORT_RETENTION_DAYS,
    DEFAULT_DREAM_TTL_DAYS,
    DREAM_DIRECTORY,
    DREAM_LOG_FILENAME,
    DREAM_REPORTS_DIRECTORY,
    DREAM_TOOL_OUTPUT_CHARS,
    DreamSettings,
    check_dream_table,
    find_run,
    load_dream_settings,
    recent_runs,
    run_dream,
)
from pydantic import ValidationError, field_validator

from ..classifier_plugin import build_classifier
from ..executor import make_executor
from ..executor.harness import missing_env
from ..install import replace_atomically
from ..project_commands import _confirm
from ..project_context import visible
from ..settings import (
    DEFAULT_DREAM_SCHEDULE_AT,
    DREAM_LAUNCH_AGENT_LABEL,
    DREAM_SCRATCH_PREFIX,
    SCHEDULE_AT_RE,
    ExecutorSettings,
    load_classifier_settings,
)
from ..views import unsupported_store
from . import schedule as launch_agent
from .adapter import FAILURE_HINTS, DreamExecutor
from .transcripts import HarnessTranscripts

STORE_FAILURE = "memriver dream: the memory store could not be read or written\n"
# dream's own files: dream.db, the run lock, the report directory and files
DREAM_FAILURE = "memriver dream: the dream store or its files could not be read or written\n"
SKIPPED = "memriver dream: another run holds the lock; this run was skipped\n"
INTERRUPTED = ("(interrupted: this run stopped before it finished; later sections are "
               "unknown)\n")
UV_CACHE_REFUSAL = ("refused: the memriver running this command lives in uv's cache (uvx), "
                    "which uv may delete at any time; install it persistently "
                    "(uv tool install memriver) and run memriver dream init from there\n")


class DreamTable(DreamSettings, ExecutorSettings):
    """[dream] as the memriver dream commands read it: memriver_dream's policy keys,
    the executor keys memriver shares with [classifier] (the Codex provider whitelist
    included) and the schedule. Every [dream] load here passes it as `model=`, so one
    validation names every bad key of the table, never a value."""

    executor: Literal["claude", "codex"]        # dream needs generated text: never jev
    # required, as before the composition: a missing path is named even beside a bad
    # executor
    executor_path: str
    schedule_at: str = DEFAULT_DREAM_SCHEDULE_AT

    @field_validator("schedule_at")
    @classmethod
    def _hh_mm(cls, value: str) -> str:
        if not SCHEDULE_AT_RE.fullmatch(value):
            raise ValueError("schedule_at must be HH:MM")
        return value


def _store_ready(settings: Settings) -> bool:
    """Whether the store has its global project. A store below the schema this
    memriver needs raises StoreNeedsUpgrade: it is not "uninitialized", it is refused."""
    try:
        return build_services(settings, root=settings.root).project.global_project_id() \
            is not None
    except StoreNeedsUpgrade:
        raise
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


def _variants(table: Mapping, owned: Mapping) -> list:
    """The table's keys that spell a key init owns in another case: the run matches
    keys case-insensitively, first spelling winning, so each would shadow init's."""
    return [key for key in table if isinstance(key, str) and key.lower() in owned
            and key not in owned]


def _with(table: dict, values: dict) -> dict:
    """The table as init leaves it: every spelling of an owned key replaced by `values`."""
    kept = {key: value for key, value in table.items() if key not in _variants(table, values)}
    return kept | values


def _salvaged(raw: Mapping, key: str) -> object | None:
    """`raw`'s own value for `key` (matched case-insensitively, as the run matches it),
    if it validates on its own; None otherwise. Used only when the whole [dream] table
    failed to load, so one bad sibling key does not throw out an otherwise-good owned
    value along with it."""
    for name, value in raw.items():
        if isinstance(name, str) and name.lower() == key:
            try:
                return getattr(check_dream_table(
                    {"executor": "claude", "executor_path": "/x", key: value}, model=DreamTable), key)
            except ValidationError:
                return None
    return None


def _write_dream_table(path: Path, values: dict) -> None:
    """Set the [dream] keys init owns, keeping every other line of the file."""
    try:
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        document = tomlkit.document()
    except (tomlkit.exceptions.ParseError, UnicodeDecodeError, OSError):
        # the file changed since the plan was read and confirmed -- edited by hand,
        # or by another process -- into something this second read cannot use
        # either: named the same one-line way as any other unusable settings.toml,
        # never a traceback; nothing is written
        raise SettingsError(unreadable=True) from None
    table = document.get("dream")
    if not isinstance(table, Mapping):
        table = tomlkit.table()
        document["dream"] = table
    for key in _variants(table, values):         # the same rule as _with
        del table[key]
    for key, value in values.items():
        table[key] = value
    # write through a symlinked settings.toml to its resolved target and keep the link:
    # os.replace on the link itself would silently swap it for a regular file. The
    # temp file still lands beside the target (replace_atomically uses its parent), so
    # the swap stays atomic and exclusive.
    replace_atomically(path.resolve(), tomlkit.dumps(document).encode("utf-8"), 0o600,
                       os.replace)


# a `#!` line is read this far at most; the macOS kernel reads no more of one
_SHEBANG_BYTES = 512


def _node_script(path: str) -> bool:
    """Whether `path` is a script whose `#!` line names node (`#!/usr/bin/env node`,
    `#!/opt/node/bin/node`), as npm installs a CLI: launchd's PATH must then hold node.
    Anything that is not a readable regular file is taken as native."""
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as file:
            first = file.readline(_SHEBANG_BYTES)
    except OSError:
        return False
    return first.startswith(b"#!") and any(os.path.basename(word) == b"node"
                                           for word in first[2:].split())


def _agent_env(home: Path, memriver: str, executor_path: str, root: Path,
               node: str | None = None) -> dict[str, str]:
    """The LaunchAgent's environment; `node`, when given, is the node a node-script
    executor needs, its directory placed right after the executor's."""
    path: list[str] = []
    for directory in (os.path.dirname(memriver), os.path.dirname(executor_path),
                      *([os.path.dirname(os.path.abspath(node))] if node else []),
                      "/usr/bin", "/bin"):
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
    try:
        ready = _store_ready(settings)
    except StoreNeedsUpgrade as err:
        stdout.write(f"refused: {unsupported_store(err)}\n")
        return 1
    if not ready:
        stdout.write("refused: the memory store is not initialized; run memriver install first\n")
        return 2
    store = Path(os.path.abspath(settings.root))
    settings_file = store / SETTINGS_FILENAME
    try:
        # load_settings above already read this file once, through a different call
        # (tomllib.load on an open binary handle); this second, dream-specific read
        # can still fail on its own -- a race, a transient I/O fault -- and must be
        # named the same one-line way, never a traceback with the path in it
        raw_table = _existing_table(settings_file)
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        raise SettingsError(unreadable=True) from None
    try:
        current = load_dream_settings(store, model=DreamTable)
    except SettingsError:
        # init rewrites the keys it owns: the table as it will stand is checked below.
        # A key it owns may still be individually valid even though a sibling key sank
        # the whole table -- that value is kept rather than silently reset.
        current = None
    name = executor or (current.executor if current else _salvaged(raw_table, "executor")) \
        or next((candidate for candidate in ("claude", "codex") if which(candidate)), None)
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
              "ttl_days": ttl_days or (current.ttl_days if current
                                       else _salvaged(raw_table, "ttl_days"))
                          or DEFAULT_DREAM_TTL_DAYS,
              "schedule_at": at or (current.schedule_at if current
                                    else _salvaged(raw_table, "schedule_at"))
                             or DEFAULT_DREAM_SCHEDULE_AT}
    try:
        # the whole table as it will stand, read the way the run reads it: a bad key
        # init does not own is never kept
        table = check_dream_table(_with(raw_table, values), model=DreamTable)
    except ValidationError as err:
        fields = validation_fields(err)
        given = [name for name in fields if name in values]
        if len(given) < len(fields):
            # a bad value the file itself holds: the settings error cli.main prints
            raise SettingsError(tuple(f"dream.{name}" for name in fields
                                      if name not in values)) from None
        stdout.write(f"refused: invalid value given for {', '.join(given)}; nothing was "
                     "written\n")
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
    # the LaunchAgent's PATH is fixed at init: node is looked up on init's own PATH
    node_script = platform == "darwin" and _node_script(values["executor_path"])
    node = which("node") if node_script else None
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
            + (f"  warning: {name} is a node script and node is not on PATH; the scheduled "
               "run cannot start it -- put node on PATH and run memriver dream init again\n"
               if node_script and node is None else "")
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
                                env=_agent_env(home, memriver, values["executor_path"], store,
                                               node),
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


def _report_path(root: Path, report_file: str) -> Path:
    # the file name only, as memriver_dream reads it: a hand-edited row never points
    # the read elsewhere
    return Path(root) / DREAM_DIRECTORY / DREAM_REPORTS_DIRECTORY / Path(report_file).name


def _print_report(path: Path, stdout: IO[str]) -> bool:
    """The report file as it is, each line with its invisible characters neutralized;
    False when it cannot be read."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        stdout.write(f"(the report file {visible(path.name)} could not be read)\n")
        return False
    # split on "\n" only: str.splitlines also breaks at U+2028 and friends, which would
    # let one stored line print as two
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    stdout.write("".join(f"{visible(line)}\n" for line in lines))
    return True


def run_run(*, phase: str | None, trigger: str, root: Path | None, stdout: IO[str],
            stderr: IO[str], executor_factory=None, transcripts=None) -> int:
    # an unusable settings.toml, [dream] table or MEMRIVER_* value raises
    # SettingsError: cli.main prints its one line
    settings = load_settings(root_override=root)
    dream = load_dream_settings(settings.root, model=DreamTable)
    # dream's changes are checked only when [classifier] checks dream writes; otherwise no
    # executor is built for dream at all (spec §8)
    table = load_classifier_settings(settings.root)
    classifier = (build_classifier(table, os.environ)
                  if table is not None and table.dream_writes else None)
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
        services = build_services(settings, root=settings.root, classifier=classifier)
        if services.project.global_project_id() is None:
            stderr.write("memriver dream: the memory store is not initialized; run memriver "
                         "install\n")
            return 1
        executor = sources = None
        if dream is not None:
            executor = (executor_factory or (lambda table: DreamExecutor(make_executor(
                table, env=os.environ, scratch_prefix=DREAM_SCRATCH_PREFIX))))(dream)
            sources = transcripts or HarnessTranscripts(tool_output_chars=DREAM_TOOL_OUTPUT_CHARS)
        run = run_dream(services, executor, sources, dream, root=Path(settings.root),
                        now=_now(), trigger=trigger, phases={phase} if phase else None,
                        failure_hints=FAILURE_HINTS)
    except StoreNeedsUpgrade as err:
        stderr.write(f"memriver dream: {unsupported_store(err)}\n")
        return 1
    except StorageFailure:
        stderr.write(STORE_FAILURE)
        return 1
    except (sqlite3.Error, OSError):
        # dream's own storage: a fixed line, never the exception's text (it can carry
        # paths or SQL); anything else is a bug and keeps its traceback
        stderr.write(DREAM_FAILURE)
        return 1
    if run.status == "skipped":
        stdout.write(SKIPPED)
        return 0
    if trigger == "schedule":
        # stdout is dream.log, which nothing prunes: one line pointing at the report,
        # so the report text stays under report_retention_days (and a hard delete)
        stdout.write(f"run {run.run_id} {run.status}; memriver dream report {run.run_id}\n")
        return 0
    if not _print_report(_report_path(settings.root, run.report_file), stdout):
        # the run itself is not undone -- its row and report file stand as they are --
        # only the final read-back failed; the same fixed line as any other dream-file
        # fault, never the exception's text
        stderr.write(DREAM_FAILURE)
        return 1
    return 0


def run_report(run_id: str | None, *, list_count: int | None, root: Path | None,
               stdout: IO[str]) -> int:
    settings = load_settings(root_override=root)
    dream = load_dream_settings(settings.root, model=DreamTable)  # its report_retention_days
    store_root = Path(settings.root)
    try:
        # the same check run and init make, through the same core entry point: a
        # store below the schema this memriver needs is refused before the dream
        # lock is taken, dream.db is opened, or retention deletes anything. An
        # uninitialized or unreadable store is not this refusal's concern: the
        # reports come from dream.db alone.
        _store_ready(settings)
    except StoreNeedsUpgrade as err:
        stdout.write(f"memriver dream: {unsupported_store(err)}\n")
        return 1
    days = (DEFAULT_DREAM_REPORT_RETENTION_DAYS if dream is None
            else dream.report_retention_days)
    try:
        # retention runs before a report, under the run lock; while a run holds the
        # lock nothing is deleted, and a "running" row is that live run
        if list_count is not None:
            runs, run = recent_runs(store_root, limit=list_count, retention_days=days), None
        else:
            runs, run = [], find_run(store_root, run_id, retention_days=days)
    except (sqlite3.Error, OSError):
        stdout.write(DREAM_FAILURE)
        return 1
    if list_count is not None:
        for listed in runs:
            stdout.write(visible(f"{listed.run_id}  {listed.started_at}  {listed.trigger}  "
                                 f"{listed.status}") + "\n")
        if not runs:
            stdout.write("(no dream runs yet)\n")
        return 0
    if run is None:
        if run_id:
            stdout.write(f"no such run: {visible(run_id[:255])}\n")
            return 2
        stdout.write("(no dream runs yet)\n")
        return 0
    code = 0 if _print_report(run.report_path, stdout) else 2
    if run.status == "interrupted":
        stdout.write(INTERRUPTED)
    return code


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
