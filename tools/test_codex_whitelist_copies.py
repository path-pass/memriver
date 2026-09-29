"""memriver_classifier.settings keeps a copy of the Codex provider-override whitelist
that memriver.settings now holds. This test keeps the copy equal to it: the two
functions' AST (ast.dump ignores comments, the only place the copies may differ) and
the whitelist's supporting constants and patterns.

tools/ is not covered by any package's own architecture test, so it is free to import
both memriver_classifier and memriver.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import memriver.settings as memriver_settings
import memriver_classifier.settings as classifier_settings


def _dump(func: object) -> str:
    return ast.dump(ast.parse(textwrap.dedent(inspect.getsource(func))))


def test_check_codex_overrides_is_copied_identically():
    assert (_dump(classifier_settings.check_codex_overrides)
            == _dump(memriver_settings.check_codex_overrides))


def test_plain_url_is_copied_identically():
    assert _dump(classifier_settings._plain_url) == _dump(memriver_settings._plain_url)


def test_the_whitelist_constants_are_copied_identically():
    assert classifier_settings._CODEX_TOP_KEYS == memriver_settings._CODEX_TOP_KEYS
    assert (classifier_settings._CODEX_PROVIDER_KEY_RE.pattern
            == memriver_settings._CODEX_PROVIDER_KEY_RE.pattern)
    assert classifier_settings._PLAIN_KEY_RE.pattern == memriver_settings._PLAIN_KEY_RE.pattern
    assert classifier_settings._ENV_NAME_RE.pattern == memriver_settings._ENV_NAME_RE.pattern
