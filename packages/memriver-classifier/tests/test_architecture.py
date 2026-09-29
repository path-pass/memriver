"""memriver-classifier's dependency rules: core only through its root package and its
settings module, pydantic in the settings module only, the standard library -- never
memriver_dream or the memriver umbrella -- and the agreed module layout, every module
name without an underscore.

The same import normalization as the other packages' architecture tests: every import
spelling (plain, from, relative, aliased) is treated alike.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import memriver_classifier
import memriver_core
import pytest

SRC = Path(memriver_classifier.__file__).parent
ROOT_PKG = "memriver_classifier"
MODULES = {ROOT_PKG, *(f"{ROOT_PKG}.{name}" for name in ("headless", "backends", "settings"))}
SETTINGS_MODULE = f"{ROOT_PKG}.settings"
STDLIB = set(sys.stdlib_module_names)
NEVER = {"memriver_dream", "memriver"}


def _module_name(path: Path) -> str:
    parts = path.relative_to(SRC).with_suffix("").parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join((ROOT_PKG, *parts)) if parts else ROOT_PKG


SOURCES = {_module_name(p): p for p in sorted(SRC.rglob("*.py"))}


def _package_of(module: str) -> str:
    return module if SOURCES[module].name == "__init__.py" else module.rpartition(".")[0]


def _imports(module: str) -> set[str]:
    targets: set[str] = set()
    for node in ast.walk(ast.parse(SOURCES[module].read_text(encoding="utf-8"))):
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


def _allowed_core(target: str) -> bool:
    # `from memriver_core import Verdict` also yields "memriver_core.Verdict"
    return (target in ("memriver_core", "memriver_core.settings")
            or target.startswith("memriver_core.settings.")
            or target.removeprefix("memriver_core.") in memriver_core.__all__)


def test_the_package_imports_only_stdlib_itself_pydantic_and_the_public_core():
    assert SOURCES, "no production module under memriver_classifier"
    for module in SOURCES:
        for target in _imports(module):
            root = target.split(".", 1)[0]
            if root == ROOT_PKG or root in STDLIB:
                continue
            if root == "memriver_core":
                assert _allowed_core(target), f"{module} reaches into core: {target}"
            elif root == "pydantic":
                assert module == SETTINGS_MODULE, f"{module} imports pydantic"
            else:
                pytest.fail(f"{module} imports {target}: memriver-classifier adds no other "
                            "dependency")


def test_the_package_never_imports_dream_or_the_umbrella():
    for module in SOURCES:
        roots = {target.split(".", 1)[0] for target in _imports(module)}
        assert not roots & NEVER, f"{module} imports {sorted(roots & NEVER)}"


def test_the_package_keeps_the_agreed_module_layout():
    assert set(SOURCES) <= MODULES, sorted(set(SOURCES) - MODULES)
    assert not [module for module in SOURCES if "_" in module.removeprefix(ROOT_PKG)]
