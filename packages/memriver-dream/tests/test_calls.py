from memriver_dream.calls import DATA_RULE, call
from memriver_dream.protocols import ExecutorResult

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["summary"],
          "properties": {"summary": {"type": "string"}}}


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
