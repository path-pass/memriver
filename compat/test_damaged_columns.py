"""Stored JSON columns that memriver could not have written: the Python implementation
refuses every sample the Rust tests refuse (`crates/memriver-core/tests/damaged_columns.rs`
reads the same file). Only the verdict is compared; the words of a refusal are never shown
to a user or an agent, and differ between the two."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from memriver_core.models.memory import tags_from_json
from memriver_core.repository.sqlite.session_store import session_from_row

SAMPLES = Path(__file__).parent.parent / "crates/memriver-core/tests/fixtures/damaged_columns.jsonl"
AT = "2026-11-22T02:16:43.300060Z"
ENTRY = f'{{"at":"{AT}","text":"x"}}'


def _session(first_prompt: str | None, recent_prompts: str):
    return session_from_row((
        "codex", "s1", "registered", "start", None, None, None, "/w", None, None, AT, AT, None,
        1, 0, 0, first_prompt, recent_prompts, None, None))


def _read(column: str, raw: str) -> None:
    if column == "tags":
        tags_from_json(raw)
    elif column == "first_prompt":
        _session(raw, "[]")
    else:
        assert column == "recent_prompts"
        _session(None, raw)


def test_the_python_implementation_refuses_every_damaged_column():
    samples = [json.loads(line) for line in SAMPLES.read_text(encoding="ascii").splitlines()]
    assert len(samples) == 117
    for sample in samples:
        try:
            _read(sample["column"], sample["raw"])
        except (ValueError, RecursionError):
            # the recursion limit instead of a ValueError on some interpreters: refused either way
            continue
        pytest.fail(f"{sample['column']} accepted {sample['raw'][:60]!r}")


def test_the_columns_memriver_writes_are_read():
    assert tags_from_json('["a","b"]') == ("a", "b")
    assert _session(ENTRY, f"[{ENTRY}]").recent_prompts[0].text == "x"
