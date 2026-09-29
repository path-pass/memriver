"""memriver_classifier.settings keeps a copy of memriver_dream.settings' Codex
provider-override whitelist (memriver_classifier may not import memriver_dream, so
this is a source copy, not a shared import). Nothing but a comment on each side keeps
the two copies equal, so this test compares them directly: the two functions' AST
(ast.dump ignores comments, which is the only place the two copies are allowed to
differ) and the whitelist's supporting constants and regex patterns.

tools/ is not covered by either package's own architecture test, so it is free to
import both memriver_classifier and memriver_dream.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import memriver_classifier.settings as classifier_settings
import memriver_dream.settings as dream_settings


def _dump(func: object) -> str:
    source = textwrap.dedent(inspect.getsource(func))
    return ast.dump(ast.parse(source))


def test_check_codex_overrides_is_copied_identically():
    assert (_dump(classifier_settings.check_codex_overrides)
            == _dump(dream_settings.check_codex_overrides))


def test_plain_url_is_copied_identically():
    assert _dump(classifier_settings._plain_url) == _dump(dream_settings._plain_url)


def test_the_whitelist_constants_are_copied_identically():
    assert classifier_settings._CODEX_TOP_KEYS == dream_settings._CODEX_TOP_KEYS
    assert (classifier_settings._CODEX_PROVIDER_KEY_RE.pattern
            == dream_settings._CODEX_PROVIDER_KEY_RE.pattern)
    assert (classifier_settings._PLAIN_KEY_RE.pattern
            == dream_settings._PLAIN_KEY_RE.pattern)
    assert (classifier_settings._ENV_NAME_RE.pattern
            == dream_settings._ENV_NAME_RE.pattern)
