"""Dependency-rule enforcement for the umbrella package.

``memriver`` is a composition root of its own: it may depend on
``memriver_core``'s public root facade and its ``bootstrap``/``config``/
``models`` surface, but never reaches into ``application`` or ``repository``
internals directly, and ``memriver.install`` (a future task) may not import
``memriver_core`` at all -- it drives the CLI-facing planning/rendering
surface, never core policy.

Reuses the same import-normalization approach as memriver-core's own
architecture test (packages/memriver-core/tests/unit/test_architecture.py) so
every import spelling -- plain import, ``from pkg import mod``, ``from
pkg.mod import Name``, any alias, relative imports -- is treated identically.
``SOURCES`` globs every ``*.py`` file under the package, so a future
``hooks.py`` or ``doctor.py`` is covered by these rules automatically, with
no test edit needed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import memriver
import pytest

SRC = Path(memriver.__file__).parent
ROOT_PKG = "memriver"


def _module_name(path: Path) -> str:
    parts = path.relative_to(SRC).with_suffix("").parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join((ROOT_PKG, *parts)) if parts else ROOT_PKG


SOURCES = {_module_name(p): p for p in sorted(SRC.rglob("*.py"))}


def _package_of(module: str) -> str:
    return module if SOURCES[module].name == "__init__.py" else module.rpartition(".")[0]


def _imports_from_source(source: str, anchor_package: str) -> set[str]:
    targets: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = anchor_package.split(".")
                anchor = anchor[: len(anchor) - node.level + 1]
                base = ".".join([*anchor, base]) if base else ".".join(anchor)
            targets.add(base)
            targets.update(f"{base}.{alias.name}" for alias in node.names)
    return targets


def _imported_modules(module: str) -> set[str]:
    source = SOURCES[module].read_text(encoding="utf-8")
    return _imports_from_source(source, _package_of(module))


def _under(candidate: str, package: str) -> bool:
    return candidate == package or candidate.startswith(package + ".")


FORBIDDEN_ROOTS = ("memriver_core.application", "memriver_core.repository")


def test_umbrella_never_imports_core_application_or_repository_internals():
    assert SOURCES, "no production module found under memriver"
    for module in SOURCES:
        offenders = [
            t for t in _imported_modules(module)
            if any(_under(t, forbidden) for forbidden in FORBIDDEN_ROOTS)
        ]
        assert not offenders, f"{module} must not import {FORBIDDEN_ROOTS}: {offenders}"


INSTALL_PACKAGE = "memriver.install"


def test_install_modules_import_no_memriver_core_symbol_at_all():
    install_modules = [m for m in SOURCES if _under(m, INSTALL_PACKAGE)]
    if not install_modules:
        pytest.skip("memriver.install does not exist yet (a later task)")
    for module in install_modules:
        offenders = [t for t in _imported_modules(module) if _under(t, "memriver_core")]
        assert not offenders, (
            f"{module} imports memriver_core ({offenders}); install must never import "
            "memriver_core, including otherwise-public bootstrap/config/models"
        )


FORBIDDEN_NAMES = ("FilesystemStoreInspector", "DiagnosticsService")


def test_umbrella_never_names_the_concrete_inspector_or_diagnostics_service():
    for module, path in SOURCES.items():
        source = path.read_text(encoding="utf-8")
        for name in FORBIDDEN_NAMES:
            assert name not in source, (
                f"{module} references {name}; neither may be constructed or "
                "imported outside memriver_core.bootstrap"
            )
