"""Dependency-rule enforcement: every forbidden edge of the architecture spec.

Walks the AST of every production module under ``memriver_core`` and asserts the
allowed import direction. Tests and the outer ``memriver`` package are outside
these assertions on purpose: implementation tests import concrete adapters
directly, and the umbrella package is a composition root of its own.

A failure here means the dependency is wrong, not that the rule is wrong.
"""

from __future__ import annotations

import ast
from pathlib import Path

import memriver_core
import pytest

SRC = Path(memriver_core.__file__).parent
ROOT_PKG = "memriver_core"


def _module_name(path: Path) -> str:
    parts = path.relative_to(SRC).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join((ROOT_PKG, *parts))


SOURCES = {_module_name(p): p for p in sorted(SRC.rglob("*.py"))}


def _parse(module: str) -> ast.Module:
    return ast.parse(SOURCES[module].read_text(encoding="utf-8"))


def _package_of(module: str) -> str:
    """The package a relative import in ``module`` is resolved against."""
    # a module maps to its parent package; a package (__init__) maps to itself
    return module if SOURCES[module].name == "__init__.py" else module.rpartition(".")[0]


def _imported_modules(module: str) -> set[str]:
    """Every module target imported by ``module``, relative imports resolved.

    ``from x.y import z`` yields both ``x.y`` and ``x.y.z``: the name may be a
    submodule, and a rule about ``x.y.z`` must catch it either way.
    """
    targets: set[str] = set()
    for node in ast.walk(_parse(module)):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = _package_of(module).split(".")
                anchor = anchor[: len(anchor) - node.level + 1]
                base = ".".join([*anchor, base]) if base else ".".join(anchor)
            targets.add(base)
            targets.update(f"{base}.{alias.name}" for alias in node.names)
    return targets


def _imported_names(module: str) -> set[str]:
    """Every symbol bound by a ``from ... import name`` in ``module``."""
    return {alias.name
            for node in ast.walk(_parse(module))
            if isinstance(node, ast.ImportFrom)
            for alias in node.names}


def _under(candidate: str, package: str) -> bool:
    return candidate == package or candidate.startswith(package + ".")


def _modules_under(package: str) -> list[str]:
    return [m for m in SOURCES if _under(m, package)]


# --- the forbidden-edge table (one row per spec rule) ------------------------

FORBIDDEN = [
    # models import stdlib and the pure ulid value generator only
    ("memriver_core.models", "memriver_core.application"),
    ("memriver_core.models", "memriver_core.repository"),
    ("memriver_core.models", "memriver_core.content_policy"),
    ("memriver_core.models", "memriver_core.config"),
    ("memriver_core.models", "pydantic"),
    ("memriver_core.models", "frontmatter"),
    ("memriver_core.models", "fcntl"),
    # the application talks to protocols, never to adapters, config, or I/O
    ("memriver_core.application", "memriver_core.repository.filesystem"),
    ("memriver_core.application", "memriver_core.content_policy.secret_scanner"),
    ("memriver_core.application", "memriver_core.config"),
    ("memriver_core.application", "pydantic"),
    ("memriver_core.application", "frontmatter"),
    ("memriver_core.application", "os"),
    ("memriver_core.application", "fcntl"),
    ("memriver_core.application", "tempfile"),
    ("memriver_core.application", "pathlib"),
    # a protocol never knows its implementation
    ("memriver_core.repository.protocol", "memriver_core.repository.filesystem"),
    ("memriver_core.repository.protocol", "memriver_core.application"),
    ("memriver_core.repository.protocol", "memriver_core.config"),
    ("memriver_core.content_policy.protocol",
     "memriver_core.content_policy.secret_scanner"),
    ("memriver_core.content_policy.protocol", "memriver_core.application"),
    ("memriver_core.content_policy.protocol", "memriver_core.config"),
    # implementations may use the error taxonomy, never the service or config
    ("memriver_core.repository.filesystem", "memriver_core.application.service"),
    ("memriver_core.repository.filesystem", "memriver_core.config"),
    ("memriver_core.content_policy.secret_scanner", "memriver_core.application.service"),
    ("memriver_core.content_policy.secret_scanner", "memriver_core.config"),
]


@pytest.mark.parametrize(("subject", "forbidden"), FORBIDDEN,
                         ids=[f"{s}-x->{f}" for s, f in FORBIDDEN])
def test_forbidden_import_edge(subject, forbidden):
    modules = _modules_under(subject)
    assert modules, f"no production module under {subject}"
    for module in modules:
        offenders = [t for t in _imported_modules(module) if _under(t, forbidden)]
        assert not offenders, f"{module} must not import {forbidden}: {offenders}"


# --- composition-root rules -------------------------------------------------

@pytest.mark.parametrize("adapter", ["FileMemoryRepository", "SecretScanner"])
def test_only_bootstrap_names_a_concrete_adapter(adapter):
    for module in SOURCES:
        if module == "memriver_core.bootstrap":
            continue
        # the adapter's own module and its package __init__ export it; naming it
        # anywhere else in production sources would be a second assembly point
        if module in ("memriver_core.repository.filesystem",
                      "memriver_core.repository.filesystem.repository",
                      "memriver_core.content_policy.secret_scanner"):
            continue
        assert adapter not in _imported_names(module), \
            f"{module} imports {adapter}; only bootstrap.py may assemble adapters"


@pytest.mark.parametrize("symbol", ["Settings", "load_settings"])
def test_only_config_and_bootstrap_import_settings(symbol):
    for module in SOURCES:
        if module == "memriver_core.bootstrap" or _under(module, "memriver_core.config"):
            continue
        assert symbol not in _imported_names(module), \
            f"{module} imports {symbol}; configuration stays in config/ and bootstrap"


# --- git discovery belongs to the umbrella package --------------------------

# gitleaks.toml is a secret-scanning rule resource under content_policy/; its
# name has nothing to do with git project discovery, which is why this rule is
# scoped to models/ and application/ rather than to the whole package.
GIT_MARKERS = ['".git"', "'.git'", "subprocess", "project_slug", "_git_root"]


@pytest.mark.parametrize("package", ["memriver_core.models", "memriver_core.application"])
@pytest.mark.parametrize("marker", GIT_MARKERS)
def test_no_git_discovery_in_models_or_application(package, marker):
    for module in _modules_under(package):
        source = SOURCES[module].read_text(encoding="utf-8")
        assert marker not in source, \
            f"{module} contains git-discovery code ({marker}); that belongs to memriver"
