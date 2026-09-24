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

import pytest
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


def test_two_alternatives_each_with_their_own_flag_group_matches_both():
    # the curl-auth-header shape: two quoted alternatives, each opening with
    # its own (?i) inside a shared enclosing group. In RE2 a mid-pattern
    # flag's scope crosses any '|' at that same depth rather than resetting
    # per alternative, so naively wrapping straight through to the group's
    # close (folding the '|' inside the new group) would make the literal
    # quote before the first (?i) mandatory and the second alternative
    # unreachable. Ground truth, verified with Go's regexp (anchored):
    # (?:"(?i)a"|'b') matches 'B' -> true, and "A" -> true.
    translated = _re2_to_python(r"""(?:"(?i)a"|'b')""")
    compiled = re.compile(translated)
    assert compiled.fullmatch("'B'") is not None
    assert compiled.fullmatch('"A"') is not None


def test_flag_inside_one_alternative_does_not_leak_past_the_pipe_boundary():
    # ground truth, verified with Go's regexp (anchored):
    # (?:x(?i)a|b)c matches Bc -> true, xAC -> false -- the 'c' after the
    # group stays case-sensitive, and the flag opened in the first
    # alternative must not make the literal 'x' before it optional
    translated = _re2_to_python(r"(?:x(?i)a|b)c")
    compiled = re.compile(translated)
    assert compiled.fullmatch("Bc") is not None
    assert compiled.fullmatch("xAC") is None


def test_negative_only_mid_pattern_flag_group_is_translated():
    # ground truth: (?i)a(?-i)b matches Ab -> true, AB -> false -- (?-i) has
    # no leading positive flags, and must still be recognised as a flag
    # group (not left untranslated, which Python cannot compile)
    translated = _re2_to_python(r"(?i)a(?-i)b")
    compiled = re.compile(translated)
    assert compiled.fullmatch("Ab") is not None
    assert compiled.fullmatch("AB") is None


def test_negative_only_flag_group_after_a_mid_pattern_positive_one():
    # ground truth: x(?i)a(?-i)b fullmatch xAb -> true, xAB -> false
    translated = _re2_to_python(r"x(?i)a(?-i)b")
    compiled = re.compile(translated)
    assert compiled.fullmatch("xAb") is not None
    assert compiled.fullmatch("xAB") is None


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


def test_mapped_posix_class_translates_even_when_not_first_in_the_bracket():
    # the mapping check itself does not care about position; only the old
    # "leave it verbatim" fallback for an *unmapped* name relied on Python's
    # own start-of-bracket nested-set reading
    translated = _re2_to_python(r"[x[:alnum:]]+")
    assert translated == r"[xa-zA-Z0-9]+"
    assert re.compile(translated).fullmatch("xa1B") is not None


def test_unmapped_posix_class_at_bracket_start_raises():
    # not one of the eight named classes: raises so the rule is reported and
    # dropped, rather than silently compiling with Python's nested-set
    # reading of a leading '['
    with pytest.raises(re.error):
        _re2_to_python(r"[[:nonsense:]]")


def test_unmapped_posix_class_not_at_bracket_start_raises():
    # ground truth: RE2 accepts [x[:blank:]]+ (blank = space and tab, not one
    # of the eight names this translator supports) and Go's regexp matches
    # x/space/tab; left verbatim, Python reads '[' here as a literal
    # character (no nested-set warning fires away from the bracket's start)
    # and silently compiles a completely different, wrong class -- matching
    # 'b]', never x/space/tab. Must raise instead of loading unnoticed.
    with pytest.raises(re.error):
        _re2_to_python(r"[x[:blank:]]+")
    with pytest.raises(re.error):
        _re2_to_python(r"[x[:nonsense:]]")


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
