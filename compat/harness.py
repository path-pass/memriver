"""Drives the Python and the Rust memriver as processes and compares what they leave.

Isolation: `run` is the only place a process is started. It builds the child's
environment from scratch and refuses a home or a store outside the test's
temporary directory (`TMP_PATH`, set per test by conftest.py).

Comparison: a `Case` runs each step on two copies of a store, one per
implementation, and snapshots both before and after. Ids and timestamps are
generated, so they differ between the sides; they are renamed -- only in the
columns and output fields declared below -- to names that say which object is
meant (`<memory:2>`, `<t:5>`). Everything else, free text included, is compared
as it is.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class Completed:
    stdout: bytes
    stderr: bytes
    code: int


def run(impl: list[str], args: list[str], *, home: Path, root: Path, cwd: Path,
        stdin: str = "", extra_env: dict[str, str] | None = None) -> Completed:
    """`impl + args` with a constructed environment: PATH, HOME, MEMRIVER_ROOT, extra_env."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "MEMRIVER_ROOT": str(root),
           **(extra_env or {})}
    assert TMP_PATH is not None, "no test is running"
    for name in ("HOME", "MEMRIVER_ROOT"):
        assert Path(env[name]).resolve().is_relative_to(TMP_PATH.resolve()), \
            f"{name}={env[name]} is outside the test's temporary directory"
    done = subprocess.run([*impl, *args], input=stdin.encode(), capture_output=True,
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
TIMESTAMP = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z"

# every table of the store is listed in both; a new table fails until it is declared
ID_COLUMNS: dict[str, tuple[str, ...]] = {
    "projects": ("id",),
    "memories": ("id", "project_id"),
    "changes": ("change_id", "undoes"),
    "memory_versions": ("memory_id", "change_id"),
    "memory_sources": ("memory_id", "source_id"),
    "change_steps": ("change_id", "memory_id"),
    "memory_reads": ("memory_id",),
    "sessions": ("project_id", "candidate_id"),
    "tool_calls": (),
    "tags": ("project_id",),
    "session_moves": ("from_project_id", "to_project_id"),
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
# in output: the JSON keys whose string value is an id (a timestamp), and the fixed
# lines with an id in them, one group around the id
OUTPUT_ID_FIELDS = {
    "json_keys": ("id", "project_id", "change_id", "candidate_id"),
    "line_patterns": (
        rf"^created project .* \[({ID})\] and bound ",
        rf"^(?:project|reads|writes): ({ID})(?=,|$)",   # project explain
        rf"^({ID})  ",                      # list: a project line
        rf"^  ({ID})  \[",                  # list: a memory line
    ),
}
OUTPUT_TIME_FIELDS = {
    "json_keys": ("at", "created", "updated", "deleted_at", "last_read_at"),
    "line_patterns": (),
}


def _patterns(fields: dict, value: str) -> list[re.Pattern[str]]:
    keys = "|".join(map(re.escape, fields["json_keys"]))
    return [re.compile(rf'"(?:{keys})":\s*"({value})"'),
            *(re.compile(pattern, re.MULTILINE) for pattern in fields["line_patterns"])]


_ID_PATTERNS = _patterns(OUTPUT_ID_FIELDS, ID)
_TIME_PATTERNS = _patterns(OUTPUT_TIME_FIELDS, TIMESTAMP)


def _declared(patterns: list[re.Pattern[str]], text: str) -> list[str]:
    return [match[1] for pattern in patterns for match in pattern.finditer(text)]


def _rename(patterns: list[re.Pattern[str]], text: str, names: dict[str, str]) -> str:
    def one(match: re.Match[str]) -> str:
        start, end = match.start(1) - match.start(), match.end(1) - match.start()
        return match[0][:start] + names.get(match[1], match[1]) + match[0][end:]
    for pattern in patterns:
        text = pattern.sub(one, text)
    return text


# ------------------------------------------------------- pairing new objects

class NotComparable(AssertionError):
    """A step's new rows cannot be paired between the two sides."""


def _project_key(row: dict, names: dict[str, str], after: Snapshot) -> tuple:
    return (row["is_global"], row["name"], row["root"])


def _memory_key(row: dict, names: dict[str, str], after: Snapshot) -> tuple:
    key = (names.get(row["project_id"], row["project_id"]), row["type"], row["description"],
           row["body"])
    batch = {change["change_id"] for change in after.tables["changes"] if change["step_count"] > 1}
    for step in after.tables["change_steps"]:
        if step["memory_id"] == row["id"] and step["op"] == "create" and step["change_id"] in batch:
            return (*key, step["step"])     # inside one batch: its position
    return key


def _change_key(row: dict, names: dict[str, str], after: Snapshot) -> tuple:
    steps = sorted((step["step"], step["op"], names.get(step["memory_id"], step["memory_id"]),
                    step["before_version"], step["after_version"])
                   for step in after.tables["change_steps"] if step["change_id"] == row["change_id"])
    return (*steps, row["changed_by"], row["changed_via"], row["step_count"],
            names.get(row["undoes"], row["undoes"]))


# a table that mints ids -> the key of one of its new rows, free of generated values.
# In dependency order: a memory's key names its project, a change's key its memories.
LOGICAL_KEYS: dict[str, Callable[[dict, dict[str, str], Snapshot], tuple]] = {
    "projects": _project_key,
    "memories": _memory_key,
    "changes": _change_key,
}
_MINTED = {"projects": ("id", "project"), "memories": ("id", "memory"),
           "changes": ("change_id", "change")}


def pair_step(table: str, py_new: list[dict], rs_new: list[dict], py_names: dict[str, str],
              rs_names: dict[str, str], py_after: Snapshot, rs_after: Snapshot) -> list[tuple[str, str]]:
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

@dataclass(eq=False)
class Side:
    case: Case
    impl: list[str]
    root: Path
    names: dict[str, str] = field(default_factory=dict)    # a new id -> <kind:n>
    times: dict[str, str] = field(default_factory=dict)    # a timestamp -> <t:n>

    def run(self, args: list[str], *, cwd: Path | None = None, stdin: str = "") -> Completed:
        return run(self.impl, args, home=self.case.home, root=self.root,
                   cwd=cwd or self.case.work, stdin=stdin)

    def api(self, code: str) -> Completed:
        """Python code over this side's store (see PY_API); it must succeed."""
        return _checked(run(PY_API, [code], home=self.case.home, root=self.root, cwd=self.case.work))

    def raw(self, name: str) -> str:
        """The id this side generated for `<kind:n>`."""
        return next(raw for raw, given in self.names.items() if given == name)


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
            self._see_times(side, snapshot(side.root), None)

    @property
    def _sides(self) -> tuple[Side, Side]:
        return (self.py, self.rs)

    def step(self, action: Callable[[Side], Completed | None]) -> tuple:
        """Runs `action` on each side, pairs the objects it created and names them."""
        before = [snapshot(side.root) for side in self._sides]
        results = tuple(action(side) for side in self._sides)
        after = [snapshot(side.root) for side in self._sides]
        for table, (column, kind) in _MINTED.items():
            new = [[row for row in now.tables.get(table, [])
                    if row[column] not in {old[column] for old in then.tables.get(table, [])}]
                   for then, now in zip(before, after)]
            for py_id, rs_id in pair_step(table, *new, self.py.names, self.rs.names, *after):
                self._count[kind] += 1
                self.py.names[py_id] = self.rs.names[rs_id] = f"<{kind}:{self._count[kind]}>"
        for side, now, result in zip(self._sides, after, results):
            self._see_times(side, now, result)
        return results

    def _see_times(self, side: Side, now: Snapshot, result: object) -> None:
        """Names the timestamps not seen before, ascending, so their order is compared."""
        seen = {row[name] for table, rows in now.tables.items()
                for row in rows for name in TIME_COLUMNS[table]}
        if isinstance(result, Completed):
            seen.update(_declared(_TIME_PATTERNS, result.stdout.decode(errors="replace")))
        for stamp in sorted(s for s in seen if isinstance(s, str) and s not in side.times):
            side.times[stamp] = f"<t:{len(side.times) + 1}>"

    def renamed(self, side: Side) -> dict:
        """The side's store with ids and timestamps renamed in their declared columns only."""
        now = snapshot(side.root)

        def cell(table: str, name: str, value: object) -> object:
            if name in ID_COLUMNS[table]:
                return side.names.get(value, value)
            if name in TIME_COLUMNS[table]:
                return side.times.get(value, value)
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

    def rendered(self, side: Side, done: Completed, allow: tuple[str, ...] = ()) -> tuple[str, str, int]:
        """(stdout, stderr, code) as compared: the store path, declared ids and declared
        timestamps renamed; the parts covered by an allowed difference masked."""
        def text(data: bytes) -> str:
            shown = data.decode("utf-8", "surrogateescape").replace(str(side.root), "<store>")
            shown = _rename(_TIME_PATTERNS, _rename(_ID_PATTERNS, shown, side.names), side.times)
            for name in allow:
                assert name in DIFFERENCES
                shown = MASKS[name].sub(f"<{name}>", shown)
            return shown
        return text(done.stdout), text(done.stderr), done.code

    def assert_same_output(self, py: Completed, rs: Completed, allow: tuple[str, ...] = ()) -> None:
        for name in allow:
            DIFFERENCES[name]    # KeyError: not a listed difference
        left, right = self.rendered(self.py, py, allow), self.rendered(self.rs, rs, allow)
        assert left == right, f"the outputs differ\npython: {left!r}\nrust:   {right!r}"


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
    `store`) and checks the result before any case may use it. Returns the store."""
    from memriver_core.repository.sqlite.database import _SCHEMA

    home, work, root = tmp_path / "home", tmp_path / "work", tmp_path / "store"

    def text(file: str) -> str:
        return ((seeds / name / file).read_text(encoding="utf-8")
                .replace("{{WORK}}", str(work)).replace("{{HOME}}", str(home)))
    layout = json.loads(text("layout.json"))
    for directory in (home, work):
        directory.mkdir(parents=True)
    for repository in layout["git"]:
        _repository(Path(repository["path"]), repository["branch"], repository["worktrees"],
                    home=home, root=root, cwd=work)
    for directory in layout["directories"]:
        Path(directory).mkdir(parents=True, exist_ok=True)
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
