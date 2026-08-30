"""Ported wholesale from tests/test_gate.py and tests/test_gate_vendored.py.

`SecretScanner().check(text, max_chars)` is the ContentPolicy implementation
that replaces `gate.check_content`; `ContentRejected` replaces `GateError`.
`max_chars` has no default here (unlike the old `check_content`), so every
call below passes it explicitly -- `BODY_LIMIT` matches the value
`memriver_core.config.DEFAULT_MAX_BODY_CHARS` carries today.
"""
import logging
import re
import tomllib

import pytest
from memriver_core.application.errors import ContentRejected
from memriver_core.content_policy.secret_scanner import (
    _RULES,
    _RULES_DIR,
    SecretScanner,
    _load_rules,
    _shannon_entropy,
)

BODY_LIMIT = 8000

_SCANNER = SecretScanner()

# --- from test_gate.py ------------------------------------------------------

BLOCKED = [
    ("aws key AKIAIOSFODNN7EXAMPLE ok", "AKIAIOSFODNN7EXAMPLE"),
    # STS temporary access-key ids start with ASIA, not AKIA
    ("AWS_ACCESS_KEY_ID=ASIAIOSFODNN7EXAMPLE", "ASIAIOSFODNN7EXAMPLE"),
    ("token ghp_" + "a" * 36, "ghp_"),
    ("-----BEGIN RSA PRIVATE KEY-----\nabc", "PRIVATE KEY"),
    ("xoxb-123456789012-abcdefghijkl", "xoxb-"),
    ('api_key = "sk-abcdefghij1234567890"', "sk-abcdefghij1234567890"),
    # fine-grained PATs do not match gh[pousr]_ and are often written unquoted,
    # so neither the classic token rule nor the assignment rule catches them
    ("token github_pat_" + "a" * 60 + " end", "github_pat_"),
    # env-file and log style assignments carry no quotes at all
    ("API_KEY=abcdefghijklmnop", "abcdefghijklmnop"),
    ("password: correcthorsebattery", "correcthorsebattery"),
    # '_' is a word character, so a leading \b never matches inside the usual
    # env var names: the keyword is preceded by '_', not by a word boundary
    ("OPENAI_API_KEY=sk-proj-abcdef1234567890", "sk-proj-abcdef1234567890"),
    ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIabcdefgh", "wJalrXUtnFEMIabcdefgh"),
    # real passwords carry punctuation: a value class that stops at the first
    # '@' or '!' counts too few characters and lets the whole assignment pass
    ('PASSWORD="P@ssw0rd!234567"', "P@ssw0rd!234567"),
    ("DB_PASSWORD=P@ss:w0rd!23456", "P@ss:w0rd!23456"),
    # a quoted value may contain the *other* quote: one shared class for both
    # quote styles stops at that apostrophe, and the unquoted branch stops at
    # the first space, so the whole passphrase used to slip through
    ('PASSWORD="it\'s a very long secret phrase"', "it's a very long secret phrase"),
    ("PASSWORD='say \"friend\" and enter'", 'say "friend" and enter'),
    # bare OpenAI key in prose: no ':'/'=' right after a key name, so the
    # credential-assignment rule never matches it
    ("OpenAI key is sk-proj-abcdefghij1234567890xyz somewhere", "sk-proj-abcdefghij1234567890xyz"),
    # space-separated keyword/name variants: 'api key' and 'secret key' with a
    # literal space instead of '_'/'-' also need to be caught
    ("API KEY=abcdefghijklmnop", "abcdefghijklmnop"),
    ("Secret Key: correcthorsebattery12", "correcthorsebattery12"),
    # 'passphrase' and 'credential(s)' are common label variants the original
    # alternation missed entirely -- neither the vendored gitleaks generic-api-key
    # rule (entropy-gated, so a low-entropy value like this skips it) nor the
    # floor rule caught them
    ("PASSPHRASE=correcthorsebattery", "correcthorsebattery"),
    ("CREDENTIAL: correcthorsebattery", "correcthorsebattery"),
    # plural variant: 's' is not a separator char, so 'credential' alone would
    # leave a stray 's' between the keyword and ':' and fail to match
    ("CREDENTIALS: correcthorsebattery", "correcthorsebattery"),
    # case-insensitivity for the new keywords, matching the existing rule's (?i)
    ("passphrase: correcthorsebattery", "correcthorsebattery"),
    # the value branches' ~12-char minimum let explicitly labeled short
    # credentials through; the label alone already identifies these as
    # secrets, so the minimum was lowered (probed floor: 6)
    ("PASSWORD=Tr0ub4dor!", "Tr0ub4dor!"),
    ("TOKEN=abc123!xyz", "abc123!xyz"),
    ("passphrase=hunter22", "hunter22"),
]

