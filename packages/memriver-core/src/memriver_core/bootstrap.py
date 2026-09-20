"""The single composition point: concrete adapters are named only here."""

from __future__ import annotations

from pathlib import Path

from .application.diagnostics import DiagnosticsService

# EMPTY_INDEX is re-exported (not composed) here: bootstrap is the one
# memriver_core surface, alongside config/models, that a transport may import
# -- so a transport comparing against MemoryService.index's own sentinel does
# not need its own copy of the literal, nor a reach into application.service.
from .application.service import EMPTY_INDEX, MemoryService
from .config import DEFAULT_MAX_BODY_CHARS, Settings
from .content_policy.secret_scanner import SecretScanner
from .repository.filesystem import FileMemoryRepository, FilesystemStoreInspector

# store_lock is re-exported for the umbrella's project registry, whose writes
# must serialize with entry writes; bootstrap is the one surface a transport
# may import it from.
from .repository.filesystem.locking import store_lock

__all__ = ["EMPTY_INDEX", "build_diagnostics_service", "build_service", "store_lock"]


def build_service(settings: Settings, *, root: Path | None = None) -> MemoryService:
    # an explicit root is authoritative: callers that already resolved it (the
    # CLI, the tests) must not have it replaced by the environment or settings
    memory_repository = FileMemoryRepository(settings.root if root is None else root)
    content_policy = SecretScanner()
    return MemoryService(
        memory_repository,
        content_policy,
        max_body_chars=settings.max_body_chars,
        # metadata keeps the default budget, so a tightened body limit does
        # not silently change harness/name/description acceptance
        metadata_max_chars=DEFAULT_MAX_BODY_CHARS,
        search_limit_default=settings.search_limit_default,
        search_limit_max=settings.search_limit_max,
        index_budget_lines=settings.index_budget_lines,
    )


def build_diagnostics_service(
    settings: Settings, *, root: Path | None = None,
) -> DiagnosticsService:
    # same explicit-root precedence as build_service: a root the caller already
    # resolved must not be replaced by the environment or settings
    inspector = FilesystemStoreInspector(settings.root if root is None else root)
    return DiagnosticsService(inspector)
