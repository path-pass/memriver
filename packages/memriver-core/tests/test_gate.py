import pytest
from memriver_core.gate import GateError, check_content

BLOCKED = [
    ("aws key AKIAIOSFODNN7EXAMPLE ok", "AKIAIOSFODNN7EXAMPLE"),
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
]

@pytest.mark.parametrize("text,marker", BLOCKED)
def test_secrets_blocked(text, marker):
    # each case pins that its own secret material never appears in the message
    with pytest.raises(GateError) as ei:
        check_content(text)
    assert marker not in str(ei.value)

@pytest.mark.parametrize("text", ["", "   ", "\n\t \n"])
def test_empty_content_blocked(text):
    # an empty body would store a useless entry and break index rendering
    with pytest.raises(GateError):
        check_content(text)


def test_oversize_blocked():
    with pytest.raises(GateError):
        check_content("x" * 8001)

def test_normal_content_passes():
    check_content("用户偏好：回复用中文；token 管理方式见 1Password 的 memriver 条目")
