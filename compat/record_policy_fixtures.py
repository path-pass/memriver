"""Record what the Python content policy answers, for the Rust tests to compare with.

    python -B compat/record_policy_fixtures.py recorded RAW OUT
    python -B compat/record_policy_fixtures.py matrix OUT
    python -B compat/record_policy_fixtures.py dialect OUT [FILLED]

`recorded` turns the inputs a run of the Python test suite fed the policy (one JSON
object a line: text, max_chars, rules) into the fixture (the text as hex, see
`hidden`): inputs that are not Unicode text are dropped and every verdict is asked
again here.

RAW is not made by this script and its recorder is not in the repository. It was
recorded once, by running the whole Python test suite (`pytest -q`, a temporary home)
with a pytest plugin that wraps `SecretScanner.check`: for every call whose text is a
`str` and whose budget is an `int` it kept the text, the budget and the rule set in
force, each distinct triple once -- None for the shipped rules; for a test's own rule
set the TOML sources `_load_rules` had read (the plugin wraps it too and remembers
them), or, for a rule list a test built by hand, one TOML source written from that
list's ids, patterns, thresholds, groups and keywords. 787 inputs were recorded; two
hold a lone surrogate and are dropped here, leaving 785. The plugin changed nothing of
the interpreter. Recording again is only needed when the Python tests feed the policy
other inputs; what the fixture says Python answers is asked again on every run of
`compat/test_policy_fixtures.py`.

`matrix` generates ASCII texts around every shipped rule -- every kind of white space
at every white-space position, and what may follow a token at the end of the text --
with the verdict of each; texts only the one rule with an entropy threshold can refuse,
with values on either side of it; and the entropy of 300 random texts as the bits of the
float.

`dialect` writes the hand-made rows below, one-rule rule sets that isolate each point
where the two regular-expression dialects part, with the Python verdict of each; the
verdict the Rust implementation must give is part of the row. `FILLED` is the fixture
as it stands: where a row leaves the Rust verdict open (`...`), it is taken from there.

Nothing of the interpreter is changed, and no store is touched: the policy is called
in memory. `verdict` is also what `compat/test_policy_fixtures.py` replays the three
fixtures with.
"""

from __future__ import annotations

import json
import random
import re
import re._parser as sre_parse
import struct
import sys
import tempfile
import tomllib
import warnings
from pathlib import Path

from memriver_core.content_policy import secret_scanner
from memriver_core.content_policy.rules_loader import _load_rules
from memriver_core.models.errors import ContentRejected

RULES_DIR = Path(secret_scanner.__file__).parent / "rules"
# what stands in for one white-space character, in this order: the ASCII white space,
# the four separators Python counts as white space, and three controls it does not
SUBSTITUTES = ["\t", "\n", "\v", "\f", "\r", " ", "\x1c", "\x1d", "\x1e", "\x1f",
               "\x00", "\x08", "\x7f"]
# what may follow a token at the end of the text
TAILS = ["", "\n", "\n\n", "\r\n", " ", "\nnext line"]
MAX_CHARS = 8000

_SHIPPED = secret_scanner._RULES
_loaded: dict[tuple[str, ...], list] = {}


def _rules(sources: list[str] | None) -> list:
    """The rule list of these TOML sources as the Python loader reads them."""
    if sources is None:
        return _SHIPPED
    key = tuple(sources)
    if key not in _loaded:
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for number, text in enumerate(sources):
                paths.append(Path(directory) / f"{number}.toml")
                paths[-1].write_text(text, encoding="utf-8")
            _loaded[key] = _load_rules(*paths)
    return _loaded[key]


def verdict(text: str, max_chars: int = MAX_CHARS, sources: list[str] | None = None) -> str | None:
    """The id of the rule that refuses `text`, or None; under `sources` when given."""
    secret_scanner._RULES = _rules(sources)
    try:
        secret_scanner.SecretScanner().check(text, max_chars)
    except ContentRejected as err:
        return err.rule_id
    finally:
        secret_scanner._RULES = _SHIPPED
    return None


def measured(rule_id: str, text: str) -> list[str]:
    """What the shipped rule `rule_id` measures for entropy in `text`, match by match."""
    for found_id, pattern, _entropy, group, _keywords in _SHIPPED:
        if found_id == rule_id:
            return [secret_scanner._secret_of(match, group) for match in pattern.finditer(text)]
    raise KeyError(rule_id)


