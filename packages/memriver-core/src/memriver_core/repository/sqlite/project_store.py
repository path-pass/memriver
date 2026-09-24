"""`ProjectStore` over the SQLite database: projects, global, search, directories."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from memriver_core.models import (
    ID_RE,
    Memory,
    Project,
    ReadWriteSet,
    Resolution,
    RootPlan,
    UnbindPlan,
    new_id,
)
from memriver_core.models.errors import (
    BindingRefused,
    IdCollision,
    ProjectNotFound,
    StorageFailure,
)

from .. import directories
from .database import (
    MEMORY_COLUMNS,
    PROJECT_COLUMNS,
    Database,
    memory_from_row,
    project_from_row,
)

GLOBAL_PROJECT_NAME = "global"


def _project(row) -> tuple[Project, bool]:
    try:
        return project_from_row(row)
    except ValueError as err:
        raise StorageFailure from err


def _bound(conn: sqlite3.Connection | None) -> list[Project]:
    """Every project that holds a directory; one invalid row fails the whole answer.

    A partly read set of bindings could hide the nearer project, so there is
    no skipping here.
    """
    if conn is None:
        return []
    rows = conn.execute(f"SELECT {PROJECT_COLUMNS} FROM projects WHERE root IS NOT NULL "
                        "ORDER BY id").fetchall()
    return [_project(row)[0] for row in rows]


def _bound_elsewhere(conn: sqlite3.Connection, root: str) -> BindingRefused | None:
    """After an IntegrityError on writing `root`: "bound-elsewhere" if a row now holds it.

    None for any other constraint; the caller re-raises it and the write turns
    it into StorageFailure. An owner id the lenient text_factory could not
    decode, or one that is not addressable, is damage, not a binding to name.
    """
    owner = conn.execute("SELECT id FROM projects WHERE root = ?", (root,)).fetchone()
    if owner is None:
        return None
    owner_id = owner[0]
    if not isinstance(owner_id, str) or not ID_RE.fullmatch(owner_id):
        raise StorageFailure
    return BindingRefused("bound-elsewhere", owner_id)


def _same(a: str, b: str) -> bool:
    same = a == b or directories.same_directory(a, b)
    if same is None:
        raise BindingRefused("unverifiable")
    return bool(same)


class SqliteProjectStore:
    def __init__(self, root: Path, *, home: Path, busy_timeout_ms: int) -> None:
        self.root = Path(root)
        self._home = Path(home)
        self._busy_timeout_ms = busy_timeout_ms
        self._database = Database(self.root, busy_timeout_ms=busy_timeout_ms)

    # --- collections ---

    def create(self, project: Project, plan: RootPlan) -> None:
        if not ID_RE.fullmatch(project.id):
            raise ValueError("invalid project id")
        # the filesystem-only checks first, so a refused plan creates no store
        self._check_confirmed(None, plan, project_id=None)
        with self._pinned_write(plan.store) as conn:
            self._check_confirmed(conn, plan, project_id=None)
            if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project.id,)).fetchone():
                raise IdCollision(project.id)
            # a row the read path would reject is never committed: the same
            # decoder that would refuse it on the next read refuses it now
            project_from_row((project.id, project.name, plan.root, 0))
            try:
                conn.execute("INSERT INTO projects (id, name, root, is_global) VALUES (?, ?, ?, 0)",
                             (project.id, project.name, plan.root))
            except sqlite3.IntegrityError:
                refusal = _bound_elsewhere(conn, plan.root)
                if refusal is None:
                    raise
                raise refusal from None

    def read(self, project_id: str) -> Project:
        if not isinstance(project_id, str) or not ID_RE.fullmatch(project_id):
            raise ProjectNotFound(project_id)
        with self._database.read() as conn:
            row = None if conn is None else conn.execute(
                f"SELECT {PROJECT_COLUMNS} FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise ProjectNotFound(project_id)
        return _project(row)[0]

    def list_projects(self) -> list[Project]:
        with self._database.read() as conn:
            rows = [] if conn is None else conn.execute(
                f"SELECT {PROJECT_COLUMNS} FROM projects ORDER BY is_global, name, id").fetchall()
        projects = []
        for row in rows:
            try:
                projects.append(project_from_row(row)[0])
            except ValueError:
                continue                            # a bulk scan skips it; doctor reports it
        return projects

    def global_project_id(self) -> str | None:
        with self._database.read() as conn:
            row = None if conn is None else conn.execute(
                f"SELECT {PROJECT_COLUMNS} FROM projects WHERE is_global = 1").fetchone()
        return None if row is None else _project(row)[0].id

    def ensure_global(self) -> str:
        with self._database.write() as conn:
            row = conn.execute(f"SELECT {PROJECT_COLUMNS} FROM projects WHERE is_global = 1").fetchone()
            if row is not None:
                return _project(row)[0].id
            project_id = new_id()
            if conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone():
                raise IdCollision(project_id)       # the facade reports StorageFailure
            conn.execute("INSERT INTO projects (id, name, root, is_global) VALUES (?, ?, NULL, 1)",
                         (project_id, GLOBAL_PROJECT_NAME))
            return project_id

    def search(self, project_id: str, read_write_set: ReadWriteSet | None, *,
               query: str | None, limit: int | None) -> list[Memory]:
        if read_write_set is not None and project_id not in read_write_set.readable():
            return []
        if not isinstance(project_id, str) or not ID_RE.fullmatch(project_id):
            return []
        needle = None
        if query is not None:
            needle = query.replace("\x00", "").lower()
            if not needle:
                return []
        with self._database.read() as conn:
            if conn is None:
                return []
            row = conn.execute(f"SELECT {PROJECT_COLUMNS} FROM projects WHERE id = ?",
                               (project_id,)).fetchone()
            if row is None:
                return []
            _project(row)                           # an invalid project row is StorageFailure
            rows = conn.execute(f"SELECT {MEMORY_COLUMNS} FROM memories "
                                "WHERE project_id = ? AND deleted_at IS NULL",
                                (project_id,)).fetchall()
        matches: list[Memory] = []
        for memory_row in rows:
            try:
                memory = memory_from_row(memory_row)
            except ValueError:
                continue                            # one bad row is skipped; doctor reports it
            # ponytail: the substring match runs in Python over one project's
            # rows (str.lower folds beyond ASCII, SQLite's LIKE does not); add
            # FTS when a project outgrows it
            if needle is None or needle in memory.description.lower() \
                    or needle in memory.body.lower():
                matches.append(memory)
        matches.sort(key=lambda m: (m.updated, m.id), reverse=True)
        return matches if limit is None else matches[:limit]

    # --- directories ---

    def resolve(self, start: str, *, ignoring: tuple[str, str] | None = None) -> Resolution:
        with self._database.read() as conn:
            bound = _bound(conn)
        if ignoring is not None:
            bound = [p for p in bound if (p.id, p.root) != ignoring]
        match = directories.nearest_bound(start, [(p.id, p.root) for p in bound])
        if match.state != "registered":
            return Resolution(match.state, diagnostic=match.diagnostic)
        return Resolution("registered", project=next(p for p in bound if p.id == match.project_id))

    def plan_root(self, directory: str, project_id: str | None) -> RootPlan:
        with self._database.read() as conn:
            project = self._adoptable(conn, project_id) if project_id is not None else None
            bound = _bound(conn)
        canonical = directories.canonical_directory(directory)
        if canonical is None:
            raise BindingRefused("not-a-directory")
        store = self._canonical_store()
        self._refuse(canonical, store, bound, project_id)
        already_bound = False
        if project is not None and project.root is not None:
            if not _same(project.root, canonical):
                raise BindingRefused("has-directory")
            already_bound = True
        nested = []
        for other in bound:
            if other.id == project_id:
                continue
            inside = directories.covers(canonical, other.root)
            if inside is None:
                raise BindingRefused("unverifiable")
            if inside and not _same(canonical, other.root):
                nested.append(other)
        return RootPlan(root=canonical, store=store, nested=tuple(nested),
                        already_bound=already_bound)

    def bind(self, project_id: str, plan: RootPlan) -> None:
        if not isinstance(project_id, str) or not ID_RE.fullmatch(project_id):
            raise BindingRefused("no-such-project")
        with self._pinned_write(plan.store, missing="no-such-project") as conn:
            project = self._adoptable(conn, project_id)
            self._check_confirmed(conn, plan, project_id=project_id)
            # decided on the current row, never on plan.already_bound
            if project.root is not None:
                if _same(project.root, plan.root):
                    return
                raise BindingRefused("has-directory")
            # bind writes only root; validate the full row as it will stand
            project_from_row((project.id, project.name, plan.root, 0))
            try:
                conn.execute("UPDATE projects SET root = ? WHERE id = ? AND root IS NULL",
                             (plan.root, project_id))
            except sqlite3.IntegrityError:
                refusal = _bound_elsewhere(conn, plan.root)
                if refusal is None:
                    raise
                raise refusal from None

    def plan_unbind(self, project_id: str, root: str, cwd: str) -> tuple[UnbindPlan, Resolution]:
        if not isinstance(project_id, str) or not ID_RE.fullmatch(project_id):
            raise BindingRefused("no-such-project")
        store = self._canonical_store()
        with self._database.read() as conn:
            row = None if conn is None else conn.execute(
                f"SELECT {PROJECT_COLUMNS} FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise BindingRefused("no-such-project")
        stored = _project(row)[0].root
        if stored is None or not self._names_root(root, stored):
            raise BindingRefused("binding-changed")
        plan = UnbindPlan(project_id=project_id, root=stored, store=store)
        return plan, self.resolve(cwd, ignoring=(project_id, stored))

    def unbind(self, plan: UnbindPlan) -> None:
        with self._pinned_write(plan.store, missing="binding-changed") as conn:
            cursor = conn.execute("UPDATE projects SET root = NULL WHERE id = ? AND root = ?",
                                  (plan.project_id, plan.root))
            if cursor.rowcount != 1:
                raise BindingRefused("binding-changed")

    # --- helpers ---

    def _canonical_store(self) -> str:
        # non-strict: the store may not exist yet when init plans its first project
        return os.path.realpath(self.root)

    def _canonical_home(self) -> str:
        try:
            return str(self._home.resolve())
        except (OSError, RuntimeError, ValueError):
            return str(self._home)

    @contextmanager
    def _pinned_write(self, store: str, *,
                      missing: str | None = None) -> Iterator[sqlite3.Connection]:
        """A write transaction on exactly the store a confirmed plan was made against.

        Checked before anything is opened or created, so a store redirected
        during the prompt to a place with no database gets no database.
        `missing` is the refusal for an operation that cannot succeed without
        an existing database (bind, unbind): it never creates one.
        """
        if self._canonical_store() != store:
            raise BindingRefused("plan-changed")
        database = Database(Path(store), busy_timeout_ms=self._busy_timeout_ms)
        if missing is not None and not database.exists():
            raise BindingRefused(missing)
        with database.write() as conn:
            yield conn

    def _adoptable(self, conn: sqlite3.Connection | None, project_id: str | None) -> Project:
        if not isinstance(project_id, str) or not ID_RE.fullmatch(project_id) or conn is None:
            raise BindingRefused("no-such-project")
        row = conn.execute(f"SELECT {PROJECT_COLUMNS} FROM projects WHERE id = ?",
                           (project_id,)).fetchone()
        if row is None:
            raise BindingRefused("no-such-project")
        project, is_global = _project(row)
        if is_global:
            raise BindingRefused("is-global")
        return project

    def _check_confirmed(self, conn: sqlite3.Connection | None, plan: RootPlan, *,
                         project_id: str | None) -> None:
        """The confirmed target is a precondition, not re-planned.

        With no connection only the filesystem checks run (no bindings to compare).
        """
        if directories.canonical_directory(plan.root) != plan.root:
            raise BindingRefused("plan-changed")
        self._refuse(plan.root, plan.store, _bound(conn), project_id)

    def _refuse(self, root: str, store: str, bound: list[Project],
                project_id: str | None) -> None:
        verdict = directories.covers(root, self._canonical_home())
        if verdict is None:
            raise BindingRefused("unverifiable")
        if verdict:
            raise BindingRefused("covers-home")
        verdict = directories.covers(root, store)
        if verdict is None:
            raise BindingRefused("unverifiable")
        if verdict:
            raise BindingRefused("covers-store")
        verdict = directories.covers(store, root)
        if verdict is None:
            raise BindingRefused("unverifiable")
        if verdict:
            raise BindingRefused("inside-store")
        for other in bound:
            if other.id != project_id and _same(root, other.root):
                raise BindingRefused("bound-elsewhere", other.id)

    @staticmethod
    def _names_root(given: str, stored: str) -> bool:
        """The literal spelling first, then the strict realpath; the path need not exist."""
        if given == stored:
            return True
        try:
            return str(Path(given).resolve(strict=True)) == stored
        except (OSError, RuntimeError, ValueError):
            return False
