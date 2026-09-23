"""`ProjectStore` over `projects/<id>.toml` files and the `store.toml` manifest."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from memriver_core.models import ID_RE, AccessContext, Memory, Project, project_name
from memriver_core.models.errors import (
    IdCollision,
    ProjectNotFound,
    StorageFailure,
)

from .files import (
    MANIFEST_FILENAME,
    PROJECTS_DIRNAME,
    data_dir_exists,
    project_path,
    read_regular_text,
    write_new,
)
from .locking import store_lock
from .memory_files import iter_memories

GLOBAL_PROJECT_NAME = "global"


def _toml_string(value: str) -> str:
    # every value written here is single-line (a project name after
    # project_name(), or an id), so it holds no control character, and a JSON
    # string literal of it is also a valid TOML basic string
    return json.dumps(value, ensure_ascii=False)


def _read_document(path: Path) -> dict | None:
    """A strict TOML document, None when absent; StorageFailure when unusable."""
    try:
        text = read_regular_text(path)
    except (OSError, UnicodeDecodeError) as err:
        raise StorageFailure from err
    if text is None:
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise StorageFailure from err


class FileProjectStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # --- port ---

    def create(self, project: Project) -> None:
        with store_lock(self.root):
            self._write_project(project)

    def read(self, project_id: str) -> Project:
        if not ID_RE.fullmatch(project_id):
            raise ProjectNotFound(project_id)
        try:
            if not data_dir_exists(self.root, PROJECTS_DIRNAME):
                raise ProjectNotFound(project_id)
        except OSError as err:
            raise StorageFailure from err
        document = _read_document(project_path(self.root, project_id))
        if document is None:
            raise ProjectNotFound(project_id)
        name = document.get("name")
        if set(document) != {"name"} or not isinstance(name, str):
            raise StorageFailure
        try:
            if project_name(name) != name:
                raise StorageFailure
        except ValueError as err:
            raise StorageFailure from err
        return Project(id=project_id, name=name)

    def global_project_id(self) -> str | None:
        document = _read_document(self.root / MANIFEST_FILENAME)
        if document is None:
            return None
        value = document.get("global_project")
        if set(document) != {"global_project"} or not isinstance(value, str) \
                or not self.exists(value):
            raise StorageFailure
        return value

    def ensure_global(self) -> str:
        with store_lock(self.root):
            existing = self.global_project_id()     # StorageFailure: write nothing
            if existing is not None:
                return existing
            project = Project.new(GLOBAL_PROJECT_NAME)
            # ponytail: two files, not one transaction; a crash in between
            # leaves an unreferenced empty project and the next run makes a
            # new one. Harmless (an empty project is legal); doctor lists it.
            # IdCollision propagates with nothing written; the facade turns
            # it into StorageFailure on this, its only call.
            self._write_project(project)
            try:
                write_new(self.root, self.root / MANIFEST_FILENAME,
                          f"global_project = {_toml_string(project.id)}\n")
            except OSError as err:
                raise StorageFailure from err
            return project.id

    def search(self, project_id: str, ctx: AccessContext, *, query: str | None,
               limit: int | None) -> list[Memory]:
        if project_id not in ctx.readable():
            return []
        try:
            self.read(project_id)       # a damaged project file is StorageFailure
        except ProjectNotFound:
            return []
        needle = None
        if query is not None:
            needle = query.replace("\x00", "").lower()
            if not needle:
                return []
        # ponytail: a linear scan of memories/ per search; add an index when a
        # store outgrows it
        matches = [m for m in iter_memories(self.root)
                   if m.project_id == project_id
                   and (needle is None or needle in m.description.lower()
                        or needle in m.body.lower())]
        matches.sort(key=lambda m: (m.updated, m.id), reverse=True)
        return matches if limit is None else matches[:limit]

    # --- helpers shared with the memory store and the inspector ---

    def exists(self, project_id: str) -> bool:
        """Lock-free: projects are not deleted through memriver, so a yes stays true.

        Only absence is False; a damaged project file is StorageFailure.
        """
        try:
            self.read(project_id)
        except ProjectNotFound:
            return False
        return True

    def _write_project(self, project: Project) -> None:
        if not ID_RE.fullmatch(project.id):
            raise ValueError("invalid project id")
        text = f"name = {_toml_string(project_name(project.name))}\n"
        try:
            write_new(self.root, project_path(self.root, project.id), text)
        except FileExistsError:
            raise IdCollision(project.id) from None     # nothing written, never overwritten
        except OSError as err:
            raise StorageFailure from err
