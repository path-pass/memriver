"""`_re2_to_python` translation, and the coverage guarantee it exists for.

`_load_rules` used to hand gitleaks' RE2 patterns straight to `re.compile`
and silently drop whatever Python could not read. On Python 3.12.11 that
dropped 26 vendored rules -- whole credential families such as Linear API
keys, Airtable PATs and SendGrid tokens went unscanned. `_re2_to_python`
translates the three RE2-only constructs behind those failures before
`re.compile` ever sees the pattern.
"""
from __future__ import annotations

import re
import tomllib

from memriver_core.content_policy.rules_loader import _load_rules, _re2_to_python
from memriver_core.content_policy.secret_scanner import _RULES, _RULES_DIR


def _raw(name):
    return tomllib.loads((_RULES_DIR / name).read_text(encoding="utf-8"))["rules"]


# --- _re2_to_python: unit tests ---------------------------------------------

def test_pattern_with_none_of_the_constructs_is_returned_unchanged():
    pattern = r"foo[a-z]{3}bar\d+"
    assert _re2_to_python(pattern) == pattern


def test_leading_flag_group_at_position_zero_is_left_alone():
    pattern = r"(?i)abc"
    assert _re2_to_python(pattern) == pattern


def test_top_level_mid_pattern_flag_scopes_to_end_of_pattern():
    translated = _re2_to_python(r"a(?i)b")
    compiled = re.compile(translated)
    assert compiled.fullmatch("aB") is not None
    assert compiled.fullmatch("Ab") is None


def test_mid_pattern_flag_inside_a_group_scopes_to_that_groups_close():
    # the flag must not leak past the enclosing group's own ')'
    translated = _re2_to_python(r"(x(?i)y)z")
    compiled = re.compile(translated)
    assert compiled.fullmatch("xYz") is not None
    assert compiled.fullmatch("xyZ") is None


def test_two_alternatives_each_with_their_own_flag_group_compiles():
    # the curl-auth-header shape: two quoted alternatives, each opening with
    # its own (?i) inside a shared enclosing group. Each occurrence gets its
    # own closing paren (both close together, right before the shared
    # group's own close), and the pattern compiles and matches the first,
    # reachable alternative case-insensitively.
    translated = _re2_to_python(r"""(?:"(?i)(?:A)"|'(?i)(?:A)')""")
    compiled = re.compile(translated)
    assert compiled.fullmatch('"a"') is not None


def test_redundant_inner_flag_group_becomes_a_scoped_group():
    # planetscale-password, verbatim: leading (?i) stays, the inner
    # redundant one becomes a scoped group
    pattern = r"(?i)\b(pscale_pw_(?i)[\w=\.-]{32,64})(?:[\x60'\"\s;]|\\[nr]|$)"
    translated = _re2_to_python(pattern)
    compiled = re.compile(translated)
    assert compiled.search("pscale_pw_" + "a1" * 16) is not None


def test_z_outside_bracket_becomes_capital_z():
    translated = _re2_to_python(r"foo\z")
    assert translated == r"foo\Z"
    assert re.compile(translated).fullmatch("foo") is not None


def test_escaped_backslash_followed_by_z_is_not_mistaken_for_the_anchor():
    # \\z is an escaped backslash followed by a literal 'z', not \z -- the
    # scanner must consume the doubled backslash as one escape unit
    translated = _re2_to_python(r"foo\\z")
    assert translated == r"foo\\z"


def test_posix_alnum_class_inside_brackets_expands_to_ascii_ranges():
    translated = _re2_to_python(r"[[:alnum:]]{3}")
    assert translated == r"[a-zA-Z0-9]{3}"
    assert re.compile(translated).fullmatch("a1B") is not None


def test_unknown_posix_class_name_is_left_alone():
    # not one of the eight named classes: left as-is, so compilation still
    # fails downstream and the rule is reported, not silently mismatched
    pattern = r"[[:nonsense:]]"
    assert _re2_to_python(pattern) == pattern


def test_airtable_pattern_verbatim_translates_and_matches():
    pattern = r"\b(pat[[:alnum:]]{14}\.[a-f0-9]{64})\b"
    translated = _re2_to_python(pattern)
    compiled = re.compile(translated)
    secret = "pat" + "a1" * 7 + "." + "0123456789abcdef" * 4
    assert compiled.search(f"token {secret} end") is not None


# --- coverage: every regex-bearing rule loads on this interpreter ----------

def test_every_regex_bearing_rule_loads_on_this_interpreter():
    # replaces the old "count > 100" guard: the real contract is that nothing
    # vendored is silently dropped, not merely that "enough" survives
    expected_ids = []
    seen = set()
    for name in ("memriver.toml", "gitleaks.toml"):
        for rule in _raw(name):
            if rule.get("regex") and rule["id"] not in seen:
                seen.add(rule["id"])
                expected_ids.append(rule["id"])
    loaded_ids = {rid for rid, *_ in _RULES}
    missing = [rid for rid in expected_ids if rid not in loaded_ids]
    assert missing == []


def test_load_rules_still_returns_a_large_ruleset():
    # kept as a sanity floor; the coverage test above is the real guard now
    assert len(_load_rules(_RULES_DIR / "memriver.toml", _RULES_DIR / "gitleaks.toml")) > 100
