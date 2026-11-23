"""Drives the Python and the Rust memriver as processes and compares what they leave.

Isolation: `run` is the only place a process is started. It builds the child's
environment from scratch and refuses a home or a store outside the test's
temporary directory (`TMP_PATH`, set per test by conftest.py).

Comparison: a `Case` runs each step on two copies of a store, one per
implementation, and snapshots both before and after. Ids and timestamps are
generated, so they differ between the sides; they are renamed -- only in the
columns and output fields declared below -- to names that say which object is
meant (`<memory:2>`, `<t:5>`). Everything else, free text included, is compared
as it is. A generated value is renamed only when it is what it should be: an id
in the id form, a timestamp that is a real time in the stored form and was taken
while its step ran (in a column that only moves forward it also follows the row's
previous time, and passes the clock only when that time is ahead of it). Anything
else fails the step, so a renamed value never hides a wrong one. A name belongs
to a kind of object: the same text as an id of another kind, or in free text, is
left alone.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .differences import DIFFERENCES, MASKS

REPO = Path(__file__).resolve().parent.parent
SEEDS = Path(__file__).resolve().parent / "seeds"
PY = [sys.executable, "-m", "memriver.cli"]
RS = [str(REPO / "target" / "release" / "memriver")]
GIT = ["git", "-c", "user.name=compat", "-c", "user.email=compat@example.invalid"]
# the Python implementation's own API over the store of the environment: for
# writes no command line offers. The code to run is the one argument.
PY_API = [sys.executable, "-c", """
import json, os, sys
from pathlib import Path
from memriver_core.bootstrap import build_services
from memriver_core.models.changes import Create, SourceRef, Update
from memriver_core.settings import load_settings
ROOT, HOME, WORK = Path(os.environ["MEMRIVER_ROOT"]), Path(os.environ["HOME"]), Path.cwd()
services = build_services(load_settings(root_override=ROOT), root=ROOT, home=HOME)
def project_id(name):
    return next(p.id for p in services.project.list_projects() if p.name == name)
exec(sys.argv[1])
"""]

TMP_PATH: Path | None = None    # the running test's tmp_path

# The Python the recorded matrices are what it answers, to the patch: later patch releases
# of 3.14 already differ on recorded cases, and 3.15 reads another TOML.
RECORDED_WITH = (3, 14, 0)
NOT_THE_RECORDING_PYTHON = (
    "the matrices are recorded with Python " + ".".join(map(str, RECORDED_WITH))
    + " exactly; this is " + sys.version.split()[0]
    + ". Another version answers some recorded cases differently, so the comparison says "
    "nothing here: run the suite with that version")


@dataclass(frozen=True)
class Completed:
    stdout: bytes
    stderr: bytes
    code: int


def _lands_inside(path: str | Path, cwd: Path, inside: Path) -> bool:
    """Whether `path`, as a process in `cwd` names it, lies under `inside` -- both where
    the system leads through every link, and where its text leads once `..` is taken off
    it: the Rust implementation tidies a store's path so, and a link followed by `..`
    then ends somewhere else."""
    named = os.path.join(Path(cwd).resolve(), path)
    return all(Path(landing).resolve().is_relative_to(inside)
               for landing in (named, os.path.normpath(named)))


def run(impl: list[str], args: list[str], *, home: Path, root: Path, cwd: Path,
        stdin: str | bytes = "", extra_env: dict[str, str] | None = None) -> Completed:
    """`impl + args` with a constructed environment: PATH, HOME, MEMRIVER_ROOT, extra_env."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "MEMRIVER_ROOT": str(root),
           **(extra_env or {})}
    assert TMP_PATH is not None, "no test is running"
    inside = TMP_PATH.resolve()
    # the directory every relative path of the child is read from, these two included
    assert Path(cwd).resolve().is_relative_to(inside), \
        f"cwd={cwd} is outside the test's temporary directory"
    for name in ("HOME", "MEMRIVER_ROOT"):
        # empty, the Python implementation's home is `/` and its store the default one
        assert env[name], f"{name} is empty"
        assert _lands_inside(env[name], cwd, inside), \
            f"{name}={env[name]} is outside the test's temporary directory"
    # Both parsers take a shortened option, so `--ro DIR` names a store as `--root DIR`
    # does -- wherever it stands: the Python parser reads options again inside a command,
    # so a `--` before the command (or before the command of `project`) ends nothing, and
    # such a line is not started at all. The one command that writes into a directory it
    # is given is `export`: whatever follows it and is not the store is held to the same
    # rule, before its own `--` and after it. A directory another command is given
    # (`project init /`) is only looked at, and may lie anywhere.
    stores, others, takes_value, skipped, ended = [], [], False, False, False
    commands = 1    # the words still to come that name the command
    for arg in args:
        name, given, value = arg.partition("=")
        was_value, takes_value, was_skipped, skipped = takes_value, False, skipped, False
        if was_value:
            stores.append(arg)
        if len(name) > 2 and "--root".startswith(name):
            assert given or arg is not args[-1], f"{arg} without a path"
            takes_value = not given
            if given:
                stores.append(value)
            if ended:       # and, were it a directory's name, that directory
                others.append(arg)
        elif not was_value and not was_skipped:
            if arg == "--":
                assert not commands, f"`--` before the command in {args}"
                ended = True
            elif commands and not arg.startswith("-"):
                commands += (arg == "project") - 1
            elif commands and len(name) > 2 and "--project-dir".startswith(name):
                skipped = not given     # its value is no command
            others.append(arg)
    if "export" in others:
        written = others[others.index("export") + 1:]
        assert all(_lands_inside(target, cwd, inside) for target in written), \
            f"export {written} is outside the test's temporary directory"
    for store in stores:
        assert _lands_inside(store, cwd, inside), \
            f"--root {store} is outside the test's temporary directory"
    data = stdin if isinstance(stdin, bytes) else stdin.encode()
    done = subprocess.run([*impl, *args], input=data, capture_output=True,
                          env=env, cwd=cwd, check=False)
    return Completed(done.stdout, done.stderr, done.returncode)