@pytest.mark.parametrize("text,marker", BLOCKED)
def test_secrets_blocked(text, marker):
    # each case pins that its own secret material never appears in the message
    with pytest.raises(ContentRejected) as ei:
        _SCANNER.check(text, BODY_LIMIT)
    assert marker not in str(ei.value)

@pytest.mark.parametrize("text", ["", "   ", "\n\t \n"])
def test_empty_content_blocked(text):
    # an empty body would store a useless entry and break index rendering
    with pytest.raises(ContentRejected):
        _SCANNER.check(text, BODY_LIMIT)


def test_oversize_blocked():
    with pytest.raises(ContentRejected):
        _SCANNER.check("x" * 8001, BODY_LIMIT)

def test_normal_content_passes():
    _SCANNER.check("用户偏好：回复用中文；token 管理方式见 1Password 的 memriver 条目", BODY_LIMIT)


def test_credential_prose_without_assignment_passes():
    # 'credential' without an assignment shape (no ':'/'=' right after the
    # keyword) must not trip the extended alternation -- proves the fix widened
    # the keyword list, not the shape the rule matches
    _SCANNER.check("rotate the credential monthly", BODY_LIMIT)


@pytest.mark.parametrize("text", [
    "the password field is required",  # prose, no assignment shape at all
    "password=",  # empty value
    "password=***",  # 3-char placeholder, below the 6-char floor
    "port=8080",  # non-credential label
])
def test_short_value_negative_controls_pass(text):
    # lowering the value-length floor to catch short real secrets must not
    # make the rule prose- or placeholder-hostile at the low end
    _SCANNER.check(text, BODY_LIMIT)


def test_max_chars_is_tunable():
    # the caller passes a configured budget explicitly; no default lives here
    _SCANNER.check("x" * 20, max_chars=BODY_LIMIT)
    with pytest.raises(ContentRejected) as ei:
        _SCANNER.check("x" * 20, max_chars=10)
    assert "10" in str(ei.value)


# --- from test_gate_vendored.py ---------------------------------------------

# Every vector here is synthetic, but a contiguous provider prefix is enough to
# trip GitHub push protection on the way to origin, so the prefixes are spliced
# at import time instead of sitting in the file as one literal.
_STRIPE = "sk_" + "live_4eC39HqLyjWDarjtT1zdp7dc"

# Secrets the hand-rolled floor rules do not recognise: each one is a
# distinct vendored rule shape, and each assertion also pins that the secret
# itself never leaks back out through the error message.
VENDORED_BLOCKED = [
    # gcp-api-key
    ("key AIzaSyB3xQ7vN2mK9pLdR4tWzX8cVfH1jGnY0uQ end",
     "AIzaSyB3xQ7vN2mK9pLdR4tWzX8cVfH1jGnY0uQ"),
    # stripe-access-token: '_' after 'sk' keeps it out of the OpenAI rule
    (f"STRIPE={_STRIPE}", _STRIPE),
    # jwt
    (
        (
            "token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0."
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        ),
        "eyJzdWIiOiIxMjM0",
    ),
    # pypi-upload-token
    (
        (
            "pypi-AgEIcHlwaS5vcmcCJDAwMDAwMDAwLTAwMDAtMDAwMC0wMDAwLTAwMDAwMDAwMDAwMAAC"
            "KlszLCJmZmZmZmZmZi1mZmZmLWZmZmYtZmZmZi1mZmZmZmZmZmZmZmYiXQAABiCkeGhwYWNr"
            "YWdl"
        ),
        "AgEIcHlwaS5vcmcC",
    ),
]


@pytest.mark.parametrize("text,marker", VENDORED_BLOCKED)
def test_vendored_secrets_blocked(text, marker):
    with pytest.raises(ContentRejected) as ei:
        _SCANNER.check(text, BODY_LIMIT)
    assert marker not in str(ei.value)


def _blocked_by(text, marker, rule_id):
    with pytest.raises(ContentRejected) as ei:
        _SCANNER.check(text, BODY_LIMIT)
    assert marker not in str(ei.value)
    # the parenthesised form pins the exact id: 'x' would otherwise be satisfied
    # by a floor rule named 'memriver-x' that shadowed the vendored rule
    assert f"({rule_id})" in str(ei.value)


