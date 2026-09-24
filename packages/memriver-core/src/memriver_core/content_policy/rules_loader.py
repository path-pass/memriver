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

# RE2 spells the eight named POSIX classes as `[:name:]` inside a bracket
# expression; Python has no such syntax and reads `[[:alnum:]]` as a nested
# set. Expanding to the ASCII range keeps the vendored pattern's meaning.
_POSIX_CLASSES = {
    "alnum": "a-zA-Z0-9",
    "alpha": "a-zA-Z",
    "digit": "0-9",
    "lower": "a-z",
    "upper": "A-Z",
    "xdigit": "0-9A-Fa-f",
    "space": "\t\n\v\f\r ",
    "word": "0-9A-Za-z_",
}

# A "global flags" group: `(?` followed by one or more RE2/`re` flag letters
# to turn on, optionally followed by `-` and more letters to turn off --
# or, with nothing to turn on, just `-` and letters to turn off -- and a
# closing `)`, with nothing else inside. Never matches `(?:`, `(?=`,
# `(?<name>`, `(?P<name>`, `(?#...)` or an already-scoped `(?flags:...)`.
_FLAG_GROUP_RE = re.compile(r"\(\?([aiLmsux]+(?:-[aiLmsux]+)?|-[aiLmsux]+)\)")


def _re2_to_python(pattern: str) -> str:
    """Translate the RE2-only constructs gitleaks.toml uses into Python `re`.

    RE2 and Python `re` agree on almost everything a gitleaks rule needs, but
    three constructs differ:

    - A mid-pattern flag group `(?i)` is, in RE2, scoped from that point to
      the end of its innermost enclosing group (or the whole pattern); Python
      requires a bare `(?flags)` at position 0 of the pattern and rejects it
      anywhere else. A mid-pattern one is rewritten to the equivalent scoped
      group `(?flags:...)`, closed at the same point RE2 would have stopped
      applying it. A flag group already at position 0 is left untouched.
      RE2's scope crosses any `|` at the same depth -- it is not reset per
      alternative -- so a `|` at that depth closes every flag group still
      open there just before it and reopens the same ones just after; naively
      wrapping straight through to the group's close would instead fold that
      `|` *inside* the new group, turning what preceded the flag group into a
      mandatory prefix and making the other branches unreachable.
    - `\\z` (absolute end of text) is Python's `\\Z`; an escaped `\\\\z` (a
      literal backslash followed by 'z') is left alone.
    - A POSIX class such as `[:alnum:]` inside a bracket expression is
      expanded to its ASCII range; an unrecognised class name is left as-is,
      so it still fails to compile rather than silently mismatching.

    The pattern is scanned once, left to right, tracking backslash escapes
    (so `\\\\z` is never misread as `\\z`) and bracket expressions (so none of
    this rewriting happens inside `[...]` except the POSIX-class case).
    """
    out: list[str] = []
    # frames[-1] holds the flags of every translated flag group still "open"
    # at the depth currently being scanned, outermost first; frames[0] stands
    # in for the top level. Entering a real group pushes a fresh, empty list
    # -- an outer scope's flags need no help from '|' bookkeeping there,
    # since they already wrap the whole nested group in the output text.
    frames: list[list[str]] = [[]]
    in_class = False
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "\\" and i + 1 < n:
            if pattern[i + 1] == "z" and not in_class:
                out.append("\\Z")
            else:
                out.append(pattern[i:i + 2])
            i += 2
            continue
        if in_class:
            if ch == "[" and pattern[i + 1:i + 2] == ":":
                end = pattern.find(":]", i + 2)
                if end != -1 and pattern[i + 2:end] in _POSIX_CLASSES:
                    out.append(_POSIX_CLASSES[pattern[i + 2:end]])
                    i = end + 2
                    continue
                out.append(ch)
                i += 1
                continue
            if ch == "]":
                in_class = False
            out.append(ch)
            i += 1
            continue
        if ch == "[":
            in_class = True
            out.append(ch)
            i += 1
            # a leading '^' (negation) or ']' (literal close bracket as the
            # first member) does not end the bracket expression it opens
            if i < n and pattern[i] == "^":
                out.append(pattern[i])
                i += 1
            if i < n and pattern[i] == "]":
                out.append(pattern[i])
                i += 1
            continue
        if ch == "(":
            match = _FLAG_GROUP_RE.match(pattern, i)
            if match:
                if i == 0:
                    out.append(match.group(0))
                else:
                    out.append(f"(?{match.group(1)}:")
                    frames[-1].append(match.group(1))
                i = match.end()
                continue
            out.append(ch)
            frames.append([])
            i += 1
            continue
        if ch == ")":
            if len(frames) > 1:
                open_flags = frames.pop()
                out.append(")" * len(open_flags))
            out.append(ch)
            i += 1
            continue
        if ch == "|":
            open_flags = frames[-1]
            if open_flags:
                out.append(")" * len(open_flags))
                out.append(ch)
                out.extend(f"(?{flags}:" for flags in open_flags)
            else:
                out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    out.append(")" * len(frames[0]))
    return "".join(out)


def _load_rules(*sources: Traversable) -> list[_Rule]:
    """Parse and compile the vendored rule TOMLs once, at import.

    The patterns are written for Go's RE2, so a couple of dozen use constructs
    Python `re` does not accept as written. `_re2_to_python` translates those
    before compiling, so nothing is dropped merely for being RE2 syntax. What
    still fails to compile after translation -- a construct with no Python
    equivalent, or one this translation does not cover -- is dropped with its
    id logged at WARNING, never raised: a bad pattern must not take down the
    scanner and with it every write, but a dropped rule is a coverage loss an
    operator must see.

    Warnings are promoted to errors so that a construct Python merely tolerates
    with a *different* meaning -- an unrecognised POSIX class name, which
    `_re2_to_python` leaves untouched and Python then reads as a nested set --
    is skipped rather than silently mis-matching.

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
                    compiled = re.compile(_re2_to_python(pattern))
            except (re.error, Warning, RecursionError) as exc:
                _log.warning("scanner: skipping rule %s, regex unusable on this "
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
