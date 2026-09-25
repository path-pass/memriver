from __future__ import annotations

import math
import re
from collections import Counter
from importlib.resources import files

from memriver_core.content_policy.rules_loader import _load_rules
from memriver_core.models.errors import ContentRejected

_RULES_DIR = files(__package__) / "rules"

_RULES = _load_rules(_RULES_DIR / "memriver.toml", _RULES_DIR / "gitleaks.toml")

# C0 + C1 control characters and the Unicode line/paragraph separators -- the
# same class the index normalizer collapses (memriver_core.models.
# single_line). str.strip() only removes whitespace, so a body of
# nothing but e.g. "\x01\x02" reads as non-empty and would render as a
# near-blank index line.
_CONTROL_CHARS_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def _shannon_entropy(text: str) -> float:
    """Shannon entropy in bits per character, as gitleaks measures it."""
    if not text:
        return 0.0
    total = len(text)
    return -sum((n / total) * math.log2(n / total) for n in Counter(text).values())


class SecretScanner:
    """`ContentPolicy` implementation: rejects content that looks like a secret.

    The default ruleset is loaded and compiled once, at import, from the
    vendored TOML files under `content_policy/rules/`.
    """

    def check(self, text: str, max_chars: int) -> None:
        """Reject content that is empty, oversized, or looks like a credential.

        `max_chars` is a required parameter with no default here: the caller
        (the application layer) supplies the configured budget.
        """
        if not _CONTROL_CHARS_RE.sub("", text).strip():
            raise ContentRejected("content is empty; nothing to store")
        if len(text) > max_chars:
            raise ContentRejected(
                f"content too large ({len(text)} > {max_chars} chars); "
                "store a summary or pointer instead")
        lowered = text.lower()
        for rule_id, pat, entropy, group, keywords in _RULES:
            # gitleaks' own prefilter: a rule declaring keywords cannot match a
            # body that contains none of them, and skipping the regex is far
            # cheaper
            if keywords and not any(k in lowered for k in keywords):
                continue
            if entropy is None:
                if pat.search(text) is not None:
                    raise ContentRejected(_rejection(rule_id), rule_id=rule_id)
                continue
            # entropy-gated: a rule can have several candidates in one body,
            # and a low-entropy first one must not shadow a high-entropy
            # later one from the same rule
            for match in pat.finditer(text):
                if _shannon_entropy(_secret_of(match, group)) >= entropy:
                    raise ContentRejected(_rejection(rule_id), rule_id=rule_id)


def _secret_of(match: re.Match[str], group: int) -> str:
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


def _rejection(rule_id: str) -> str:
    # the matched text is deliberately absent: an error message travels into
    # logs and agent transcripts, which is exactly where a secret must not go
    return (f"content rejected: looks like a secret ({rule_id}). "
            "Store a pointer (e.g. 'token is in 1Password item X') instead.")