def entropy_bits(text: str) -> str:
    """The entropy Python computes for `text`, as the sixteen hex digits of the float."""
    return struct.pack(">d", secret_scanner._shannon_entropy(text)).hex()


def hidden(text: str) -> str:
    """`text` as the hex digits of its UTF-8 bytes. The texts are synthetic, but many
    have the shape of a provider's token on purpose, and a hosting service that scans
    what is pushed for such shapes would refuse the file that spells them out."""
    return text.encode("utf-8").hex()


def shown(row: dict, key: str = "text") -> str:
    """The text a fixture row holds under `<key>_hex`."""
    return bytes.fromhex(row[f"{key}_hex"]).decode("utf-8")


def _write(path: str, rows: list[dict]) -> None:
    Path(path).write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="ascii")


# ------------------------------------------------------------------ recorded


def recorded(raw: str, out: str) -> None:
    rows = []
    for line in Path(raw).read_text(encoding="ascii").splitlines():
        sample = json.loads(line)
        # a lone surrogate is not Unicode text: nothing can send one as UTF-8
        if any(0xD800 <= ord(char) <= 0xDFFF for char in sample["text"]):
            continue
        sample["verdict"] = verdict(sample["text"], sample["max_chars"], sample["rules"])
        rows.append({"text_hex": hidden(sample["text"]), "max_chars": sample["max_chars"],
                     "rules": sample["rules"], "verdict": sample["verdict"]})
    _write(out, rows)
    print(f"{out}: {len(rows)} inputs")


# -------------------------------------------------------------------- matrix

_WORD = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
_CATEGORIES = {"category_digit": "0123456789", "category_word": _WORD, "category_space": " \t",
               "category_not_space": _WORD + "-+/=", "category_not_word": "-+/= .",
               "category_not_digit": "abcXYZ-_"}


def _member(rng: random.Random, items) -> str:
    """One character a bracket expression admits (negated: something plain outside it)."""
    negate = bool(items) and str(items[0][0]) == "NEGATE"
    choices = []
    for op, value in items:
        op = str(op)
        if op == "LITERAL":
            choices.append(chr(value))
        elif op == "RANGE":
            choices.append(chr(rng.randint(value[0], min(value[1], 0x7E))))
        elif op == "CATEGORY":
            choices.append(rng.choice(_CATEGORIES.get(str(value).lower(), "x")))
    if negate:
        return rng.choice([c for c in _WORD + "-.~" if c not in choices] or ["x"])
    return rng.choice(choices or ["x"])


def _generate(rng: random.Random, parsed) -> str:
    """A text the parsed pattern nearly matches: often it does, sometimes one short."""
    out = []
    for op, value in parsed:
        op = str(op)
        if op == "LITERAL":
            out.append(chr(value))
        elif op == "NOT_LITERAL":
            out.append("x" if chr(value) != "x" else "y")
        elif op == "ANY":
            out.append(rng.choice(_WORD))
        elif op == "IN":
            out.append(_member(rng, value))
        elif op in ("MAX_REPEAT", "MIN_REPEAT"):
            low, high, item = value
            count = rng.choice([low, low, min(high, low + 1), min(high, low + rng.randint(0, 6))])
            if low > 0 and rng.random() < 0.15:
                count = low - 1
            out.extend(_generate(rng, item) for _ in range(count))
        elif op == "SUBPATTERN":
            out.append(_generate(rng, value[3]))
        elif op == "BRANCH":
            out.append(_generate(rng, rng.choice(value[1])))
        elif op == "CATEGORY":
            out.append(rng.choice(_CATEGORIES.get(str(value).lower(), "x")))
        # anchors, word boundaries and lookaheads emit nothing
    return "".join(out)


