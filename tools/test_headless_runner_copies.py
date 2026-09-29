"""memriver_classifier.headless keeps a copy of the headless harness runner that
memriver.executor.harness now holds: the process runner, the environment isolation,
the Codex provider-override handling and the fixed argv. This test keeps the copy
equal to it until the classifier runs on the executor layer too.

tools/ is not covered by any package's own architecture test, so it is free to import
both memriver_classifier and memriver.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import memriver_classifier.headless as classifier_headless
from memriver.executor import harness


def _without_docstring(function_def: ast.FunctionDef) -> ast.FunctionDef:
    body = function_def.body
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        function_def.body = body[1:]
    return function_def


def _dump(func: object) -> str:
    """AST of one function's source, its docstring (if any) stripped."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return ast.dump(_without_docstring(tree.body[0]))


def test_the_codex_feature_whitelist_is_copied_identically():
    assert classifier_headless._CODEX_FEATURES_OFF == harness._CODEX_FEATURES_OFF


def test_the_login_and_quota_patterns_are_copied_identically():
    assert classifier_headless._LOGIN.pattern == harness._LOGIN.pattern
    assert classifier_headless._LOGIN.flags == harness._LOGIN.flags
    assert classifier_headless._QUOTA.pattern == harness._QUOTA.pattern
    assert classifier_headless._QUOTA.flags == harness._QUOTA.flags


def test_the_helpers_are_copied_identically():
    for name in ("isolated_env", "override_args", "missing_env", "_codex_errors",
                 "run_process"):
        assert _dump(getattr(classifier_headless, name)) == _dump(getattr(harness, name)), name


def test_the_argv_matches_for_the_same_inputs():
    system_prompt, schema = "check this note", {"type": "object"}
    assert classifier_headless.claude_argv(
        "claude", system_prompt=system_prompt, schema=schema, model="haiku",
        settings_path="/etc/auth.json") == harness.claude_argv(
        "claude", system_prompt=system_prompt, schema=schema, model="haiku",
        settings_path="/etc/auth.json")
    files = Path("/scratch/files")
    overrides = {"model_provider": "azure", "model_providers.azure.env_key": "AZURE_KEY"}
    assert classifier_headless.codex_argv("codex", files=files, model="m",
                                          overrides=overrides) == \
        harness.codex_argv("codex", files=files, model="m", overrides=overrides)
