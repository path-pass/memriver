"""memriver_classifier.headless keeps a copy of the umbrella's headless harness
runner (memriver.executors): the process runner, the environment isolation, the
Codex provider-override handling, and the fixed argv memriver dream's own copy
builds. Neither package may import the other (memriver-classifier must not import
memriver, and memriver dream is required while the classifier is optional), so this
is a source copy, not a shared import; nothing else keeps the two copies in step.
This test compares them directly, the way tools/test_codex_whitelist_copies.py does
for the Codex provider-override whitelist.

tools/ is not covered by either package's own architecture test, so it is free to
import both memriver_classifier and memriver.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import memriver.executors as dream_executors
import memriver_classifier.headless as classifier_headless


def _without_docstring(function_def: ast.FunctionDef) -> ast.FunctionDef:
    body = function_def.body
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        function_def.body = body[1:]
    return function_def


def _dump_source(source: str) -> str:
    """AST of one function's source, its docstring (if any) stripped."""
    tree = ast.parse(textwrap.dedent(source))
    return ast.dump(_without_docstring(tree.body[0]))


def _dump(func: object) -> str:
    return _dump_source(inspect.getsource(func))


def test_the_codex_feature_whitelist_is_copied_identically():
    assert classifier_headless._CODEX_FEATURES_OFF == dream_executors._CODEX_FEATURES_OFF


def test_the_login_and_quota_patterns_are_copied_identically():
    assert classifier_headless._LOGIN.pattern == dream_executors._LOGIN.pattern
    assert classifier_headless._LOGIN.flags == dream_executors._LOGIN.flags
    assert classifier_headless._QUOTA.pattern == dream_executors._QUOTA.pattern
    assert classifier_headless._QUOTA.flags == dream_executors._QUOTA.flags


def test_isolated_env_is_copied_identically():
    assert _dump(classifier_headless.isolated_env) == _dump(dream_executors.isolated_env)


def test_override_args_is_copied_identically():
    assert _dump(classifier_headless.override_args) == _dump(dream_executors.override_args)


def test_missing_env_is_copied_identically():
    assert _dump(classifier_headless.missing_env) == _dump(dream_executors.missing_env)


def test_codex_error_extraction_is_copied_identically():
    assert _dump(classifier_headless._codex_errors) == _dump(dream_executors._codex_errors)


def test_run_process_is_copied_identically_but_for_the_grace_constant_name():
    # the classifier's own kill-grace constant is KILL_GRACE_S; the umbrella's is
    # DREAM_KILL_GRACE_S -- both name the same setting, spelled for their own
    # package -- so the name is normalised before comparing the rest of the body,
    # which is where an isolation drift would actually land.
    classifier_source = textwrap.dedent(inspect.getsource(classifier_headless.run_process))
    dream_source = textwrap.dedent(inspect.getsource(dream_executors.run_process))
    normalised_dream_source = dream_source.replace("DREAM_KILL_GRACE_S", "KILL_GRACE_S")
    assert _dump_source(classifier_source) == _dump_source(normalised_dream_source)


def test_claude_argv_matches_the_dream_executor_for_the_same_inputs():
    system_prompt, schema = "check this note", {"type": "object"}
    classifier_argv = classifier_headless.claude_argv(
        "claude", system_prompt=system_prompt, schema=schema, model=None,
        settings_path=None)
    dream_argv = dream_executors.ClaudeExecutor("claude", env={}).argv(
        system_prompt=system_prompt, schema=schema)
    assert classifier_argv == dream_argv


def test_codex_argv_matches_the_dream_executor_for_the_same_inputs():
    files = Path("/scratch/files")
    overrides = {"model_provider": "azure", "model_providers.azure.env_key": "AZURE_KEY"}
    classifier_argv = classifier_headless.codex_argv(
        "codex", files=files, model=None, overrides=overrides)
    dream_argv = dream_executors.CodexExecutor("codex", env={}, overrides=overrides).argv(
        files=files)
    assert classifier_argv == dream_argv
