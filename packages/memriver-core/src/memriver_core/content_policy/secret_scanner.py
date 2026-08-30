from __future__ import annotations

import logging
import math
import re
import tomllib
import warnings
from collections import Counter
from importlib.resources import files
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from importlib.abc import Traversable

from memriver_core.application.errors import ContentRejected

_log = logging.getLogger(__name__)

# (rule id, compiled pattern, entropy threshold, secretGroup, lowercased keywords)
_Rule = tuple[str, "re.Pattern[str]", float | None, int, tuple[str, ...]]

_RULES_DIR = files(__package__) / "rules"


def _load_rules(*sources: Traversable) -> list[_Rule]:
    """Parse and compile the vendored rule TOMLs once, at import.

    The patterns are written for Go's RE2, so a couple of dozen are not valid
    Python `re` -- and *which* ones depends on the interpreter (`\\z` only became
    legal in 3.14). Each is compiled here rather than at vendoring time so the
    ruleset adapts to whatever runs it; an incompatible rule is dropped with its
    id logged, never raised, because a bad pattern must not take down the scanner
    and with it every write.

    Warnings are promoted to errors so that constructs Python merely tolerates
    with a *different* meaning (POSIX classes such as `[[:alnum:]]`, which Python
    reads as a nested set) are skipped rather than silently mis-matching.

    Sources are read in order and ids are deduplicated first-wins, so memriver's
    own floor rules take precedence over an upstream rule of the same id.

    A `[policy]` table in any source names, in `honor_entropy_only_for`, the ids
    whose entropy threshold survives loading; every other rule is enforced by
    shape alone. That is what lets gitleaks.toml stay vendored verbatim while
    memriver applies a stricter reading of it. No `[policy]` table anywhere means
    every threshold is honoured -- upstream semantics, so a hand-trimmed or
    third-party rules file fails safe toward changing nothing.
    """
    rules: list[_Rule] = []
    seen: set[str] = set()
    honored: set[str] | None = None
    for source in sources:
        config = tomllib.loads(source.read_text(encoding="utf-8"))
        policy = config.get("policy")
        if policy is not None:
            honored = set(policy.get("honor_entropy_only_for", ()))
        for rule in config.get("rules", ()):
            rule_id = rule["id"]
            pattern = rule.get("regex")
            if not pattern:
                continue  # path-only rule: memriver gates content, not files
            if rule_id in seen:
                _log.debug("scanner: rule %s already defined, keeping the first", rule_id)
                continue
            seen.add(rule_id)
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error")
                    compiled = re.compile(pattern)
            except (re.error, Warning, RecursionError) as exc:
                _log.debug("scanner: skipping rule %s, regex unusable on this "
                           "interpreter: %s", rule_id, exc)
                continue
            entropy = rule.get("entropy")
            rules.append((
                rule_id,
                compiled,
                float(entropy) if entropy is not None else None,
                int(rule.get("secretGroup", 0)),
                tuple(k.lower() for k in rule.get("keywords", ())),
            ))
    if honored is None:
        return rules
    return [(rid, pat, ent if rid in honored else None, grp, kw)
            for rid, pat, ent, grp, kw in rules]


_RULES = _load_rules(_RULES_DIR / "memriver.toml", _RULES_DIR / "gitleaks.toml")


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
        if not text.strip():
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
            match = pat.search(text)
            if match is None:
                continue
            if entropy is not None and _shannon_entropy(_secret_of(match, group)) < entropy:
                continue
            raise ContentRejected(_rejection(rule_id))


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
