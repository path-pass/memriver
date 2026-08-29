import pytest
from memriver_core.gate import GateError, check_content

BLOCKED = [
    "aws key AKIAIOSFODNN7EXAMPLE ok",
    "token ghp_" + "a" * 36,
    "-----BEGIN RSA PRIVATE KEY-----\nabc",
    "xoxb-123456789012-abcdefghijkl",
    'api_key = "sk-abcdefghij1234567890"',
]

@pytest.mark.parametrize("text", BLOCKED)
def test_secrets_blocked(text):
    with pytest.raises(GateError) as ei:
        check_content(text)
    assert "AKIA" not in str(ei.value) and "ghp_" not in str(ei.value)

def test_oversize_blocked():
    with pytest.raises(GateError):
        check_content("x" * 8001)

def test_normal_content_passes():
    check_content("用户偏好：回复用中文；token 管理方式见 1Password 的 memriver 条目")
