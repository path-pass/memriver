from __future__ import annotations

import pytest
from memriver_core.models import PromptEntry, SessionKey, now


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
def test_a_session_key_accepts_the_two_harnesses_that_supply_ids(harness):
    assert SessionKey(harness, "abc-123").harness == harness


def test_a_session_key_rejects_any_other_harness():
    with pytest.raises(ValueError):
        SessionKey("cursor", "abc")


@pytest.mark.parametrize("session_id", ["a", "x" * 128, "0199a2b4-7c1d-7e2f-9a3b-4c5d6e7f8091",
                                        "会话"])
def test_a_session_key_accepts_printable_ids_up_to_128_characters(session_id):
    assert SessionKey("codex", session_id).session_id == session_id


@pytest.mark.parametrize("session_id", [
    "", "x" * 129, "a b", "a\tb", "a\n",
    "a" + chr(0x202E), "a" + chr(0x200B), "a" + chr(0xD800), "a" + chr(0x3000),
    None, 7,
])
def test_a_session_key_rejects_empty_long_whitespace_and_invisible_ids(session_id):
    with pytest.raises(ValueError):
        SessionKey("claude-code", session_id)


def test_a_prompt_entry_holds_text_or_an_omission():
    at = now()
    assert PromptEntry(at, text="fix the build").text == "fix the build"
    assert PromptEntry(at, omitted="secret").omitted == "secret"


@pytest.mark.parametrize("fields", [
    {},                                              # neither
    {"text": "x", "omitted": "secret"},              # both
    {"omitted": "because"},                          # not one of the four
    {"text": 5},                                     # not text
])
def test_a_prompt_entry_needs_exactly_one_of_text_and_a_known_omission(fields):
    with pytest.raises(ValueError):
        PromptEntry(now(), **fields)


@pytest.mark.parametrize("at", ["", "yesterday", "2026-02-30T00:00:00.000000Z", None])
def test_a_prompt_entry_needs_a_timestamp(at):
    with pytest.raises(ValueError):
        PromptEntry(at, text="x")


def test_a_rejected_prompt_entry_never_echoes_its_text():
    with pytest.raises(ValueError) as exc_info:
        PromptEntry("not a time", text="my secret prompt")
    assert "my secret prompt" not in str(exc_info.value)