def both(args: list[str], **where) -> tuple[Completed, Completed]:
    """The same command by both implementations, on the same home and store."""
    return run(PY, args, **where), run(RS, args, **where)


def _checked(done: Completed) -> Completed:
    assert done.code == 0, done.stderr.decode(errors="replace")
    return done


# ---------------------------------------------------------------- snapshots

@dataclass(frozen=True)
class Snapshot:
    """Every table's raw rows (column name -> value), the schema text, the version."""

    tables: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    schema: list[str] = field(default_factory=list)
    user_version: int = 0


def snapshot(root: Path) -> Snapshot:
    path = root / "memriver.db"
    if not path.exists():
        return Snapshot()
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.text_factory = lambda data: data.decode("utf-8", "surrogateescape")
    try:
        master = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY rowid").fetchall()
        tables = {row["name"]: [dict(r) for r in conn.execute(f'SELECT * FROM "{row["name"]}"')]
                  for row in master if row["type"] == "table"}
        return Snapshot(tables, [row["sql"] for row in master if row["sql"] is not None],
                        conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


# ------------------------------------------------- where generated values stand

ID = "[0-9a-hjkmnp-tv-z]{10}"
# an id where another pattern of the same line may already have put its name
SEEN = rf"(?:{ID}|<[a-z]+:[0-9]+>)"
TIMESTAMP = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z"

# every table of the store is listed in both; a new table fails until it is declared.
# An id column says which kind of object it refers to: projects and memories have
# separate id spaces, so the same text may stand for one of each.
ID_COLUMNS: dict[str, dict[str, str]] = {
    "projects": {"id": "project"},
    "memories": {"id": "memory", "project_id": "project"},
    "changes": {"change_id": "change", "undoes": "change"},
    "memory_versions": {"memory_id": "memory", "change_id": "change"},
    "memory_sources": {"memory_id": "memory", "source_id": "memory"},
    "change_steps": {"change_id": "change", "memory_id": "memory"},
    "memory_reads": {"memory_id": "memory"},
    "sessions": {"project_id": "project", "candidate_id": "project"},
    "tool_calls": {},
    "tags": {"project_id": "project"},
    "session_moves": {"from_project_id": "project", "to_project_id": "project"},
}
TIME_COLUMNS: dict[str, tuple[str, ...]] = {
    "projects": (),
    "memories": ("created", "updated", "deleted_at", "last_read_at"),
    "changes": ("at",),
    "memory_versions": (),
    "memory_sources": (),
    "change_steps": (),
    "memory_reads": ("read_at",),
    "sessions": ("started_at", "last_active_at", "ended_at", "summary_at"),
    "tool_calls": ("recorded_at",),
    "tags": ("updated",),
    "session_moves": ("moved_at",),
}
# Text columns holding JSON entries with a timestamp each (`{"at":"<time>",...}`, the
# entry's first key, as both implementations write it): only those `at` values are named,
# everything else in the cell is compared as it is.
JSON_TIME_COLUMNS: dict[str, tuple[str, ...]] = {
    "sessions": ("first_prompt", "recent_prompts"),
}
# an entry opens with `{"at":"`; inside a JSON string a brace and a quote cannot stand so
JSON_TIME = re.compile(r'\{"at":"(' + TIMESTAMP + r')"')

# What identifies a row of a table that has timestamps, so a step's change to one cell
# can be told from a row that was there before. None: the table has no key and a row is
# itself (memory_reads only ever gains rows).
ROW_KEYS: dict[str, tuple[str, ...] | None] = {
    "memories": ("id",),
    "changes": ("change_id",),
    "memory_reads": None,
    "sessions": ("harness", "session_id"),
    "tool_calls": ("harness", "call_id"),
    "tags": ("project_id", "name"),
    "session_moves": ("harness", "session_id", "seq"),
}


def _new_versions(row: dict, was: dict, then: Snapshot, now: Snapshot) -> int:
    return row["version"] - was["version"]


def _new_moves(row: dict, was: dict, then: Snapshot, now: Snapshot) -> int:
    """How many moves the step added to the session of `row`: all of them for the session's
    own row, those up to and including it for the row of one move."""
    def moves(snap: Snapshot) -> int:
        return sum(move["harness"] == row["harness"] and move["session_id"] == row["session_id"]
                   and move["seq"] <= row.get("seq", move["seq"])
                   for move in snap.tables.get("session_moves", []))
    return moves(now) - moves(then)


# Cells that only move forward: the two places the Python implementation calls
# now_strictly_after -- a memory's new version (`updated`, and `deleted_at`, which is
# set to it) and a session's move (`sessions.last_active_at` and the `moved_at` of the
# move's own row, both the same value). Each advance is max(the clock, the time before
# it plus one microsecond): never before the clock, and past it only when the stored
# time is ahead of the clock -- then by exactly one microsecond per advance.
# (table, column) -> (the table and column holding the previous time, the columns that
#                     find that row, how many advances the step made up to this cell)
MONOTONIC: dict[tuple[str, str], tuple[str, str, tuple[str, ...], Callable[..., int]]] = {
    ("memories", "updated"): ("memories", "updated", ("id",), _new_versions),
    ("memories", "deleted_at"): ("memories", "updated", ("id",), _new_versions),
    ("sessions", "last_active_at"): ("sessions", "last_active_at", ("harness", "session_id"), _new_moves),
    ("session_moves", "moved_at"): ("sessions", "last_active_at", ("harness", "session_id"), _new_moves),
}


def _instant(stamp: object) -> datetime | None:
    """The UTC time a stored timestamp names; None unless it has exactly the stored
    form and is a real calendar time."""
    if not (isinstance(stamp, str) and re.fullmatch(TIMESTAMP, stamp)):
        return None
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _stamp(instant: datetime) -> str:
    return instant.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class Output:
    """Where generated values stand in one kind of output. Each pattern has one group,
    around the value, and is applied per line; an id pattern names the kind of object.
    Everything after the first `body_after` is free text and is left as it is."""

    ids: tuple[tuple[str, str], ...] = ()
    times: tuple[str, ...] = ()
    body_after: str | None = None
    # for an id that a pattern cannot find because free text stands on its line:
    # (the text, the side's store) -> [(start, end, kind)], from what the store holds
    locate: Callable[[str, Snapshot], list[tuple[int, int, str]]] | None = None
    # the output is one JSON object on one line, written as Python's
    # `json.dumps(ensure_ascii=False)` writes it, and the values are in the string at
    # this path of keys: the patterns and `locate` apply to that string only
    json_text: tuple[str, ...] | None = None


def _json(key: str, value: str) -> str:
    return rf'"{key}":\s*"({value})"'


def _created_project(text: str, store: Snapshot) -> list[tuple[int, int, str]]:
    """Where `project init` printed the new project's id. Its line is
    `created project <name> [<id>] and bound <root>`, and both the name and the root are
    the user's text, which may itself look like `[<id>] and bound`. So the line is not
    parsed: for each project the store holds, the exact line the command prints for it
    is built and looked for, and the id is taken from its known offset."""
    from memriver.project_context import visible  # how the command shows name and root

    found = []
    for row in store.tables.get("projects", []):
        if row["root"] is None:
            continue
        before = f"created project {visible(row['name'])} ["
        line = f"{before}{row['id']}] and bound {visible(row['root'])}\n"
        at = text.find(line)
        while at != -1:
            if at == 0 or text[at - 1] == "\n":
                found.append((at + len(before), at + len(before) + len(row["id"]), "project"))
            at = text.find(line, at + 1)
    return found


def _hook_projects(text: str, store: Snapshot) -> list[tuple[int, int, str]]:
    """Where a hook's text names a project: `project: <name> [<id>]` (the header) and
    `project <name> [<id>]` (a notice), the name shown and cut as a header field. Free
    text stands before and after both, so for each project the store holds the exact
    phrase is built and looked for, and the id taken from its known offset."""
    from memriver.project_context import visible  # how the hook shows a name

    found = []
    for row in store.tables.get("projects", []):
        for lead in ("project: ", "project "):
            before = f"{lead}{visible(row['name'])[:120]} ["
            phrase = f"{before}{row['id']}]"
            at = text.find(phrase)
            while at != -1:
                found.append((at + len(before), at + len(before) + len(row["id"]), "project"))
                at = text.find(phrase, at + 1)
    return found


def _history_ids(text: str, store: Snapshot) -> list[tuple[int, int, str]]:
    """Where `history` printed ids. A version's line is
    `v<n>  <at>  <changed_by>[ (<via>)]  change <id>...`, with the caller's own text before
    the id, so the start of the line is built from the change rows the store holds and
    the id taken at its known offset. A `sources:` line is ids and versions only."""
    from memriver.project_context import visible  # how the command shows stored text

    found = []
    at = 0
    for line in text.splitlines(keepends=True):
        cited = re.fullmatch(rf"(?:    )?sources: {ID} v[0-9]+(?:, {ID} v[0-9]+)*\n?", line)
        if cited:
            found += [(at + m.start(1), at + m.end(1), "memory")
                      for m in re.finditer(rf"(?:: |, )({ID}) v[0-9]+", line)]
        for row in store.tables.get("changes", []):
            via = f" ({visible(row['changed_via'])})" if row["changed_via"] else ""
            head = f"  {visible(row['at'])}  {visible(row['changed_by'])}{via}  change "
            start = re.match(r"v[0-9]+", line)
            if start and line[start.end():].startswith(head + row["change_id"]):
                begin = at + start.end() + len(head)
                found.append((begin, begin + len(row["change_id"]), "change"))
                break
        at += len(line)
    return found


# the marks an index line of a memory opens with (a tag's line names no memory)
_MARKS = "(?:user|feedback|project|reference|rule)(?:, global(?:, rule)?)?"

# The caller says which kind of output it compares; nothing is renamed in "plain".
# A pattern is used only where the value's place on its line is fixed -- first on the
# line, or last (`history-version`), with any free text on the other side of it -- and
# these commands print free text with line breaks replaced, so it cannot start a line.
OUTPUTS: dict[str, Output] = {
    "plain": Output(),
    "project-init": Output(ids=((rf"^refused: .* is already bound to project ({ID})$", "project"),),
                           locate=_created_project),
    "project-explain": Output(ids=((rf"^(?:project|reads|writes): ({ID})(?=,|$)", "project"),)),
    "project-unbind": Output(ids=(
        (rf"^  the current directory resolves afterwards: registered ({ID})$", "project"),)),
    "list": Output(ids=((rf"^({ID})  ", "project"), (rf"^  ({ID})  \[", "memory"))),
    "show": Output(
        ids=((rf"^id: ({ID})$", "memory"), (rf"^project: ({ID})$", "project")),
        times=tuple(rf"^{name}: ({TIMESTAMP})$"
                    for name in ("created", "updated", "last_read_at", "deleted")),
        body_after="\n---\n"),
    "history-version": Output(
        ids=((rf"^id: ({ID})$", "memory"), (rf"^change: .*  change ({ID})$", "change")),
        times=(rf"^change: ({TIMESTAMP})  ",),
        body_after="\n---\n", locate=_history_ids),
    "history": Output(times=(rf"^v[0-9]+  ({TIMESTAMP})  ",), locate=_history_ids),
    "search": Output(ids=((rf"^  ({ID})  \[", "memory"),)),
    "sessions": Output(
        # a move's first project stands between two directories: it is not renamed
        ids=((rf"^  project: .* \(({ID})\)$", "project"),
             (rf"^  moved: .*\(({ID})\)\)$", "project")),
        times=(rf"^  last active: ({TIMESTAMP}) \(", rf"^  moved: ({TIMESTAMP}) from ",
               rf"^  first recorded: ({TIMESTAMP})$", rf"^  last end event: ({TIMESTAMP})$")),
    "sessions-json": Output(
        ids=((rf'^ +"(?:project|from_project_id|to_project_id)": "({ID})",?$', "project"),),
        times=((rf'^ +"(?:first_recorded|last_active_at|last_end_event_at|summary_at|at|moved_at)"'
               rf': "({TIMESTAMP})",?$'),)),
    "undo": Output(
        ids=((rf"^memriver undo: change ({ID}) at ", "change"),
             (rf"^memriver undo: change .*, an undo of ({ID})$", "change"),
             (rf"^  ({ID})  (?:global|project {SEEN})  ", "memory"),
             (rf"^  {SEEN}  project ({ID})  ", "project"),
             (rf"^  ({ID}): (?:soft delete|restore the state of v[0-9]+)$", "memory"),
             (rf"^undone change ({ID}) by change {SEEN}$", "change"),
             (rf"^undone change {SEEN} by change ({ID})$", "change"),
             (rf"^refused \(hard-deleted\): a hard delete removed part of change ({ID}); ", "change"),
             (rf"^refused \(changed\): ({ID}) changed after change {SEEN}; ", "memory"),
             (rf"^refused \(changed\): {SEEN} changed after change ({ID}); ", "change")),
        times=(rf"^memriver undo: change {SEEN} at ({TIMESTAMP}) by ",)),
    "restore": Output(
        ids=((rf"^memriver restore: ({ID}) from v[0-9]+ to the state of v[0-9]+$", "memory"),
             (rf"^restored ({ID}) to the state of v[0-9]+ as v[0-9]+ \(change {SEEN}\)$", "memory"),
             (rf"^restored {SEEN} to the state of v[0-9]+ as v[0-9]+ \(change ({ID})\)$", "change"),
             (rf"^no such version: ({ID}) v[0-9]+$", "memory"),
             (rf"^refused \(version\): ({ID}) changed while waiting; ", "memory"))),
    "delete": Output(
        ids=((rf"^memriver delete: ({ID}) \[[a-z]+\] in ", "memory"),
             (rf"^memriver delete: {SEEN} \[[a-z]+\] in project ({ID}): ", "project"),
             (rf"^deleted ({ID})$", "memory"),
             (rf"^refused: ({ID}) belongs to project {SEEN}, not this ", "memory"),
             (rf"^refused: {SEEN} belongs to project ({ID}), not this ", "project"),
             (rf"^refused: ({ID}) changed since version [0-9]+; run memriver show {SEEN} and retry$",
              "memory"),
             (rf"^refused: {SEEN} changed since version [0-9]+; run memriver show ({ID}) and retry$",
              "memory"),
             (rf"^  ({ID})  (?:global|project {SEEN})  v[0-9]+", "memory"),
             (rf"^  {SEEN}  project ({ID})  v[0-9]+", "project"),
             (rf"^    because ({ID}) v[0-9]+(?: \(history\))? cites {SEEN} v[0-9]+$", "memory"),
             (rf"^    because {SEEN} v[0-9]+(?: \(history\))? cites ({ID}) v[0-9]+$", "memory"),
             (rf"^to delete exactly these, run: memriver delete ({ID}) --hard --confirm ", "memory"),
             (rf"^purged ({ID})$", "memory"))),
    # the directory written to is the caller's and is shown as given: nothing is generated
    "export": Output(),
    # what a SessionStart or UserPromptSubmit hook hands the agent: an index line starts
    # with its marks and the memory's id; a project is named by `_hook_projects`
    "hook": Output(ids=((rf"^- \[{_MARKS}\] ({ID}): ", "memory"),),
                   locate=_hook_projects,
                   json_text=("hookSpecificOutput", "additionalContext")),
    # what the cases of test_dump_store.py print for a change they applied
    "change-json": Output(ids=((_json("id", ID), "memory"), (_json("project_id", ID), "project"),
                               (_json("change_id", ID), "change"))),
}


def _rename(pattern: str, text: str, name: Callable[[str], str]) -> str:
    def one(match: re.Match[str]) -> str:
        start, end = match.start(1) - match.start(), match.end(1) - match.start()
        return match[0][:start] + name(match[1]) + match[0][end:]
    return re.sub(pattern, one, text, flags=re.MULTILINE)


# ------------------------------------------------------- pairing new objects

Names = dict[tuple[str, str], str]     # (kind of object, the id as generated) -> <kind:n>


class NotComparable(AssertionError):
    """A step's new rows cannot be paired between the two sides."""


def _project_key(row: dict, names: Names, after: Snapshot) -> tuple:
    return (row["is_global"], row["name"], row["root"])


def _memory_key(row: dict, names: Names, after: Snapshot) -> tuple:
    key = (names.get(("project", row["project_id"]), row["project_id"]), row["type"], row["description"],
           row["body"])
    batch = {change["change_id"] for change in after.tables["changes"] if change["step_count"] > 1}
    for step in after.tables["change_steps"]:
        if step["memory_id"] == row["id"] and step["op"] == "create" and step["change_id"] in batch:
            return (*key, step["step"])     # inside one batch: its position
    return key


def _change_key(row: dict, names: Names, after: Snapshot) -> tuple:
    steps = sorted((step["step"], step["op"],
                    names.get(("memory", step["memory_id"]), step["memory_id"]),
                    step["before_version"], step["after_version"])
                   for step in after.tables["change_steps"] if step["change_id"] == row["change_id"])
    return (*steps, row["changed_by"], row["changed_via"], row["step_count"],
            names.get(("change", row["undoes"]), row["undoes"]))


# a table that mints ids -> the key of one of its new rows, free of generated values.
# In dependency order: a memory's key names its project, a change's key its memories.
LOGICAL_KEYS: dict[str, Callable[[dict, Names, Snapshot], tuple]] = {
    "projects": _project_key,
    "memories": _memory_key,
    "changes": _change_key,
}
_MINTED = {"projects": ("id", "project"), "memories": ("id", "memory"),
           "changes": ("change_id", "change")}


def pair_step(table: str, py_new: list[dict], rs_new: list[dict], py_names: Names,
              rs_names: Names, py_after: Snapshot, rs_after: Snapshot) -> list[tuple[str, str]]:
    """(Python id, Rust id) for the new rows of `table` with equal logical keys, in key
    order. Rows whose key the other side lacks stay unpaired, and so different.
    NotComparable when the counts differ or one side has a key twice: never an order."""
    column = _MINTED[table][0]
    if len(py_new) != len(rs_new):
        raise NotComparable(f"{table}: {len(py_new)} new on the Python side, {len(rs_new)} on the Rust side")
    keyed = []
    for rows, names, after in ((py_new, py_names, py_after), (rs_new, rs_names, rs_after)):
        keys = Counter(repr(LOGICAL_KEYS[table](row, names, after)) for row in rows)
        twice = [key for key, count in keys.items() if count > 1]
        if twice:
            raise NotComparable(f"{table}: {keys[twice[0]]} new rows with the same logical key {twice[0]}")
        keyed.append({repr(LOGICAL_KEYS[table](row, names, after)): row[column] for row in rows})
    return [(keyed[0][key], keyed[1][key]) for key in sorted(keyed[0]) if key in keyed[1]]


# ------------------------------------------------------------------ a case

def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(eq=False)
class Side:
    case: Case
    impl: list[str]
    root: Path
    names: Names = field(default_factory=dict)             # (kind, a new id) -> <kind:n>
    times: dict[str, str] = field(default_factory=dict)    # a timestamp -> <t:n>

    def run(self, args: list[str], *, cwd: Path | None = None, stdin: str | bytes = "",
            extra_env: dict[str, str] | None = None) -> Completed:
        return run(self.impl, args, home=self.case.home, root=self.root,
                   cwd=cwd or self.case.work, stdin=stdin, extra_env=extra_env)

    def api(self, code: str) -> Completed:
        """Python code over this side's store (see PY_API); it must succeed."""
        return _checked(run(PY_API, [code], home=self.case.home, root=self.root, cwd=self.case.work))

    def raw(self, name: str) -> str:
        """The id this side generated for `<kind:n>`."""
        return next(raw for (_, raw), given in self.names.items() if given == name)


class Case:
    """Steps on two copies of one store: `py` by the Python implementation, `rs` by `rs`.

    The working directories and the home are shared, so paths are equal on both
    sides; each side has its own store, shown as `<store>` in compared output.
    """

    def __init__(self, tmp_path: Path, *, seed: Path | None = None, rs: list[str] = RS) -> None:
        self.home, self.work = tmp_path / "home", tmp_path / "work"
        for directory in (self.home, self.work):
            directory.mkdir(parents=True, exist_ok=True)
        self.py = Side(self, PY, tmp_path / "store-py")
        self.rs = Side(self, rs, tmp_path / "store-rs")
        self._count: Counter[str] = Counter()
        for side in self._sides:
            if seed is not None:
                shutil.copytree(seed, side.root)
            self._see_times(side, Snapshot(), snapshot(side.root), None)

    @property
    def _sides(self) -> tuple[Side, Side]:
        return (self.py, self.rs)

    def step(self, action: Callable[[Side], Completed | None]) -> tuple:
        """Runs `action` on each side, pairs the objects it created and names them."""
        before = [snapshot(side.root) for side in self._sides]
        results, spans = [], []
        for side in self._sides:
            start = _now()
            results.append(action(side))
            spans.append((start, _now()))
        after = [snapshot(side.root) for side in self._sides]
        for table, (column, kind) in _MINTED.items():
            new = [[row for row in now.tables.get(table, [])
                    if row[column] not in {old[column] for old in then.tables.get(table, [])}]
                   for then, now in zip(before, after)]
            for row in (*new[0], *new[1]):
                assert isinstance(row[column], str) and re.fullmatch(ID, row[column]), \
                    f"{table}: not an id: {row[column]!r}"
            for py_id, rs_id in pair_step(table, *new, self.py.names, self.rs.names, *after):
                self._count[kind] += 1
                name = f"<{kind}:{self._count[kind]}>"
                self.py.names[kind, py_id] = self.rs.names[kind, rs_id] = name
        for side, then, now, span in zip(self._sides, before, after, spans):
            self._see_times(side, then, now, span)
        return tuple(results)

    def _see_times(self, side: Side, then: Snapshot, now: Snapshot,
                   span: tuple[datetime, datetime] | None) -> None:
        """Checks every timestamp cell the step wrote, then names the timestamps not seen
        before, ascending, so their order is compared.

        A cell was written when its row is new or its value differs from the row's value
        before the step; whether the value itself was seen before does not matter. What it
        holds must be a real time in the stored form, and:
        - never earlier than the first reading of `span`, the clock read before and after
          this side's action;
        - in a cell that only moves forward (MONOTONIC), with a previous time: later than
          that time by at least one microsecond for each advance the step made, and
          later than the second reading only as exactly that -- which is what is written
          when the previous time is ahead of the clock;
        - anywhere else: not later than the second reading.
        Without a span (a seed as loaded) only the form is checked."""
        for table, rows in now.tables.items():
            key = ROW_KEYS[table] if TIME_COLUMNS[table] else None
            if key is None:     # a row is itself: count the ones that were there
                there = Counter(repr(row) for row in then.tables.get(table, []))
                earlier = {}
            else:
                earlier = {tuple(row[k] for k in key): row for row in then.tables.get(table, [])}
            for row in rows:
                if key is None:
                    there[repr(row)] -= 1
                    was = row if there[repr(row)] >= 0 else None
                else:
                    was = earlier.get(tuple(row[k] for k in key))
                for name in TIME_COLUMNS[table]:
                    stamp = row[name]
                    if stamp is None:
                        continue
                    instant = _instant(stamp)
                    assert instant is not None, f"not a timestamp: {stamp!r}"
                    if span is None:
                        continue
                    base, advances = self._previous(table, name, row, was, then, now)
                    if base is None and was is not None and was[name] == stamp:
                        continue    # not written by this step
                    latest = span[1]
                    if base is not None:
                        least = base + timedelta(microseconds=max(advances, 1))
                        assert instant >= least, (f"{table}.{name}: {stamp} is not after {_stamp(base)} "
                                                  f"by its {advances} advance(s)")
                        if advances:    # past the clock only as far as the advances carry it
                            latest = max(latest, least)
                    assert span[0] <= instant <= latest, (
                        f"{table}.{name}: {stamp} is outside the step, which ran from "
                        f"{_stamp(span[0])} to {_stamp(span[1])}")
                for name in JSON_TIME_COLUMNS.get(table, ()):
                    there_before = set(JSON_TIME.findall(was[name] or "")) if was else set()
                    for stamp in JSON_TIME.findall(row[name] or ""):
                        instant = _instant(stamp)
                        assert instant is not None, f"{table}.{name}: not a timestamp: {stamp!r}"
                        # an entry the step added was taken while it ran
                        if span is not None and stamp not in there_before:
                            assert span[0] <= instant <= span[1], (
                                f"{table}.{name}: {stamp} is outside the step, which ran from "
                                f"{_stamp(span[0])} to {_stamp(span[1])}")
        seen = {row[name] for table, rows in now.tables.items()
                for row in rows for name in TIME_COLUMNS[table]}
        seen |= {stamp for table, rows in now.tables.items() for row in rows
                 for name in JSON_TIME_COLUMNS.get(table, ())
                 for stamp in JSON_TIME.findall(row[name] or "")}
        for stamp in sorted(seen - {None} - side.times.keys()):
            side.times[stamp] = f"<t:{len(side.times) + 1}>"

    @staticmethod
    def _previous(table: str, name: str, row: dict, was: dict | None, then: Snapshot,
                  now: Snapshot) -> tuple[datetime | None, int]:
        """For a cell that only moves forward and that this step advanced: the time it
        advanced from and how many advances the step made. (None, 0) for any other cell."""
        if (table, name) not in MONOTONIC:
            return None, 0
        base_table, base_column, key, count = MONOTONIC[table, name]
        holder = next((old for old in then.tables.get(base_table, [])
                       if all(old[k] == row[k] for k in key)), None)
        if holder is None:      # the row it advances from is new too: the clock applies
            return None, 0
        advances = count(row, holder, then, now)
        unchanged = was is not None and was[name] == row[name]
        if unchanged and advances == 0:
            return None, 0
        return _instant(holder[base_column]), advances

    def renamed(self, side: Side) -> dict:
        """The side's store with ids and timestamps renamed in their declared columns only."""
        now = snapshot(side.root)

        def cell(table: str, name: str, value: object) -> object:
            if name in ID_COLUMNS[table]:
                return side.names.get((ID_COLUMNS[table][name], value), value)
            if name in TIME_COLUMNS[table]:
                return side.times.get(value, value)
            if name in JSON_TIME_COLUMNS.get(table, ()) and isinstance(value, str):
                return JSON_TIME.sub(
                    lambda found: '{"at":"' + side.times.get(found[1], found[1]) + '"', value)
            return value
        return {"user_version": now.user_version, "schema": now.schema,
                "tables": {table: sorted((tuple(cell(table, name, value) for name, value in row.items())
                                          for row in rows), key=repr)
                           for table, rows in now.tables.items()}}

    def assert_same_store(self) -> None:
        py, rs = self.renamed(self.py), self.renamed(self.rs)
        if py == rs:
            return
        lines = [f"{name} differs: python={py[name]!r} rust={rs[name]!r}"
                 for name in ("user_version", "schema") if py[name] != rs[name]]
        for table in sorted({*py["tables"], *rs["tables"]}):
            left, right = py["tables"].get(table, []), rs["tables"].get(table, [])
            if left != right:
                lines += [f"{table} differs:",
                          *(f"  only python: {row!r}" for row in left if row not in right),
                          *(f"  only rust:   {row!r}" for row in right if row not in left)]
        raise AssertionError("the stores differ\n" + "\n".join(lines))

    def rendered(self, side: Side, done: Completed, kind: str = "plain",
                 allow: tuple[str, ...] = ()) -> tuple[str, str, int]:
        """(stdout, stderr, code) as compared. In stdout, up to the free text of an
        output of this `kind`: the side's store path as `<store>`, and the ids and
        timestamps in the fields the kind declares by their names. In stderr only the
        store path. Then the parts covered by an allowed difference are masked."""
        output = OUTPUTS[kind]

        def masked(shown: str) -> str:
            for name in allow:
                assert name in DIFFERENCES
                shown = MASKS[name].sub(f"<{name}>", shown)
            return shown

        def text(data: bytes) -> str:
            return data.decode("utf-8", "surrogateescape")

        def named(head: str) -> str:
            if output.locate is not None:   # right to left, so the places found stay valid
                for start, end, of in sorted(output.locate(head, snapshot(side.root)), reverse=True):
                    head = head[:start] + side.names.get((of, head[start:end]), head[start:end]) + head[end:]
            head = head.replace(str(side.root), "<store>")
            for pattern, of in output.ids:
                head = _rename(pattern, head, lambda raw, of=of: side.names.get((of, raw), raw))
            for pattern in output.times:
                head = _rename(pattern, head, lambda raw: side.times.get(raw, raw))
            return head

        head, separator, body = text(done.stdout), "", ""
        if output.body_after is not None:
            head, separator, body = head.partition(output.body_after)
        if output.json_text is None:
            head = named(head)
        else:
            head = _named_inside_json(head, output.json_text, named)
        return (masked(head + separator + body),
                masked(text(done.stderr).replace(str(side.root), "<store>")), done.code)

    def assert_same_output(self, py: Completed, rs: Completed, kind: str = "plain",
                           allow: tuple[str, ...] = ()) -> None:
        for name in allow:
            DIFFERENCES[name]    # KeyError: not a listed difference
        left, right = self.rendered(self.py, py, kind, allow), self.rendered(self.rs, rs, kind, allow)
        assert left == right, f"the outputs differ\npython: {left!r}\nrust:   {right!r}"


def _named_inside_json(head: str, path: tuple[str, ...], named: Callable[[str], str]) -> str:
    """`head` with `named` applied to the string at `path` of the one JSON object it holds.
    Only output in exactly the form Python's `json.dumps(ensure_ascii=False)` writes, with
    a line break after it, is opened and written back so; anything else is left as it is
    and so compared byte for byte."""
    try:
        value = json.loads(head)
    except ValueError:
        return head
    if json.dumps(value, ensure_ascii=False) + "\n" != head:
        return head
    holder = value
    for key in path[:-1]:
        holder = holder.get(key) if isinstance(holder, dict) else None
    if not (isinstance(holder, dict) and isinstance(holder.get(path[-1]), str)):
        return head
    holder[path[-1]] = named(holder[path[-1]])
    return json.dumps(value, ensure_ascii=False) + "\n"


# -------------------------------------------------------------------- seeds

_SEED = """
for directory in (WORK / "proj" / "sub", WORK / "other", WORK / "repo" / "deep" / "er",
                  WORK / "repo-linked" / "deep"):
    directory.mkdir(parents=True, exist_ok=True)
global_id = services.project.ensure_global()
services.project.init_project("repo with history", services.project.plan_root(str(WORK / "repo")))
project = services.project.init_project("demo proj", services.project.plan_root(str(WORK / "proj")))
services.memory.apply([
    Create(global_id, "feedback", "always answer in the user's language",
           "Reply in the language the user writes in.", level=1),
    Create(global_id, "feedback", "testing: temp dirs only",
           "Tests use temporary directories, never real data.", level=2, tags=("testing",)),
    Create(global_id, "reference", "testing: port check",
           "Check a fixed port is free before binding.", level=2, tags=("testing", "network")),
    Create(global_id, "user", "who the user is", "The user maintains memriver."),
    Create(global_id, "feedback", "", "No description here.\\nSecond line of the body."),
], changed_by="seed")
services.memory.describe_tag(global_id, "testing", "how tests and acceptance runs must behave",
                             changed_by="seed")
context = services.project.open_project_context(str(WORK / "proj"))
bodies = [
    ("deploy script location", "The deploy script lives in tools/deploy.sh"),
    ("", "A body with no description at all, long enough to be cut in the index cue and the "
         "search snippet for sure."),
    ("日本語のメモ", "リリース手順は docs/release.md にある。ＡＢＣ　全角スペース。"),
    ("中文备忘", "部署脚本在 tools/deploy.sh，发布前先跑 e2e。"),
    ("Straße and ﬁne ligatures", "STRASSE is how the fold spells it; ﬁle and file match."),
    ("emoji 🚀 cue", "Ship it 🚀 once CI is green."),
] + [(f"note {n} about release train", f"Entry number {n}: the release train leaves on day {n}.")
     for n in range(24)]
written = [services.memory.record(content=body, type="project", sync=True, harness="cursor",
                                  description=description, context=context)
           for description, body in bodies]
services.memory.apply([Create(project.id, "project", "derived from the deploy note",
                              "Deploys are scripted.", sources=(SourceRef(written[0].id, 1),))],
                      changed_by="seed")
"""


def _git(args: list[str], *, home: Path, root: Path, cwd: Path) -> None:
    _checked(run(GIT, args, home=home, root=root, cwd=cwd))


def _repository(path: Path, branch: str, worktrees: list[dict], **where) -> None:
    """A repository with one commit and its linked worktrees (`branch`: None = detached)."""
    _git(["init", "-q", "-b", branch, str(path)], **where)
    _git(["-C", str(path), "commit", "-q", "--allow-empty", "-m", "one"], **where)
    for tree in worktrees:
        how = ["-b", tree["branch"]] if tree.get("branch") else ["--detach"]
        _git(["-C", str(path), "worktree", "add", "-q", *how, tree["path"]], **where)


def seed_store(tmp_path: Path) -> Path:
    """A store written by the Python implementation, for `Case(tmp_path, seed=...)`: two
    bound projects (one a git repository with two linked worktrees), global with rules
    and tags, and a memory derived from another."""
    home, work, root = tmp_path / "home", tmp_path / "work", tmp_path / "seed"
    for directory in (home, work):
        directory.mkdir(parents=True)
    where = {"home": home, "root": root, "cwd": work}
    _repository(work / "repo", "trunk", [
        {"path": str(work / "repo-linked"), "branch": "feature/x"},
        {"path": str(work / "repo-detached")}], **where)
    _checked(run(PY_API, [_SEED], **where))
    return root


def load_seed(name: str, tmp_path: Path, *, seeds: Path = SEEDS) -> Path:
    """Builds the committed seed `<seeds>/<name>/` under `tmp_path` (`home`, `work`,
    `store`) and checks the result before any case may use it. Returns the store.

    `tmp_path` must be empty or absent: everything in it is then made by the loader, so
    git finds no repository, link or setting of someone else's to write through."""
    from memriver_core.repository.sqlite.database import _SCHEMA

    # everything is placed from the case's own root, with links followed -- never from
    # where the caller happens to stand
    base = tmp_path.resolve()
    assert TMP_PATH is not None and base.is_relative_to(TMP_PATH.resolve()), \
        f"seed {name}: {tmp_path} is outside the test's temporary directory"
    home, work, root = base / "home", base / "work", base / "store"

    def text(file: str) -> str:
        return ((seeds / name / file).read_text(encoding="utf-8")
                .replace("{{WORK}}", str(work)).replace("{{HOME}}", str(home)))

    def place(written: str) -> Path:
        """Where a path of the layout lies; refused unless that is under the case's root."""
        path = (base / written).resolve()
        assert path.is_relative_to(base), f"seed {name}: {written} is outside {tmp_path}"
        return path
    layout = json.loads(text("layout.json"))
    # before anything is created; git is then given only these checked absolute paths
    directories = [place(directory) for directory in layout["directories"]]
    repositories = [(place(repository["path"]), repository["branch"],
                     [{**tree, "path": str(place(tree["path"]))} for tree in repository["worktrees"]])
                    for repository in layout["git"]]
    assert not base.exists() or (base.is_dir() and not any(base.iterdir())), \
        f"seed {name}: {tmp_path} is not empty"
    for directory in (home, work):
        directory.mkdir(parents=True)
    for path, branch, worktrees in repositories:
        _repository(path, branch, worktrees, home=home, root=root, cwd=work)
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    root.mkdir(mode=0o700)
    database = root / "memriver.db"
    database.touch(mode=0o600)
    conn = sqlite3.connect(database)
    try:
        conn.executescript(text("schema.sql") + text("data.sql"))
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 6, f"seed {name}: user_version is {version}, not 6"
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)], f"seed {name}"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == [], f"seed {name}"
    finally:
        conn.close()
    assert snapshot(root).schema == list(_SCHEMA), f"seed {name}: not the schema of the Python version"
    return root
