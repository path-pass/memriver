"""The three content-policy fixtures of the Rust tests say what the Python implementation
answers: asked again here, line by line, so a fixture that drifts from Python -- or a
Python that starts answering differently -- fails."""

from __future__ import annotations

import json
from pathlib import Path

from .record_policy_fixtures import (
    SUBSTITUTES,
    dialect,
    entropy_bits,
    measured,
    shown,
    verdict,
)

FIXTURES = Path(__file__).parent.parent / "crates/memriver-core/tests/fixtures"


def _rows(name: str) -> list[dict]:
    return [json.loads(line) for line in (FIXTURES / name).read_text(encoding="ascii").splitlines()]


def test_the_recorded_inputs_get_the_recorded_verdicts():
    rows = _rows("policy.jsonl")
    assert len(rows) == 785
    for row in rows:
        text = shown(row)
        assert verdict(text, row["max_chars"], row["rules"]) == row["verdict"], text[:80]
    assert sum(row["rules"] is not None for row in rows) == 11
    assert {"empty", "too-large", None} <= {row["verdict"] for row in rows}


def test_the_ascii_matrix_is_what_python_answers():
    rows = _rows("policy_ascii_matrix.jsonl")
    verdicts = entropies = 0
    for row in rows:
        if "entropy" in row:
            assert entropy_bits(row["entropy"]) == row["bits"], row["entropy"]
            entropies += 1
            continue
        text = shown(row)
        assert text.isascii()
        assert verdict(text) == row["verdict"], text
        verdicts += 1
        for at, expected in row.get("subs", ()):
            assert text[at].isspace()
            found = [verdict(text[:at] + other + text[at + 1:]) for other in SUBSTITUTES]
            assert found == expected, (text, at)
            verdicts += len(SUBSTITUTES)
        if "measured_hex" in row:
            parts = [bytes.fromhex(part).decode("utf-8") for part in row["measured_hex"]]
            assert measured(row["rule"], text) == parts, text
    assert (verdicts, entropies) == (16469, 300)
    # the one rule with an entropy threshold is a verdict here, and so is its absence
    generic = [row["verdict"] for row in rows if "generic" in row]
    assert (generic.count("generic-api-key"), generic.count(None)) == (52, 48)


def test_the_dialect_rows_state_what_python_answers(tmp_path):
    rows = _rows("policy_dialect.jsonl")
    for row in rows:
        if "text" in row:
            assert verdict(row["text"], 8000, [row["rules"]]) == row["python"], row
            # a difference is tagged, and nothing on ASCII text is one
            assert (row["python"] != row["rust"]) == (row["difference"] is not None), row
            assert row["difference"] is None or not row["text"].isascii(), row
    assert sum("rust_error" in row for row in rows) == 8
    # the file is what the script writes from its own rows
    again = tmp_path / "again.jsonl"
    dialect(str(again), str(FIXTURES / "policy_dialect.jsonl"))
    assert again.read_bytes() == (FIXTURES / "policy_dialect.jsonl").read_bytes()