# A provider prefix followed by near-uniform filler: the shape of a credential
# with almost no entropy. Upstream gates every provider rule on entropy, so all
# of these cleared the vendored layer, and the first two -- shapes memriver has
# no floor rule for -- passed the gate outright. The [policy] table honours
# entropy only for the loose generic-api-key, so shape alone rejects them now.
# Each case also pins WHICH rule id rejected it: a floor rule quietly taking over
# a vendored rule's job is how the vendored layer rots unnoticed.
LOW_ENTROPY_BLOCKED = [
    ("key AIza" + "a" * 35 + " end", "AIza" + "a" * 35, "gcp-api-key"),
    ("STRIPE=" + "sk_" + "live_" + "a" * 24, "live_" + "a" * 24, "stripe-access-token"),
    ("token ghp_" + "a" * 36, "ghp_" + "a" * 36, "github-pat"),
    # all five gh*_ prefixes: the deleted memriver-github-token covered the whole
    # family, so a vendoring sync that drops one upstream rule must fail here
    ("oauth gho_" + "b" * 36, "gho_" + "b" * 36, "github-oauth"),
    ("token ghu_" + "a" * 36, "ghu_" + "a" * 36, "github-app-token"),
    ("token ghs_" + "a" * 36, "ghs_" + "a" * 36, "github-app-token"),
    ("token ghr_" + "a" * 36, "ghr_" + "a" * 36, "github-refresh-token"),
    ("aws key AKIA" + "A" * 16 + " noted", "AKIA" + "A" * 16, "aws-access-token"),
]
# Full-length low-entropy Slack and fine-grained-PAT shapes are covered by the
# de-entropy'd vendored rules too, but a floor rule still matches them first, so
# they are pinned in FLOOR_ONLY_BLOCKED below rather than here.


@pytest.mark.parametrize("text,marker,rule_id", LOW_ENTROPY_BLOCKED)
def test_low_entropy_provider_shapes_blocked(text, marker, rule_id):
    _blocked_by(text, marker, rule_id)


# The shapes the vendored layer still misses, entropy or not: these are why the
# matching floor rules survive the de-entropy'd vendored coverage. Each vector is
# a real-world truncation or spelling of a credential that upstream pins to one
# exact length or one exact infix.
FLOOR_ONLY_BLOCKED = [
    # upstream's github-fine-grained-pat demands exactly 82 body chars
    ("token github_pat_" + "a" * 60 + " end", "github_pat_" + "a" * 60,
     "memriver-github-fine-grained-pat"),
    # upstream's slack-bot-token demands two numeric segments
    ("xoxb-123456789012-abcdefghijkl", "xoxb-123456789012", "memriver-slack-token"),
    # upstream's openai-api-key demands the literal 'T3BlbkFJ' infix
    ("OpenAI key is sk-proj-abcdefghij1234567890xyz somewhere",
     "sk-proj-abcdefghij1234567890xyz", "memriver-openai-api-key"),
    # no vendored rule matches a PEM header on its own
    ("-----BEGIN RSA PRIVATE KEY-----\nabc", "PRIVATE KEY",
     "memriver-private-key-header"),
    # the credential-assignment heuristic: no provider shape at all
    ("password: correcthorsebattery", "correcthorsebattery",
     "memriver-credential-assignment"),
]


@pytest.mark.parametrize("text,marker,rule_id", FLOOR_ONLY_BLOCKED)
def test_floor_rules_cover_what_vendored_rules_miss(text, marker, rule_id):
    _blocked_by(text, marker, rule_id)


def test_shipped_policy_honours_entropy_only_for_generic_api_key():
    # the loaded ruleset is the contract: every other rule is enforced by shape
    assert [rid for rid, _p, ent, *_ in _RULES if ent is not None] == ["generic-api-key"]


# Memory bodies are prose, ULIDs and links; a vendored rule that fires on any of
# these has to be dealt with in secret_scanner.py, since gitleaks.toml is
# vendored verbatim.
PASSING = [
    (
        "用户偏好：回复用中文；runtime 用 mise 管理，python 包用 uv。gitleaks 的社区"
        "规则集 vendor 进 memriver-core。Slack 通知走 webhook，Notion 文档同步的"
        " api key 存在 1Password 里。Stripe 账单每月看一次，adobe reader 已安装，"
        "algolia 负责文档搜索，sentry 收集报错，postman collection 在仓库里。"
    ),
    (
        "Entry 01JZ8QK9X3M4N5P6R7S8T9VABC updated after "
        "01K3M7QW2E4R6T8Y0U1I3O5P7A and 01K3M7QW2E4R6T8Y0U1I3O5P7B creation "
        "at 2026-08-29T12:34:56Z."
    ),
    (
        "See https://github.com/gitleaks/gitleaks/blob/master/config/gitleaks.toml"
        "?utm_source=memriver&utm_campaign=abcdefghijklmnopqrstuvwxyz0123456789"
        "&ref=aHR0cHM6Ly9leGFtcGxlLmNvbS9sb25nL3BhdGgvdG8vc29tZXRoaW5n#section-4"
    ),
]


