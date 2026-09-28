"""ProjectService: projects and global, bindings and directory-mode contexts.

ProjectStore owns collections and directories; this service creates, reads,
lists, binds and unbinds projects and builds the context one directory
grants. Nothing here touches a file, a table or settings: every limit is
injected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from memriver_core.application.contexts import (
    NONE_HEADER,
    degraded_context,
    no_project_context,
    registered_context,
    unavailable_context,
)
from memriver_core.models import (
    Project,
    ProjectContext,
    Resolution,
    RootPlan,
    UnbindPlan,
)
from memriver_core.models.errors import IdCollision, StorageFailure

if TYPE_CHECKING:
    from memriver_core.repository.protocol import ProjectStore


class ProjectService:
    def __init__(self, project_store: ProjectStore, *, header_field_chars: int,
                 project_name_max_chars: int) -> None:
        self._project_store = project_store
        self._header_field_chars = header_field_chars
        self._project_name_max_chars = project_name_max_chars

    def open_project_context(self, start: str) -> ProjectContext:
        """The header, state and read/write set for one directory. Never raises for a
        store problem: an unreadable store is an empty, clearly labelled project context."""
        try:
            resolution = self._project_store.resolve(start)
            global_project_id = self._project_store.global_project_id()
        except StorageFailure:
            return unavailable_context(None)
        project = resolution.project
        if resolution.state == "registered" and project is not None \
                and project.id != global_project_id:
            return registered_context(project, global_project_id,
                                      header_field_chars=self._header_field_chars)
        if resolution.state == "degraded":
            return degraded_context(resolution.diagnostic or "", global_project_id,
                                    header_field_chars=self._header_field_chars)
        return no_project_context("none", NONE_HEADER, global_project_id)

    def global_project_id(self) -> str | None:
        return self._project_store.global_project_id()

    def ensure_global(self) -> str:
        try:
            return self._project_store.ensure_global()
        except IdCollision as err:
            raise StorageFailure from err

    def read_project(self, project_id: str) -> Project:
        return self._project_store.read(project_id)

    def list_projects(self) -> list[Project]:
        return self._project_store.list_projects()

    def plan_root(self, directory: str, project_id: str | None = None) -> RootPlan:
        return self._project_store.plan_root(directory, project_id)

    def init_project(self, name: str, plan: RootPlan) -> Project:
        project = Project.new(name, max_chars=self._project_name_max_chars)
        try:
            self._project_store.create(project, plan)
        except IdCollision as err:
            raise StorageFailure from err
        return Project(id=project.id, name=project.name, root=plan.root)

    def adopt(self, project_id: str, plan: RootPlan) -> None:
        self._project_store.bind(project_id, plan)

    def plan_unbind(self, project_id: str, root: str,
                    cwd: str) -> tuple[UnbindPlan, Resolution]:
        return self._project_store.plan_unbind(project_id, root, cwd)

    def unbind(self, plan: UnbindPlan) -> None:
        self._project_store.unbind(plan)
