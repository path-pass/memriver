"""The single composition point: concrete adapters are named only here."""

from __future__ import annotations

from pathlib import Path

from .application.diagnostics import DiagnosticsService

# EMPTY_INDEX is re-exported (not composed) here: bootstrap is the one
# memriver_core surface, alongside config/models, that a transport may import.
from .application.service import EMPTY_INDEX, MemoryService
from .config import DEFAULT_MAX_BODY_CHARS, Settings
from .content_policy.secret_scanner import SecretScanner
from .repository.filesystem import (
    FileMemoryStore,
    FileProjectStore,
    FilesystemStoreInspector,
)

# store_lock is re-exported for the umbrella's directory registry, whose
# writes must serialize with store writes.
from .repository.filesystem.locking import store_lock

__all__ = ["EMPTY_INDEX", "build_diagnostics_service", "build_service", "store_lock"]


def build_service(settings: Settings, *, root: Path | None = None) -> MemoryService:
    # an explicit root is authoritative: callers that already resolved it (the
    # CLI, the tests) must not have it replaced by the environment or settings
    store_root = settings.root if root is None else root
    project_store = FileProjectStore(store_root)
    memory_store = FileMemoryStore(store_root, project_store)
    return MemoryService(
        memory_store,
        project_store,
        SecretScanner(),
        max_body_chars=settings.max_body_chars,
        # metadata keeps the default budget, so a tightened body limit does
        # not silently change harness/description acceptance
        metadata_max_chars=DEFAULT_MAX_BODY_CHARS,
        search_limit_default=settings.search_limit_default,
        search_limit_max=settings.search_limit_max,
        index_budget_lines=settings.index_budget_lines,
    )


def build_diagnostics_service(
    settings: Settings, *, root: Path | None = None,
) -> DiagnosticsService:
    inspector = FilesystemStoreInspector(settings.root if root is None else root)
    return DiagnosticsService(inspector)