@pytest.mark.parametrize("text", PASSING)
def test_ordinary_memory_content_passes(text):
    _SCANNER.check(text, BODY_LIMIT)


def _raw(name):
    return tomllib.loads((_RULES_DIR / name).read_text(encoding="utf-8"))["rules"]


def test_vendored_rules_are_usable():
    # the vendored file is a real ruleset, and enough of it survives compilation
    # on this interpreter to be worth loading at all
    assert len(_raw("gitleaks.toml")) > 150
    assert len(_RULES) > 100


def test_memriver_floor_rules_all_load():
    # our own rules are hand-written for Python `re`: none may be skipped, and
    # they must come first so a floor rule wins over a same-named upstream one
    floor = _raw("memriver.toml")
    assert len(floor) == 5
    loaded = [r[0] for r in _RULES[:len(floor)]]
    assert loaded == [r["id"] for r in floor]
    assert all(rid.startswith("memriver-") for rid in loaded)


def test_rule_ids_are_unique():
    ids = [rule_id for rule_id, *_ in _RULES]
    assert len(ids) == len(set(ids))


def test_duplicate_ids_across_files_keep_the_first(tmp_path, caplog):
    # first file wins, so memriver.toml's floor rules cannot be displaced by an
    # upstream rule that happens to share an id
    (tmp_path / "a.toml").write_text('[[rules]]\nid = "dup"\nregex = "aaa"\n')
    (tmp_path / "b.toml").write_text('[[rules]]\nid = "dup"\nregex = "bbb"\n')
    with caplog.at_level(logging.DEBUG, logger="memriver_core.content_policy.secret_scanner"):
        rules = _load_rules(tmp_path / "a.toml", tmp_path / "b.toml")
    assert [(rid, pat.pattern) for rid, pat, *_ in rules] == [("dup", "aaa")]
    assert "dup" in caplog.text


def test_uncompilable_rule_is_skipped_not_fatal(tmp_path, caplog):
    # gitleaks patterns are RE2; some are invalid Python `re`, and which ones
    # varies by interpreter. One bad rule must not take the whole scanner down.
    (tmp_path / "rules.toml").write_text(
        '[[rules]]\nid = "bad-regex"\nregex = "(unclosed"\n\n'
        '[[rules]]\nid = "posix-class"\nregex = "[[:alnum:]]{10}"\n\n'
        '[[rules]]\nid = "path-only"\npath = "\\\\.pem$"\n\n'
        '[[rules]]\nid = "good"\nregex = "ZQ9[0-9]{4}"\n'
    )
    with caplog.at_level(logging.DEBUG, logger="memriver_core.content_policy.secret_scanner"):
        rules = _load_rules(tmp_path / "rules.toml")
    assert [rid for rid, *_ in rules] == ["good"]
    # each dropped rule is named, so a sync that loses coverage is diagnosable
    assert "bad-regex" in caplog.text
    # a POSIX class compiles in Python with a *different* meaning; it must be
    # rejected too, not silently mis-matched
    assert "posix-class" in caplog.text


# --- [policy] honor_entropy_only_for --------------------------------------

_SYNTHETIC = """
[[rules]]
id = "synthetic-key"
regex = '''ZQ9\\w+'''
entropy = 3.0
"""

# 'ZQ9aaaaaaaa' measures ~1.28 bits/char, comfortably under the rule's 3.0
_BELOW_THRESHOLD = "marker ZQ9aaaaaaaa end"


def _policy_rules(tmp_path, text):
    (tmp_path / "p.toml").write_text(text, encoding="utf-8")
    return _load_rules(tmp_path / "p.toml")


def test_entropy_honored_for_listed_rule(tmp_path, monkeypatch):
    rules = _policy_rules(
        tmp_path, '[policy]\nhonor_entropy_only_for = ["synthetic-key"]\n' + _SYNTHETIC)
    monkeypatch.setattr("memriver_core.content_policy.secret_scanner._RULES", rules)
    # the rule is listed, so its threshold still gates: below it, nothing happens
    _SCANNER.check(_BELOW_THRESHOLD, BODY_LIMIT)


