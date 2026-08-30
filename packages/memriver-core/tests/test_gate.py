import pytest
from memriver_core.gate import GateError, check_content

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


def test_credential_prose_without_assignment_passes():
    # 'credential' without an assignment shape (no ':'/'=' right after the
    # keyword) must not trip the extended alternation -- proves the fix widened
    # the keyword list, not the shape the rule matches
    check_content("rotate the credential monthly")


@pytest.mark.parametrize("text", [
    "the password field is required",  # prose, no assignment shape at all
    "password=",  # empty value
    "password=***",  # 3-char placeholder, below the 6-char floor
    "port=8080",  # non-credential label
])
def test_short_value_negative_controls_pass(text):
    # lowering the value-length floor to catch short real secrets must not
    # make the rule prose- or placeholder-hostile at the low end
    check_content(text)


def test_max_chars_is_tunable():
    # the umbrella package passes a configured budget; core keeps 8000 as default
    check_content("x" * 20)
    with pytest.raises(GateError) as ei:
        check_content("x" * 20, max_chars=10)
    assert "10" in str(ei.value)
