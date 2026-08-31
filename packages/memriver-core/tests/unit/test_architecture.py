"""Dependency-rule enforcement: every forbidden edge of the architecture spec.

Walks the AST of every production module under ``memriver_core`` and asserts the
allowed import direction. Tests and the outer ``memriver`` package are outside
these assertions on purpose: implementation tests import concrete adapters
directly, and the umbrella package is a composition root of its own.

A failure here means the dependency is wrong, not that the rule is wrong.
"""

from __future__ import annotations

import ast
import sys
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


def _package_of(module: str) -> str:
    """The package a relative import in ``module`` is resolved against."""
    # a module maps to its parent package; a package (__init__) maps to itself
    return module if SOURCES[module].name == "__init__.py" else module.rpartition(".")[0]


def _imports_from_source(source: str, anchor_package: str) -> set[str]:
    """Normalize every import target in ``source``, relative imports resolved.

    This is the single helper every rule below evaluates against, covering
    both ``ast.Import`` and ``ast.ImportFrom`` (aliases included). ``from x.y
    import z`` yields both ``x.y`` and ``x.y.z``: the name may be a submodule,
    and a rule about ``x.y.z`` must catch it either way. Because it treats
    both import forms identically, ``import x.y as z``, ``from x import y``,
    ``from . import y`` and ``from .y import z as w`` are all equivalent for
    rule-checking: none of them can hide the module actually being reached.
    Takes raw text and an anchor package rather than a module name so it can
    also be exercised directly against synthetic sources in tests.
    """
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
    """Every module target imported by ``module`` (see ``_imports_from_source``)."""
    source = SOURCES[module].read_text(encoding="utf-8")
    return _imports_from_source(source, _package_of(module))