def _texts(rng: random.Random, rule_id: str, pattern: re.Pattern, keywords, wanted: int):
    """Up to `wanted` ASCII texts around one rule, the token last: first ones the rule
    refuses, then ones it does not."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        parsed = sre_parse.parse(pattern.pattern, pattern.flags)
    hits, misses = [], []
    for _ in range(60):
        candidate = _generate(rng, parsed)
        keyword = rng.choice(keywords) if keywords else ""
        text = f"{keyword} {candidate}" if keyword else candidate
        if not text.isascii() or not text.strip() or len(text) > 600:
            continue
        (hits if verdict(text) == rule_id else misses).append(text)
        if len(hits) >= wanted:
            break
    chosen = hits[:wanted - 1] + misses[:1]
    for text in hits[wanted - 1:] + misses[1:]:
        if len(chosen) < wanted and text not in chosen:
            chosen.append(text)
    return chosen, bool(hits)


def _has_dollar(pattern: str) -> bool:
    """Whether the pattern as written has a `$` that is neither escaped nor in brackets."""
    in_class, at = False, 0
    while at < len(pattern):
        char = pattern[at]
        if char == "\\":
            at += 2
            continue
        if in_class:
            in_class = char != "]"
        elif char == "[":
            in_class = True
            at += 1 + (pattern[at + 1:at + 2] == "^")
            at += pattern[at:at + 1] == "]"
            continue
        elif char == "$":
            return True
        at += 1
    return False


GENERIC = "generic-api-key"


def _generic(rng: random.Random) -> list[dict]:
    """Texts only the one rule with an entropy threshold can refuse: a key name and a
    separator no floor rule knows, and a value whose entropy lies on either side of the
    threshold -- because its alphabet is small or large. Then texts with two candidates:
    a low one before a high one, which is refused, and two low ones, which are not."""
    names, separators = ["auth", "access", "key", "auth.key"], ["=>", ":=", " => ", " := ", ","]

    def value(symbols: int) -> str:
        alphabet = rng.sample("abcdefghijklmnopqrstuvwxyz0123456789", symbols)
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(12, 36)))

    def row(text: str, kind: str) -> dict:
        return {"rule": GENERIC, "text_hex": hidden(text), "verdict": verdict(text), "subs": [],
                "generic": kind, "measured_hex": [hidden(part) for part in measured(GENERIC, text)]}

    def candidate(symbols: int) -> str:
        return f"{rng.choice(names)}{rng.choice(separators)}{value(symbols)}"

    wanted = {(GENERIC, "one"): 40, (None, "one"): 40, (GENERIC, "low-then-high"): 12,
              (None, "two-low"): 8}
    rows = []
    for _ in range(4000):
        if not any(wanted.values()):
            break
        if rng.random() < 0.6:
            made = row(candidate(rng.randint(6, 18)), "one")
        else:
            low, other = candidate(rng.randint(3, 8)), candidate(rng.randint(3, 22))
            text = f"{low}{rng.choice([chr(10), '; ', ' '])}{other}"
            parts = [secret_scanner._shannon_entropy(part) for part in measured(GENERIC, text)]
            if len(parts) != 2 or parts[0] >= 3.5:
                continue
            made = row(text, "low-then-high" if parts[1] >= 3.5 else "two-low")
        key = (made["verdict"], made["generic"])
        if wanted.get(key, 0) > 0:
            wanted[key] -= 1
            rows.append(made)
    assert not any(wanted.values()), wanted
    return rows


def matrix(out: str) -> None:
    rng = random.Random(117)
    written = {rule["id"]: rule["regex"] for name in ("memriver.toml", "gitleaks.toml")
               for rule in tomllib.loads((RULES_DIR / name).read_text(encoding="utf-8"))["rules"]
               if rule.get("regex")}
    rows, with_hit, dollars = [], 0, 0
    for rule_id, pattern, entropy, _group, keywords in _SHIPPED:
        texts, hit = _texts(rng, rule_id, pattern, keywords, 3)
        with_hit += hit
        for text in texts:
            substitutions = []
            for at, char in enumerate(text):
                if char.isspace():
                    substitutions.append(
                        [at, [verdict(text[:at] + other + text[at + 1:]) for other in SUBSTITUTES]])
            rows.append({"rule": rule_id, "text_hex": hidden(text), "verdict": verdict(text),
                         "subs": substitutions})
        if _has_dollar(written[rule_id]) and texts:
            dollars += 1
            for tail in TAILS:
                text = texts[0] + tail
                row = {"rule": rule_id, "text_hex": hidden(text), "verdict": verdict(text),
                       "tail": True}
                if entropy is not None:
                    row["measured_hex"] = [hidden(part) for part in measured(rule_id, text)]
                rows.append(row)
    generic = _generic(random.Random(3500))
    rows.extend(generic)
    checks = sum(1 + len(row.get("subs", ())) * len(SUBSTITUTES) for row in rows)
    # the entropy itself, as the bits of the float: a threshold is compared with it
    alphabet = _WORD + "-+/=!@ \u00e9\u4e2d"
    for _ in range(300):
        text = "".join(rng.choice(alphabet[:rng.randint(2, len(alphabet))])
                       for _ in range(rng.randint(1, 48)))
        rows.append({"entropy": text, "bits": entropy_bits(text)})
    _write(out, rows)
    print(f"{out}: {len(rows) - 300} texts ({len(generic)} of them for {GENERIC}), "
          f"{checks} verdicts, 300 entropies; {with_hit} of {len(_SHIPPED)} rules "
          f"have a text they refuse; {dollars} rules with a $")


# ------------------------------------------------------------------- dialect


def _one(pattern: str) -> str:
    """A rule file of one rule, `r`, with this pattern."""
    assert "'''" not in pattern
    return f"[[rules]]\nid = \"r\"\nregex = '''{pattern}'''\n"


HIT, MISS = "r", None
KELVIN, LONG_S = "K", "ſ"
# (pattern, text, what Rust must answer, which listed difference when that is not what
# Python answers, why). `...` as the Rust answer: recorded from the finished Rust code
# and then frozen -- the test asserts it differs from Python's.
_ROWS = [
    (r"(?i)x\w", "x" + KELVIN, HIT, None,
     "under i the narrowed word class folds to the Kelvin sign, as Python's does"),
    (r"(?i)x\w", "x" + LONG_S, HIT, None, "and to the long s"),
    (r"x\w", "x" + KELVIN, MISS, "a", "without i the narrowed class is ASCII"),
    (r"x\w", "x" + LONG_S, MISS, "a", "without i the narrowed class is ASCII"),
    (r"x\w", "xK", HIT, None, "control: the ASCII letter, without i"),
    (r"(?i)x\w", "xK", HIT, None, "control: the ASCII letter, with i"),
    (r"x\w", "xé", MISS, "a", "a letter outside ASCII is no word character"),
    (r"x\w", "x５", MISS, "a", "a full-width digit is no word character"),
    (r"(?i)x\w", "xé", MISS, "a", "nor under i"),
    (r"x\d", "x５", MISS, "a", "a full-width digit is no digit"),
    (r"(?i)x\d", "x５", MISS, "a", "nor under i"),
    (r"x[\w-]", "xé", MISS, "a", "the same inside a bracket expression"),
    (r"x[^\w]", "xé", HIT, "a", "and negated: a letter outside ASCII is a non-word character"),
    (r"x\W", "xé", HIT, "a", "as it is to the negated class"),
    (r"x\D", "x５", HIT, "a", "a full-width digit is a non-digit"),
    (r"(?i)x[a-z]", "x" + KELVIN, HIT, None, "an explicit letter class folds the same in both"),
    (r"(?i)x[a-z]", "x" + LONG_S, HIT, None, "an explicit letter class folds the same in both"),
    (r"x[a-z]", "x" + KELVIN, MISS, None, "and without i matches neither"),
    (r"\bkey", "ákey", ..., "b", "a combining mark is a word character to the crate only"),
    (r"key\b", "keý", ..., "b", "a combining mark is a word character to the crate only"),
    (r"key\b", "key‿", ..., "b", "connector punctuation other than _ likewise"),
    (r"key\b", "key²", ..., "b", "a numeric character that is no decimal digit: Python's only"),
    (r"key\b", "key_", MISS, None, "control: the underscore is a word character to both"),
    (r"key\b", "key-", HIT, None, "control: a hyphen is not"),
    (r"(?i)api", "apı", MISS, "c", "dotless i matches i under Python's IGNORECASE only"),
    (r"(?i)api", "APİ", MISS, "c", "and so does the dotted capital I"),
    (r"(?i)api", "API", HIT, None, "control: ASCII case folding"),
    (r"A$", "A\n", HIT, None, "$ before one final line feed"),
    (r"A$", "A\n\n", MISS, None, "not before two"),
    (r"A$", "A", HIT, None, "$ at the end"),
    (r"A$", "A\r\n", MISS, None, "a carriage return is no line end"),
    (r"A$", "xA\ny", MISS, None, "nor is a line end in the middle"),
    (r"(?:A|B$|C)", "B\n", HIT, None, "$ as the last item of an alternative"),
    (r"(?:A|B$|C)", "B\nx", MISS, None, "$ as the last item of an alternative"),
    (r"(?:x(?:A$|B)|C$)", "xA\n", HIT, None, "$ in a nested alternative at the end"),
    (r"k\sv", "k\x1fv", HIT, None, "U+001F is white space to Python"),
    (r"k\sv", "k\x1cv", HIT, None, "and so is U+001C"),
    (r"k\sv", "k\u0085v", HIT, None, "U+0085 is white space to both"),
    (r"k\sv", "k　v", HIT, None, "the ideographic space is white space to both"),
    (r"k\sv", "k\x08v", MISS, None, "control: a backspace is not"),
    (r"k[\s:]v", "k\x1ev", HIT, None, "the same inside a bracket expression"),
    (r"k[^\s]v", "k\x1cv", MISS, None, "and negated"),
    (r"k[\S]v", "k\x1cv", MISS, None, "\\S inside a bracket expression is a nested class"),
    (r"k[\S]v", "kxv", HIT, None, "\\S inside a bracket expression is a nested class"),
    (r"k[\s\S-]v", "k\x1dv", HIT, None, "white space or not, both in one bracket expression"),
    (r"k\Sv", "k\x1dv", MISS, None, "\\S outside one"),
    (r"k\Sv", "k-v", HIT, None, "\\S outside one"),
    (r"[[:alnum:]]{3}x", "a1Zx", HIT, None, "a POSIX class is ASCII to both"),
    (r"[[:alnum:]]{3}x", "aéZx", MISS, None, "a POSIX class is ASCII to both"),
    (r"a(?i)b|c", "aB", HIT, None, "a flag group in the middle holds to the end of its group"),
    (r"a(?i)b|c", "C", HIT, None, "across the alternation"),
    (r"a(?i)b|c", "Ab", MISS, None, "and not before itself"),
    (r"(?:a(?i)b)c", "aBC", MISS, None, "a flag group ends with the group it stands in"),
    (r"(?i)a(?-i)b", "Ab", HIT, None, "a flag turned off again"),
    (r"(?i)a(?-i)b", "AB", MISS, None, "a flag turned off again"),
    (r"a\z", "a\n", MISS, None, "\\z is the very end only"),
    (r"a\z", "a", HIT, None, "\\z is the very end only"),
]
# (pattern, the loader error Rust must give, why); these are never compiled
_REFUSED = [
    (r"A$\b", "DollarNotInTailPosition", "a zero-width assertion after $"),
    (r"A$B", "DollarNotInTailPosition", "a character after $"),
    (r"(?:A$)+", "DollarNotInTailPosition", "a quantified group around $"),
    (r"(?:A$|B)C", "DollarNotInTailPosition", "something after the group $ ends"),
    (r"(?m)A$", "MultilineFlag", "$ is a line end under m in both engines"),
    (r"(?im:A)", "MultilineFlag", "the m flag, scoped"),
    (r"[[:blank:]]x", "UnknownClass", "a POSIX class the Python implementation drops the rule for"),
    (r"\pLx", "UnknownClass", "a Unicode class the Python implementation has no syntax for"),
]


def dialect(out: str, filled: str | None = None) -> None:
    frozen = {}
    if filled is not None and Path(filled).exists():
        for line in Path(filled).read_text(encoding="ascii").splitlines():
            row = json.loads(line)
            if "text" in row:
                frozen[row["rules"], row["text"]] = row["rust"]
    rows = []
    for pattern, text, rust, difference, why in _ROWS:
        python = verdict(text, MAX_CHARS, [_one(pattern)])
        if rust is ...:
            rust = frozen.get((_one(pattern), text), "?")
        assert (python != rust) == (difference is not None), (pattern, text, python, rust)
        rows.append({"rules": _one(pattern), "text": text, "python": python, "rust": rust,
                     "difference": difference, "why": why})
    for pattern, error, why in _REFUSED:
        rows.append({"rules": _one(pattern), "rust_error": error, "why": why})
    _write(out, rows)
    print(f"{out}: {len(_ROWS)} verdicts, {len(_REFUSED)} refused patterns")


if __name__ == "__main__":
    {"recorded": recorded, "matrix": matrix, "dialect": dialect}[sys.argv[1]](*sys.argv[2:])
