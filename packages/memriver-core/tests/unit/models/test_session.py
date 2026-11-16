from __future__ import annotations

import pytest
from memriver_core.models import PromptEntry, SessionKey, is_call_id, now


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


@pytest.mark.parametrize("call_id", ["a", "x" * 256, "toolu_01ABCdef", "调用"])
def test_a_call_id_is_printable_up_to_256_characters(call_id):
    assert is_call_id(call_id)


@pytest.mark.parametrize("call_id", [
    "", "x" * 257, "a b", "a\n", "a" + chr(0x202E), "a" + chr(0xD800), None, 7,
])
def test_a_call_id_is_never_empty_long_whitespace_or_invisible(call_id):
    assert not is_call_id(call_id)


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


# --- session moves ---------------------------------------------------------------

from memriver_core.models import CallRecord, MoveOutcome, SessionMove

_KEY = SessionKey("claude-code", "s1")
_STAMP = "2026-11-16T00:00:00.000000Z"


def _move(**overrides):
    fields = {"key": _KEY, "seq": 1, "moved_at": _STAMP, "from_entry_cwd": "/a",
              "from_project_id": "0123456789", "to_entry_cwd": "/b", "to_project_id": None}
    return SessionMove(**{**fields, **overrides})


def test_a_session_move_holds_what_it_was_given():
    move = _move()
    assert (move.seq, move.from_project_id, move.to_project_id) == (1, "0123456789", None)


@pytest.mark.parametrize("overrides", [
    {"seq": 0}, {"seq": -1}, {"seq": "1"}, {"seq": True}, {"seq": 1.0},
    {"moved_at": "yesterday"}, {"moved_at": None},
    {"from_entry_cwd": None}, {"to_entry_cwd": 3},
    {"from_project_id": "short"}, {"to_project_id": "ABCDEFGHIJ"}, {"from_project_id": 5},
])
def test_a_session_move_rejects_a_value_it_could_not_hold(overrides):
    with pytest.raises(ValueError):
        _move(**overrides)


def test_a_call_record_is_a_key_and_an_optional_directory():
    assert CallRecord(_KEY, "/w").cwd == "/w"
    assert CallRecord(_KEY, None).cwd is None


def _outcome(**overrides):
    fields = {"from_project_id": None, "to_project_id": None, "from_candidate_id": None,
              "to_candidate_id": None, "to_entry_cwd": "/b", "pending": False,
              "degraded": False}
    return MoveOutcome(**{**fields, **overrides})


@pytest.mark.parametrize(("overrides", "changed"), [
    ({}, False),
    ({"from_project_id": "a" * 10, "to_project_id": "a" * 10}, False),
    ({"from_project_id": "a" * 10}, True),
    ({"to_project_id": "b" * 10}, True),
    ({"from_project_id": "a" * 10, "to_project_id": "b" * 10}, True),
    ({"degraded": True}, True),
    ({"pending": True}, False),
    ({"pending": True, "from_candidate_id": "a" * 10, "to_candidate_id": "a" * 10}, False),
    ({"pending": True, "to_candidate_id": "b" * 10}, True),
    # a pending row compares candidates, never project ids
    ({"pending": True, "from_project_id": "a" * 10}, False),
    # a registered row compares project ids, never candidates
    ({"to_candidate_id": "b" * 10}, False),
])
def test_a_move_outcome_changed_truth_table(overrides, changed):
    assert _outcome(**overrides).changed is changed