def test_entropy_dropped_for_unlisted_rule(tmp_path, monkeypatch):
    rules = _policy_rules(
        tmp_path, '[policy]\nhonor_entropy_only_for = ["other-rule"]\n' + _SYNTHETIC)
    assert rules[0][2] is None
    monkeypatch.setattr("memriver_core.content_policy.secret_scanner._RULES", rules)
    # same body, same threshold: unlisted means shape alone rejects
    with pytest.raises(ContentRejected) as ei:
        _SCANNER.check(_BELOW_THRESHOLD, BODY_LIMIT)
    assert "(synthetic-key)" in str(ei.value)


def test_missing_policy_table_honors_every_entropy(tmp_path, monkeypatch):
    # a hand-trimmed rules file with no [policy] keeps upstream semantics
    rules = _policy_rules(tmp_path, _SYNTHETIC)
    assert rules[0][2] == 3.0
    monkeypatch.setattr("memriver_core.content_policy.secret_scanner._RULES", rules)
    _SCANNER.check(_BELOW_THRESHOLD, BODY_LIMIT)


def test_shannon_entropy():
    assert _shannon_entropy("") == 0.0
    assert _shannon_entropy("aaaa") == 0.0
    # 'aabb': two symbols at p=0.5 -> -2 * 0.5 * log2(0.5) = 1.0 bits/char
    assert _shannon_entropy("aabb") == pytest.approx(1.0)
    # 'abcd': four symbols at p=0.25 -> 2.0 bits/char
    assert _shannon_entropy("abcd") == pytest.approx(2.0)


# --- entropy and secret-group semantics ------------------------------------
#
# Real vendored rules cannot pin these branches: none of them lets the entropy
# of the whole match land on the opposite side of the threshold from the
# entropy of its capture group, which is exactly the distinction under test.
# So the rule list is swapped for synthetic rules whose numbers are known:
# 'aaaaaaaa' has entropy 0.0, 'abcdefgh' has 3.0, and 'ZQ-aaaaaaaa' -- the
# whole match in the group cases -- has ~1.28. A threshold of 1.0 therefore
# separates "measured the group" from "measured the whole match".

def _with_rules(monkeypatch, *rules):
    monkeypatch.setattr(
        "memriver_core.content_policy.secret_scanner._RULES",
        [(rid, re.compile(p), ent, grp, ()) for rid, p, ent, grp in rules],
    )


def test_entropy_below_threshold_passes(monkeypatch):
    _with_rules(monkeypatch, ("low-entropy", r"aaaaaaaa", 1.0, 0))
    _SCANNER.check("release notes for aaaaaaaa builds", BODY_LIMIT)


def test_entropy_above_threshold_rejects(monkeypatch):
    _with_rules(monkeypatch, ("high-entropy", r"ZQ9[A-Za-z0-9]+", 3.0, 0))
    with pytest.raises(ContentRejected) as ei:
        _SCANNER.check("value ZQ9abcdefgh here", BODY_LIMIT)
    assert "high-entropy" in str(ei.value)
    assert "ZQ9abcdefgh" not in str(ei.value)


def test_entropy_uses_declared_secret_group(monkeypatch):
    # secretGroup=2 selects 'aaaaaaaa' (0.0); group 1 or the whole match would
    # both clear the threshold, so a pass proves group 2 was the one measured
    _with_rules(monkeypatch, ("grouped", r"ZQ-(\w+)-(\w+)", 1.0, 2))
    _SCANNER.check("marker ZQ-abcdefgh-aaaaaaaa end", BODY_LIMIT)


def test_declared_secret_group_still_rejects_a_real_secret(monkeypatch):
    # same rule, operands swapped: group 2 is now 'abcdefgh' (3.0)
    _with_rules(monkeypatch, ("grouped", r"ZQ-(\w+)-(\w+)", 1.0, 2))
    with pytest.raises(ContentRejected) as ei:
        _SCANNER.check("marker ZQ-aaaaaaaa-abcdefgh end", BODY_LIMIT)
    assert "grouped" in str(ei.value)


def test_entropy_prefers_group_one_when_no_secret_group(monkeypatch):
    # no secretGroup: group 1 is 'aaaaaaaa' (0.0) but the whole match
    # 'ZQ-aaaaaaaa' is ~1.28, so measuring the whole match would reject
    _with_rules(monkeypatch, ("ungrouped", r"ZQ-(\w+)", 1.0, 0))
    _SCANNER.check("marker ZQ-aaaaaaaa end", BODY_LIMIT)


def test_entropy_falls_back_to_whole_match_without_groups(monkeypatch):
    _with_rules(monkeypatch, ("no-groups", r"ZQ-\w+", 1.0, 0))
    with pytest.raises(ContentRejected):
        _SCANNER.check("marker ZQ-aaaaaaaa end", BODY_LIMIT)
