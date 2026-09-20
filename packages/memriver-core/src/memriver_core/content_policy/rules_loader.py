"""Side-effect-free loader for the vendored gitleaks-format rule TOMLs.

Split out of `secret_scanner` so a caller can validate a rules file --
including one that will replace the *live* file `secret_scanner` loads at
import -- without importing `secret_scanner` itself. `secret_scanner` loads
its own default ruleset at module import (`_RULES = _load_rules(...)`), so if
that live file is already corrupt, merely importing `secret_scanner` raises;
a validator that has to import it first could then never validate, let alone
repair, exactly the file it exists to protect. Nothing here runs at import
time.
"""
from __future__ import annotations

import logging
import re
import tomllib
import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from importlib.abc import Traversable

# Pinned to the scanner's own name, not `__name__`: this loader's skipped-rule
# diagnostics are observed (by callers and by tests) on the
# "memriver_core.content_policy.secret_scanner" channel, and moving the code
# here must not move the log channel out from under them.
_log = logging.getLogger("memriver_core.content_policy.secret_scanner")

# (rule id, compiled pattern, entropy threshold, secretGroup, lowercased keywords)
_Rule = tuple[str, "re.Pattern[str]", float | None, int, tuple[str, ...]]


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
