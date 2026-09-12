"""The single composition point: concrete adapters are named only here."""

from __future__ import annotations

from pathlib import Path

from .application.diagnostics import DiagnosticsService
from .application.service import MemoryService
from .config import DEFAULT_MAX_BODY_CHARS, Settings
from .content_policy.secret_scanner import SecretScanner
from .repository.filesystem import FileMemoryRepository, FilesystemStoreInspector


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
