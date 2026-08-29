from __future__ import annotations

import math
import re
import warnings
from collections import Counter

from .gate_rules import VENDORED_RULES

MAX_BODY_CHARS = 8000

_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("AWS access key", re.compile(r"A[KS]IA[0-9A-Z]{16}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    # fine-grained PATs use a different prefix and allow '_' in the body, so the
    # classic gh[pousr]_ rule never matches them
    ("GitHub fine-grained PAT", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("private key block", re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----")),
    ("Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("OpenAI API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    # quotes are optional: env files, shell exports and log lines carry none.
    # No word boundary around the keyword: '_' is a word character, so the usual
    # env var names (OPENAI_API_KEY, AWS_SECRET_ACCESS_KEY) have none before the
    # keyword nor after it, and the trailing name parts have to be consumed too.
    # The required '[:=]' is what keeps prose out ('token 管理方式', '1Password').
    # The value has three branches because real passwords carry punctuation: a
    # single restricted character class stopped at the first '@' or '!' and
    # counted too few characters, so 'PASSWORD="P@ssw0rd!234567"' passed. Each
    # quote style needs its own branch, since one class shared by both stops at
    # the *opposite* quote inside the value and the unquoted branch then stops
    # at the first space -- 'PASSWORD="it's a very long secret phrase"' slipped
    # through both. Each quoted branch now runs to its own closing quote; the
    # unquoted branch takes any run of non-whitespace, which still keeps a
    # sentence after ':' out
    ("credential assignment",
     re.compile(r"(?i)(api[ _-]?key|secret|token|password|passwd)"
                r"(?:[ _-][A-Za-z0-9]+)*\s*[:=]\s*"
                r"(?:\"[^\"]{12,}\"|'[^']{12,}'|\S{12,})")),
]


def _compile_vendored() -> list[tuple[str, re.Pattern, float | None, int, tuple[str, ...]]]:
    """Compile the generated gitleaks rules once, at import.

    The sync script already proves every pattern compiles, so a failure here
    means the generated file was hand-edited; drop that one rule rather than
    taking the whole gate -- and with it every write -- down with it.
    """
    compiled = []
    for rule_id, pattern, entropy, group, keywords in VENDORED_RULES:
        try:
            compiled.append((rule_id, re.compile(pattern), entropy, group, keywords))
        except re.error as exc:
            warnings.warn(f"skipping uncompilable vendored rule {rule_id}: {exc}",
                          RuntimeWarning, stacklevel=2)
    return compiled


_VENDORED = _compile_vendored()


def _shannon_entropy(text: str) -> float:
    """Shannon entropy in bits per character, as gitleaks measures it."""
    if not text:
        return 0.0
    total = len(text)
    return -sum((n / total) * math.log2(n / total) for n in Counter(text).values())


class GateError(ValueError):
    pass


def check_content(body: str, max_chars: int = MAX_BODY_CHARS) -> None:
    """Reject content that is empty, oversized, or looks like a credential.

    `max_chars` is a plain parameter so callers can configure the budget without
    core growing a settings dependency; omitting it keeps the historical 8000.
    """
    if not body.strip():
        raise GateError("content is empty; nothing to store")
    if len(body) > max_chars:
        raise GateError(f"content too large ({len(body)} > {max_chars} chars); "
                        "store a summary or pointer instead")
    for label, pat in _SECRET_PATTERNS:
        if pat.search(body):
            raise GateError(_rejection(label))
    lowered = body.lower()
    for rule_id, pat, entropy, group, keywords in _VENDORED:
        # gitleaks' own prefilter: a rule declaring keywords cannot match a body
        # that contains none of them, and skipping the regex is far cheaper
        if keywords and not any(k in lowered for k in keywords):
            continue
        match = pat.search(body)
        if match is None:
            continue
        if entropy is not None and _shannon_entropy(_secret_of(match, group)) < entropy:
            continue
        raise GateError(_rejection(rule_id))


def _secret_of(match: re.Match, group: int) -> str:
    """The substring upstream measures entropy over.

    gitleaks tunes its thresholds against the credential itself, not the
    boilerplate a pattern has to anchor on -- a keyword and separator dragged
    into the match depress its entropy and let a real secret through. So take
    the rule's declared secretGroup, else the first capture group, and fall
    back to the whole match only when the pattern captures nothing.
    """
    for n in (group, 1):
        if 0 < n <= match.re.groups and match.group(n) is not None:
            return match.group(n)
    return match.group(0)


def _rejection(label: str) -> str:
    # the matched text is deliberately absent: an error message travels into
    # logs and agent transcripts, which is exactly where a secret must not go
    return (f"content rejected: looks like a secret ({label}). "
            "Store a pointer (e.g. 'token is in 1Password item X') instead.")