def _imported_names(module: str) -> set[str]:
    """Every symbol bound by a ``from ... import name`` in ``module``."""
    source = SOURCES[module].read_text(encoding="utf-8")
    return {alias.name
            for node in ast.walk(ast.parse(source))
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


# --- allowlists for the strict layers ----------------------------------------

# FORBIDDEN above is a blacklist: it only stops imports someone thought to
# name. For the innermost layers, a brand-new third-party dependency should
# fail loudly even when nobody remembered to blacklist it, so these layers are
# also allowlisted: anything that isn't stdlib and isn't named here fails,
# no matter what it is. This only adds strength — every FORBIDDEN row above
# still runs unchanged.
STDLIB = set(sys.stdlib_module_names)

ALLOWED_NON_STDLIB = {
    # models: stdlib + the pure ulid value generator + intra-models only
    "memriver_core.models": {"ulid", "memriver_core.models"},
    # application: stdlib + models + the two protocols + intra-application
    "memriver_core.application": {
        "memriver_core.models",
        "memriver_core.repository.protocol",
        "memriver_core.repository.inspection_protocol",
        "memriver_core.content_policy.protocol",
        "memriver_core.application",
    },
    # repository.protocol: stdlib + models, per spec section 3
    "memriver_core.repository.protocol": {
        "memriver_core.models",
        "memriver_core.repository.protocol",
    },
    # content_policy.protocol: stdlib only today (spec section 3) — keep it
    # that tight rather than pre-granting models it doesn't use yet.
    "memriver_core.content_policy.protocol": set(),
}


@pytest.mark.parametrize("layer", sorted(ALLOWED_NON_STDLIB))
def test_strict_layer_allowlist(layer):
    allowed = ALLOWED_NON_STDLIB[layer]
    modules = _modules_under(layer)
    assert modules, f"no production module under {layer}"
    for module in modules:
        for target in _imported_modules(module):
            root = target.split(".", 1)[0]
            if root in STDLIB or any(_under(target, entry) for entry in allowed):
                continue
            pytest.fail(
                f"{module} imports {target}, which is neither stdlib nor in "
                f"the {layer} allowlist {sorted(allowed)}"
            )


# --- composition-root rules -------------------------------------------------

# Concrete adapters may only be assembled in bootstrap.py. Reaching one via
# `import memriver_core.repository.filesystem as fs; fs.FileMemoryRepository()`
# must be caught exactly like `from memriver_core.repository.filesystem import
# FileMemoryRepository` — so, in addition to the from-import symbol check
# below, this also checks the normalized module-target set: importing the
# adapter's module at all, under any alias, from an unauthorized module is
# itself the violation.
CONCRETE_ADAPTER_MODULES = {
    "FileMemoryRepository": "memriver_core.repository.filesystem",
    "SecretScanner": "memriver_core.content_policy.secret_scanner",
    "FilesystemStoreInspector": "memriver_core.repository.filesystem",
}

# The services bootstrap composes are held to the same rule for the same
# reason: DiagnosticsService is not an adapter, but a second module naming it
# would be a second composition point just the same. It gets its own table
# because the exemption and the failure message differ in kind, not in force.
COMPOSED_SERVICE_MODULES = {
    "DiagnosticsService": "memriver_core.application.diagnostics",
}


def _assert_only_bootstrap_names(symbol: str, owning_module: str, what: str) -> None:
    for module in SOURCES:
        if module == "memriver_core.bootstrap":
            continue
        # the symbol's own package exports it and its modules import each
        # other freely (the codec, the layout helpers, the inspector); naming
        # it anywhere else in production sources would be a second assembly
        # point. The exemption is the package, not a list of sibling modules,
        # so adding a module inside it never weakens this rule by tempting
        # someone to widen the list.
        if _under(module, owning_module):
            continue
        assert symbol not in _imported_names(module), \
            f"{module} imports {symbol}; only bootstrap.py may assemble {what}"
        offenders = [t for t in _imported_modules(module) if _under(t, owning_module)]
        assert not offenders, (
            f"{module} imports the {owning_module} module ({offenders}); "
            f"only bootstrap.py may assemble {what}, even via a module alias"
        )


@pytest.mark.parametrize("adapter", sorted(CONCRETE_ADAPTER_MODULES))
def test_only_bootstrap_names_a_concrete_adapter(adapter):
    _assert_only_bootstrap_names(adapter, CONCRETE_ADAPTER_MODULES[adapter], "adapters")


@pytest.mark.parametrize("service", sorted(COMPOSED_SERVICE_MODULES))
def test_only_bootstrap_names_a_composed_service(service):
    _assert_only_bootstrap_names(service, COMPOSED_SERVICE_MODULES[service], "services")


def test_only_bootstrap_constructs_filesystem_inspector():
    # the inspector is an adapter like any other, so the rule above already
    # covers it; this pins the table entry that puts it under that rule.
    assert CONCRETE_ADAPTER_MODULES["FilesystemStoreInspector"] == (
        "memriver_core.repository.filesystem"
    )


def test_diagnostics_application_depends_only_on_models_and_inspection_port():
    imports = _imported_modules("memriver_core.application.diagnostics")
    core_imports = {name for name in imports if name.startswith("memriver_core.")}
    allowed = {
        "memriver_core.models",
        "memriver_core.repository.inspection_protocol",
    }
    offenders = [
        name for name in core_imports
        if not any(_under(name, prefix) for prefix in allowed)
    ]
    assert not offenders, (
        f"application/diagnostics may import models and the inspection port only: "
        f"{offenders}"
    )


@pytest.mark.parametrize("symbol", ["Settings", "load_settings"])
def test_only_config_and_bootstrap_import_settings(symbol):
    for module in SOURCES:
        if module == "memriver_core.bootstrap" or _under(module, "memriver_core.config"):
            continue
        assert symbol not in _imported_names(module), \
            f"{module} imports {symbol}; configuration stays in config/ and bootstrap"
        offenders = [t for t in _imported_modules(module) if _under(t, "memriver_core.config")]
        assert not offenders, (
            f"{module} imports the memriver_core.config module ({offenders}); "
            f"configuration stays in config/ and bootstrap, even via a module alias"
        )


# --- synthetic-source self-tests for the normalization helper ---------------
#
# These compile tiny synthetic modules (no files on disk) to prove the walker
# itself treats every import spelling as equivalent — the exact bypasses I-3
# named: a plain module import with an alias, a `from pkg import mod` module
# import, a `from pkg.mod import Name` symbol import, and a relative `from .
# import mod`. A clean module must not trip any of them.

@pytest.mark.parametrize(
    ("source", "anchor_package"),
    [
        # plain `import pkg.mod as alias`
        ("import memriver_core.repository.filesystem as fs\n", "memriver_core.bootstrap"),
        # `from pkg import mod` — reaches a module, not a name
        ("from memriver_core.repository import filesystem\n", "memriver_core.bootstrap"),
        # `from pkg.mod import Name`
        ("from memriver_core.repository.filesystem import FileMemoryRepository\n",
         "memriver_core.bootstrap"),
        # `from pkg.mod import Name as alias`
        ("from memriver_core.repository.filesystem import FileMemoryRepository as fmr\n",
         "memriver_core.bootstrap"),
        # relative `from . import mod`
        ("from . import filesystem\n", "memriver_core.repository"),
        # relative `from .mod import Name as alias`
        ("from .filesystem import FileMemoryRepository as w\n", "memriver_core.repository"),
    ],
)
def test_imports_from_source_catches_every_bypass_form(source, anchor_package):
    targets = _imports_from_source(source, anchor_package)
    assert any(_under(t, "memriver_core.repository.filesystem") for t in targets), (
        f"normalization missed a reference to memriver_core.repository.filesystem "
        f"in {source!r}: got {targets}"
    )


def test_imports_from_source_catches_config_module_alias_bypass():
    # the other bypass I-3 named: `import memriver_core.config as cfg; cfg.Settings(...)`
    targets = _imports_from_source(
        "import memriver_core.config as cfg\n", "memriver_core.application"
    )
    assert any(_under(t, "memriver_core.config") for t in targets)


@pytest.mark.parametrize(
    "source",
    [
        "import os\n",
        "from memriver_core.models import Memory\n",
        "from __future__ import annotations\n",
    ],
)
def test_imports_from_source_clean_module_passes(source):
    targets = _imports_from_source(source, "memriver_core.bootstrap")
    assert not any(_under(t, "memriver_core.repository.filesystem") for t in targets)
    assert not any(_under(t, "memriver_core.config") for t in targets)


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
