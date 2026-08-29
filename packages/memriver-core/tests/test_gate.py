import pytest
from memriver_core.gate import GateError, check_content

BLOCKED = [
    ("aws key AKIAIOSFODNN7EXAMPLE ok", "AKIAIOSFODNN7EXAMPLE"),
    ("token ghp_" + "a" * 36, "ghp_"),
    ("-----BEGIN RSA PRIVATE KEY-----\nabc", "PRIVATE KEY"),
    ("xoxb-123456789012-abcdefghijkl", "xoxb-"),
    ('api_key = "sk-abcdefghij1234567890"', "sk-abcdefghij1234567890"),
]

@pytest.mark.parametrize("text,marker", BLOCKED)
def test_secrets_blocked(text, marker):
    # each case pins that its own secret material never appears in the message
    with pytest.raises(GateError) as ei:
        check_content(text)
    assert marker not in str(ei.value)

def test_oversize_blocked():
    with pytest.raises(GateError):
        check_content("x" * 8001)

def test_normal_content_passes():
    check_content("用户偏好：回复用中文；token 管理方式见 1Password 的 memriver 条目")
