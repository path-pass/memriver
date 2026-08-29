import re

import pytest
from memriver_core.gate import GateError, check_content
from memriver_core.gate_rules import VENDORED_RULES

# Every vector here is synthetic, but a contiguous provider prefix is enough to
# trip GitHub push protection on the way to origin, so the prefixes are spliced
# at import time instead of sitting in the file as one literal.
_STRIPE = "sk_" + "live_4eC39HqLyjWDarjtT1zdp7dc"

# Secrets the hand-rolled floor in gate.py does not recognise: each one is a
# distinct vendored rule shape, and each assertion also pins that the secret
# itself never leaks back out through the error message.
VENDORED_BLOCKED = [
    # gcp-api-key
    ("key AIzaSyB3xQ7vN2mK9pLdR4tWzX8cVfH1jGnY0uQ end",
     "AIzaSyB3xQ7vN2mK9pLdR4tWzX8cVfH1jGnY0uQ"),
    # stripe-access-token: '_' after 'sk' keeps it out of the OpenAI rule
    (f"STRIPE={_STRIPE}", _STRIPE),
    # jwt
    ("token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
     "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0."
     "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U", "eyJzdWIiOiIxMjM0"),
    # pypi-upload-token
    ("pypi-AgEIcHlwaS5vcmcCJDAwMDAwMDAwLTAwMDAtMDAwMC0wMDAwLTAwMDAwMDAwMDAwMAAC"
     "KlszLCJmZmZmZmZmZi1mZmZmLWZmZmYtZmZmZi1mZmZmZmZmZmZmZmYiXQAABiCkeGhwYWNr"
     "YWdl", "AgEIcHlwaS5vcmcC"),
]


@pytest.mark.parametrize("text,marker", VENDORED_BLOCKED)
def test_vendored_secrets_blocked(text, marker):
    with pytest.raises(GateError) as ei:
        check_content(text)
    assert marker not in str(ei.value)


# Memory bodies are prose, ULIDs and links; a vendored rule that fires on any of
# these belongs in EXCLUDED_RULE_IDS in tools/sync_gitleaks_rules.py.
PASSING = [
    "用户偏好：回复用中文；runtime 用 mise 管理，python 包用 uv。gitleaks 的社区"
    "规则集 vendor 进 memriver-core。Slack 通知走 webhook，Notion 文档同步的"
    " api key 存在 1Password 里。Stripe 账单每月看一次，adobe reader 已安装，"
    "algolia 负责文档搜索，sentry 收集报错，postman collection 在仓库里。",
    "Entry 01JZ8QK9X3M4N5P6R7S8T9VABC superseded 01JZ8QK9X3M4N5P6R7S8T9VXYZ at "
    "2026-08-29T12:34:56Z; 01K3M7QW2E4R6T8Y0U1I3O5P7A and "
    "01K3M7QW2E4R6T8Y0U1I3O5P7B were created 2026-08-28T00:00:00+08:00.",
    "See https://github.com/gitleaks/gitleaks/blob/master/config/gitleaks.toml"
    "?utm_source=memriver&utm_campaign=abcdefghijklmnopqrstuvwxyz0123456789"
    "&ref=aHR0cHM6Ly9leGFtcGxlLmNvbS9sb25nL3BhdGgvdG8vc29tZXRoaW5n#section-4",
]


@pytest.mark.parametrize("text", PASSING)
def test_ordinary_memory_content_passes(text):
    check_content(text)


def test_vendored_rules_are_usable():
    assert len(VENDORED_RULES) > 100
    for rule_id, pattern, *_ in VENDORED_RULES:
        re.compile(pattern)  # raises if the generated file is corrupt


def test_shannon_entropy():
    from memriver_core.gate import _shannon_entropy

    assert _shannon_entropy("") == 0.0
    assert _shannon_entropy("aaaa") == 0.0
    # 'aabb': two symbols at p=0.5 -> -2 * 0.5 * log2(0.5) = 1.0 bits/char
    assert _shannon_entropy("aabb") == pytest.approx(1.0)
    # 'abcd': four symbols at p=0.25 -> 2.0 bits/char
    assert _shannon_entropy("abcd") == pytest.approx(2.0)
