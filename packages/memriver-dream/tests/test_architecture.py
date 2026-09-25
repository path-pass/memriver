"""memriver-dream's dependency rules: core only through its public surface, and no
harness anywhere -- no harness name, no subprocess, no harness file format.

The same import normalization as the core and umbrella architecture tests:
every import spelling (plain, from, relative, aliased) is treated alike.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import memriver_core
import memriver_dream
import pytest

SRC = Path(memriver_dream.__file__).parent
ROOT_PKG = "memriver_dream"
PUBLIC_CORE = ("memriver_core.bootstrap", "memriver_core.models", "memriver_core.settings")
FORBIDDEN_STDLIB = {"subprocess"}
HARNESS_WORDS = ("claude", "codex", "jsonl", "anthropic", "openai")
STDLIB = set(sys.stdlib_module_names)


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


def _under(candidate: str, package: str) -> bool:
    return candidate == package or candidate.startswith(package + ".")


def _allowed_core(target: str) -> bool:
    # the root package is the public error surface: `from memriver_core import GroupConflict`
    return (target == "memriver_core" or target.removeprefix("memriver_core.")
            in memriver_core.__all__ or any(_under(target, p) for p in PUBLIC_CORE))


def test_dream_imports_only_stdlib_itself_and_the_public_core_surface():
    assert SOURCES, "no production module under memriver_dream"
    for module in SOURCES:
        for target in _imports(module):
            root = target.split(".", 1)[0]
            if root == ROOT_PKG:
                continue
            if root == "memriver_core":
                assert _allowed_core(target), f"{module} reaches into core: {target}"
            elif root in STDLIB:
                assert root not in FORBIDDEN_STDLIB, f"{module} imports {root}"
            else:
                pytest.fail(f"{module} imports {target}: memriver-dream adds no dependency")


@pytest.mark.parametrize("word", HARNESS_WORDS)
def test_dream_sources_name_no_harness(word):
    for module, path in SOURCES.items():
        assert word not in path.read_text(encoding="utf-8").lower(), f"{module} names {word}"
