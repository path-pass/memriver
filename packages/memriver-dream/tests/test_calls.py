from memriver_dream.calls import DATA_RULE, call, cut, estimate_tokens, matches
from memriver_dream.protocols import ExecutorResult

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["summary"],
          "properties": {"summary": {"type": "string"}}}
MATCH_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["kind", "items"],
                "properties": {"kind": {"type": "string", "enum": ["a", "b"]},
                               "items": {"type": "array", "items": {"type": "integer"}},
                               "flag": {"type": "boolean"}}}


def test_a_valid_answer_is_returned_and_the_data_rule_rides_on_the_system_prompt(executor):
    executor.replies = [{"summary": "ok"}]
    assert call(executor, system_prompt="Summarize.", prompt="p", schema=SCHEMA) == {
        "summary": "ok"}
    assert executor.calls[0]["system_prompt"].endswith(DATA_RULE)
    assert executor.calls[0]["timeout_s"] == 300


def test_a_failure_passes_through_and_a_schema_mismatch_is_schema(executor):
    executor.replies = [ExecutorResult(error="quota"), {"summary": 1}, {"other": "x"}]
    assert [call(executor, system_prompt="s", prompt="p", schema=SCHEMA)
            for _ in range(3)] == ["quota", "schema", "schema"]


def test_estimate_counts_a_wide_character_as_one_token_and_four_others_as_one():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2                  # rounded up
    assert estimate_tokens(chr(0x4E2D) * 3) == 3          # CJK
    assert estimate_tokens(chr(0xFF21) + "ab") == 2       # fullwidth + two others


def test_cut_keeps_every_piece_within_the_budget_and_loses_nothing():
    text = "x" * 50 + chr(0x4E2D) * 30 + "y" * 7
    pieces = cut(text, 10)
    assert "".join(pieces) == text
    assert all(estimate_tokens(piece) <= 10 for piece in pieces)
    assert len(pieces) > 1


def test_matches_the_subset_dream_uses():
    assert matches({"kind": "a", "items": [1, 2]}, MATCH_SCHEMA)
    assert matches({"kind": "b", "items": [], "flag": True}, MATCH_SCHEMA)


def test_refuses_what_the_schema_does_not_allow():
    for value in ({"kind": "c", "items": []}, {"kind": "a"}, {"kind": "a", "items": ["1"]},
                  {"kind": "a", "items": [True]}, {"kind": "a", "items": [], "extra": 1},
                  ["kind"], {"kind": "a", "items": [], "flag": 1}):
        assert not matches(value, MATCH_SCHEMA), value
