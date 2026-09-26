"""memriver-dream's dependency rules: core only through its public surface, and no
harness anywhere -- no harness name, no subprocess, no harness file format.
The one exception is the settings module: it parses the user's [dream] table,
whose keys and values (executor = "codex", codex_overrides) name the executor.

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
FORBIDDEN_STDLIB = {"subprocess", "pty", "multiprocessing"}
# os itself is used for ordinary things (os.path, ...); only these calls shell out or
# spawn another process
_FORBIDDEN_OS_CALLS = ("system", "popen", "fork", "forkpty")
HARNESS_WORDS = ("claude", "codex", "jsonl", "anthropic", "openai")
STDLIB = set(sys.stdlib_module_names)
# the settings baseline core already depends on, declared again in dream's own
# pyproject because the settings module imports it directly
THIRD_PARTY = {"pydantic", "pydantic_settings"}
# parses the user's [dream] table, whose file format names the executor
SETTINGS_MODULE = "memriver_dream.settings"


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


def _os_alias(tree: ast.AST) -> str | None:
    """The local name a plain `import os` (or `import os as alias`) binds; None when
    `os` is not imported that way."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os":
                    return alias.asname or "os"
    return None


def _forbidden_os_calls(module: str) -> list[str]:
    """`os.<name>` calls that shell out or spawn another process."""
    tree = ast.parse(SOURCES[module].read_text(encoding="utf-8"))
    alias = _os_alias(tree)
    return [node.func.attr for node in ast.walk(tree)
           if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
           and isinstance(node.func.value, ast.Name) and node.func.value.id == alias
           and (node.func.attr in _FORBIDDEN_OS_CALLS
                or node.func.attr.startswith(("spawn", "exec")))]


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
            elif root not in THIRD_PARTY:
                pytest.fail(f"{module} imports {target}: memriver-dream adds no dependency")


def test_only_the_settings_module_imports_the_settings_libraries():
    for module in SOURCES:
        if module != SETTINGS_MODULE:
            roots = {target.split(".", 1)[0] for target in _imports(module)}
            assert not roots & THIRD_PARTY, f"{module} imports a settings library"


def test_dream_never_shells_out_or_spawns_a_process_via_os():
    for module in SOURCES:
        assert not _forbidden_os_calls(module), f"{module} calls os.<forbidden>"


@pytest.mark.parametrize("word", HARNESS_WORDS)
def test_dream_sources_name_no_harness(word):
    for module, path in SOURCES.items():
        if module == SETTINGS_MODULE:
            continue
        assert word not in path.read_text(encoding="utf-8").lower(), f"{module} names {word}"
