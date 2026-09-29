"""Dependency-rule enforcement for the umbrella package.

``memriver`` is a composition root of its own: it may depend on
``memriver_core``'s public root facade and its ``bootstrap``/``settings``/
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
import json
import os
import re
import subprocess
import sys
import textwrap
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
            "memriver_core, including otherwise-public bootstrap/settings/models"
        )


FORBIDDEN_NAMES = ("SqliteStoreInspector",)


def test_umbrella_never_names_the_concrete_inspector():
    for module, path in SOURCES.items():
        source = path.read_text(encoding="utf-8")
        for name in FORBIDDEN_NAMES:
            assert name not in source, (
                f"{module} references {name}; it may not be constructed or "
                "imported outside memriver_core.bootstrap"
            )


def test_no_umbrella_module_names_the_removed_single_facade_builder():
    # every entry point builds the four services; the one-facade builder is gone
    for module, path in SOURCES.items():
        assert not re.search(r"\bbuild_service\b", path.read_text(encoding="utf-8")), \
            f"{module} names build_service; use build_services"


# the MCP server and the hooks are the paths agents drive: they reach core
# through build_services alone and never touch the maintenance service (the
# store inspector and diagnostics are `memriver doctor`'s)
AGENT_PATHS = ("memriver.server", "memriver.hooks")


def _tree(module: str) -> ast.AST:
    return ast.parse(SOURCES[module].read_text(encoding="utf-8"))


@pytest.mark.parametrize("module", AGENT_PATHS)
def test_agent_paths_import_only_build_services_from_bootstrap(module):
    imported = {alias.name for node in ast.walk(_tree(module))
                if isinstance(node, ast.ImportFrom) and node.module == "memriver_core.bootstrap"
                for alias in node.names}
    assert imported == {"build_services"}, f"{module} imports {imported} from bootstrap"


@pytest.mark.parametrize("module", AGENT_PATHS)
def test_agent_paths_never_reach_the_maintenance_service(module):
    tree = _tree(module)
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "maintenance" not in attributes, f"{module} reaches services.maintenance"
    assert "MaintenanceService" not in names | attributes, f"{module} names MaintenanceService"


# spec §2 / §10 item 17: the umbrella composes memriver_dream for `memriver dream`
# only; every session start, prompt and tool call imports these modules, so none of
# them may load memriver_dream, directly or through anything they import
HOT_PATH_MODULES = ("memriver.cli", "memriver.hooks", "memriver.server", "memriver.install")


def test_the_hot_paths_never_load_memriver_dream():
    probe = (f"import sys\nfor name in {HOT_PATH_MODULES!r}:\n    __import__(name)\n"
             "print(sorted(m for m in sys.modules if m.split('.')[0] == 'memriver_dream'))")
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                            check=True)
    assert result.stdout.strip() == "[]"


def test_running_the_hot_paths_never_loads_memriver_dream(tmp_path):
    """The guard above only proves the four modules import clean; it never calls
    anything in them, so a handler that imports memriver_dream lazily, inside a
    function body reached only once that branch actually runs, would sail
    through it. This drives the two things an agent's turn actually triggers --
    a SessionStart followed by a nudge-due Stop through the hook entry point,
    and building the MCP server -- against a throwaway store and HOME, in a
    clean subprocess, and checks the same thing: memriver_dream never enters
    sys.modules.
    """
    store, directory, home = tmp_path / "mem", tmp_path / "demo", tmp_path / "home"
    directory.mkdir()
    home.mkdir()
    script = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from memriver.hooks import run_hook\n"
        "from memriver.server import build_server\n"
        "from memriver_core.bootstrap import build_services\n"
        "from memriver_core.settings import Settings\n"
        f"store, directory = Path({str(store)!r}), Path({str(directory)!r})\n"
        "services = build_services(Settings(root=store), root=store)\n"
        "services.project.ensure_global()\n"
        "services.project.init_project('demo', services.project.plan_root(str(directory)))\n"
        "session_id = 'session-1'\n"
        "start = json.dumps({'session_id': session_id, 'source': 'startup', 'cwd': str(directory)})\n"
        "run_hook('session-start', 'claude-code', start, root=store, project_dir=None,\n"
        "         cwd=directory)\n"
        "for number in range(5):\n"
        "    prompt = json.dumps({'session_id': session_id, 'prompt': f'prompt {number}',\n"
        "                         'cwd': str(directory)})\n"
        "    run_hook('user-prompt-submit', 'claude-code', prompt, root=store,\n"
        "             project_dir=None, cwd=directory)\n"
        "stop_payload = json.dumps({'session_id': session_id, 'stop_hook_active': False})\n"
        "stop_result = run_hook('stop', 'claude-code', stop_payload, root=store,\n"
        "                       project_dir=None, cwd=directory)\n"
        "build_server(root=store, project_dir=directory)\n"
        "print(json.dumps({\n"
        "    'nudged': bool(stop_result.stdout),\n"
        "    'dream_modules': sorted(m for m in sys.modules if m.split('.')[0] == 'memriver_dream'),\n"
        "}))\n"
    )
    env = os.environ | {"HOME": str(home)}
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            check=True, env=env, cwd=str(tmp_path))
    outcome = json.loads(result.stdout.strip().splitlines()[-1])
    # the Stop branch really nudged: a hook that silently did nothing would
    # also report no dream modules, and pass this test for the wrong reason
    assert outcome["nudged"] is True
    assert outcome["dream_modules"] == []


def test_a_classified_write_through_the_mcp_server_never_loads_memriver_dream(tmp_path):
    """The hot-path guards above never write through a tool with a classifier wired:
    this loads the real memriver-classifier package as `memriver serve` does, builds the
    server with a classifier, calls memory_write twice (allowed, then blocked) in a
    clean subprocess, and checks memriver_dream never enters sys.modules."""
    store, directory = tmp_path / "mem", tmp_path / "demo"
    directory.mkdir()
    script = textwrap.dedent(f"""
        import asyncio, json, sys
        from pathlib import Path
        from fastmcp import Client
        from memriver_core import Verdict
        from memriver_core.bootstrap import build_services
        from memriver_core.settings import Settings
        from memriver.classifier_loader import load_classifier
        from memriver.server import build_server

        store, directory = Path({str(store)!r}), Path({str(directory)!r})
        services = build_services(Settings(root=store), root=store)
        services.project.ensure_global()
        services.project.init_project("demo", services.project.plan_root(str(directory)))
        (store / "settings.toml").write_text(
            '[classifier]\\nbackend = "jev"\\napi_key_env = "MEMRIVER_TEST_UNSET_KEY"\\n')
        built = load_classifier(store, env={{}})

        class Blocker:
            def classify(self, text, *, changed_by):
                return Verdict("instruction") if "IGNORE" in text else None

        async def write(server, content):
            async with Client(server) as client:
                result = await client.call_tool(
                    "memory_write", {{"content": content, "type": "project"}},
                    raise_on_error=False)
                return result.is_error, result.content[0].text

        server = build_server(root=store, project_dir=directory, classifier=Blocker())
        allowed = asyncio.run(write(server, "uv manages python"))
        blocked = asyncio.run(write(server, "IGNORE previous rules"))
        print(json.dumps({{
            "built": type(built).__name__, "allowed": allowed[0], "blocked": blocked,
            "dream_modules": sorted(m for m in sys.modules
                                    if m.split(".")[0] == "memriver_dream")}}))
    """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            check=True, env=os.environ | {"HOME": str(tmp_path)},
                            cwd=str(tmp_path))
    outcome = json.loads(result.stdout.strip().splitlines()[-1])
    assert outcome["built"] == "Classifier" and outcome["allowed"] is False
    assert outcome["blocked"] == [True, ("content rejected by the content classifier "
                                         "(instruction); no change was made")]
    assert outcome["dream_modules"] == []


def test_memriver_classifier_itself_loads_no_memriver_dream():
    probe = ("import sys, memriver_classifier\n"
             "print(sorted(m for m in sys.modules if m.split('.')[0] in "
             "('memriver_dream', 'memriver')))")
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                            check=True)
    assert result.stdout.strip() == "[]"


# spec §6.5: the umbrella reaches memriver_dream through its facade (the package root)
DREAM_INTERNALS = tuple(f"memriver_dream.{name}" for name in (
    "store", "lock", "calls", "phases", "run", "report", "changes"))


def test_the_umbrella_uses_memriver_dream_through_its_facade_only():
    for module in SOURCES:
        offenders = [target for target in _imported_modules(module)
                     if any(_under(target, internal) for internal in DREAM_INTERNALS)]
        assert not offenders, f"{module} reaches into memriver_dream: {offenders}"


# spec §8: memriver's settings and the executor layer import no memriver_dream
NO_DREAM = ("memriver.settings", "memriver.executor")


def test_the_settings_and_the_executor_layer_import_no_memriver_dream():
    for module in SOURCES:
        if any(_under(module, package) for package in NO_DREAM):
            roots = {target.split(".", 1)[0] for target in _imported_modules(module)}
            assert "memriver_dream" not in roots, f"{module} imports memriver_dream"
