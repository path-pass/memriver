"""Transcript readers over synthetic Claude Code and Codex files (structure only)."""

from __future__ import annotations

import json
import os

import pytest
from memriver.transcripts import ClaudeTranscripts, CodexTranscripts, HarnessTranscripts
from memriver_core.models import Session, SessionKey

AT = "2026-09-25T10:00:00.000Z"


def _session(harness: str, path) -> Session:
    return Session(key=SessionKey(harness, "s1"), status="registered", origin="start",
                   project_id="pppppppppp", candidate_id=None, candidate_root=None,
                   entry_cwd="/work", branch=None,
                   transcript_path=None if path is None else str(path),
                   started_at="2026-09-25T10:00:00.000000Z",
                   last_active_at="2026-09-25T10:00:00.000000Z", ended_at=None,
                   prompt_count=0, last_write_prompt_count=0, last_nudge_prompt_count=0,
                   first_prompt=None, recent_prompts=())


def _write(path, objects, tail: str = "") -> None:
    path.write_text("".join(json.dumps(o) + "\n" for o in objects) + tail, encoding="utf-8")


CLAUDE = [
    {"type": "user", "timestamp": AT, "isSidechain": False,
     "message": {"role": "user", "content": "fix the login bug on branch fix-login"}},
    {"type": "user", "timestamp": AT, "isMeta": True,
     "message": {"role": "user", "content": "injected caveat"}},
    {"type": "assistant", "timestamp": AT, "isSidechain": False,
     "message": {"role": "assistant", "content": [
         {"type": "thinking", "thinking": "private"},
         {"type": "text", "text": "Looking at auth.py"},
         {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"path": "auth.py"}}]}},
    {"type": "user", "timestamp": AT, "isSidechain": False,
     "message": {"role": "user", "content": [
         {"type": "tool_result", "tool_use_id": "toolu_1", "content": "x" * 50}]}},
    {"type": "assistant", "timestamp": AT, "isSidechain": True,
     "message": {"role": "assistant", "content": [{"type": "text", "text": "sub-agent"}]}},
    {"type": "user", "timestamp": AT, "isCompactSummary": True,
     "message": {"role": "user", "content": "Summary of the earlier conversation"}},
    {"type": "ai-title", "aiTitle": "Fix login", "sessionId": "s1"},
    {"type": "attachment", "timestamp": AT, "attachment": {"type": "x"}},
]


def test_claude_records_keep_prompts_text_tools_compactions_and_titles(tmp_path):
    path = tmp_path / "s1.jsonl"
    _write(path, CLAUDE)
    transcript = ClaudeTranscripts(tool_output_chars=10).read(_session("claude-code", path))
    assert [(r.kind, r.text) for r in transcript.records] == [
        ("user", "fix the login bug on branch fix-login"),
        ("assistant", "Looking at auth.py"),
        ("tool", "Read: " + "x" * 10 + " [cut]"),
        ("compact", "Summary of the earlier conversation"),
        ("title", "Fix login"),
    ]
    assert transcript.records[0].at == AT and transcript.complete


def test_an_unfinished_last_line_is_ignored_and_left_out_of_the_fingerprint(tmp_path):
    whole, partial = tmp_path / "whole.jsonl", tmp_path / "partial.jsonl"
    _write(whole, CLAUDE[:1])
    _write(partial, CLAUDE[:1], tail='{"type": "user", "mess')
    reader = ClaudeTranscripts(tool_output_chars=100)
    first, second = reader.read(_session("claude-code", whole)), reader.read(
        _session("claude-code", partial))
    assert (first.complete, second.complete) == (True, False)
    assert first.records == second.records and first.fingerprint == second.fingerprint
    _write(whole, CLAUDE[:2])
    assert reader.read(_session("claude-code", whole)).fingerprint != first.fingerprint


def test_a_bad_line_is_skipped_and_no_path_inside_a_transcript_is_followed(tmp_path):
    other = tmp_path / "other.txt"
    other.write_text("MARKER-FROM-ANOTHER-FILE", encoding="utf-8")
    path = tmp_path / "s1.jsonl"
    path.write_text("not json\n" + json.dumps({
        "type": "user", "timestamp": AT,
        "message": {"role": "user", "content": f"see {other}"}}) + "\n", encoding="utf-8")
    transcript = ClaudeTranscripts(tool_output_chars=100).read(_session("claude-code", path))
    assert [r.text for r in transcript.records] == [f"see {other}"]


def test_a_lone_surrogate_in_a_record_is_never_handed_out(tmp_path):
    # json.dumps escapes it to plain ASCII (\ud800), so the file itself holds no raw
    # surrogate; json.loads then decodes that escape back into one, same as a harness's
    # own transcript could (memriver_dream.calls.storable notes UTF-8 takes none of them).
    path = tmp_path / "s1.jsonl"
    _write(path, [{"type": "user", "timestamp": AT,
                   "message": {"role": "user", "content": chr(0xD800)}}] + CLAUDE[:1])
    transcript = ClaudeTranscripts(tool_output_chars=100).read(_session("claude-code", path))
    assert [r.text for r in transcript.records] == ["fix the login bug on branch fix-login"]


@pytest.mark.parametrize("kind", ["none", "relative", "missing", "directory", "symlink",
                                  "fifo"])
def test_anything_but_a_readable_regular_file_is_no_transcript(tmp_path, kind):
    target = tmp_path / "real.jsonl"
    _write(target, CLAUDE[:1])
    path = {"none": None, "relative": "s1.jsonl", "missing": tmp_path / "gone.jsonl",
            "directory": tmp_path, "symlink": tmp_path / "link.jsonl",
            "fifo": tmp_path / "pipe"}[kind]
    if kind == "symlink":
        path.symlink_to(target)
    if kind == "fifo":
        os.mkfifo(path)
    assert ClaudeTranscripts(tool_output_chars=100).read(_session("claude-code", path)) is None


CODEX = [
    {"timestamp": AT, "type": "session_meta", "payload": {"id": "s1", "source": "cli"},
     "ordinal": 0},
    {"timestamp": AT, "type": "response_item", "ordinal": 1, "payload": {
        "type": "message", "role": "developer",
        "content": [{"type": "input_text", "text": "developer instructions"}]}},
    {"timestamp": AT, "type": "response_item", "ordinal": 2, "payload": {
        "type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<environment_context>cwd</environment_context>"},
            {"type": "input_text", "text": "speed up the build"}]}},
    {"timestamp": AT, "type": "response_item", "ordinal": 3, "payload": {
        "type": "reasoning", "summary": [], "encrypted_content": "e"}},
    {"timestamp": AT, "type": "response_item", "ordinal": 4, "payload": {
        "type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{}"}},
    {"timestamp": AT, "type": "response_item", "ordinal": 5, "payload": {
        "type": "function_call_output", "call_id": "c1", "output": "y" * 30}},
    {"timestamp": AT, "type": "response_item", "ordinal": 6, "payload": {
        "type": "custom_tool_call", "call_id": "c2", "name": "apply_patch", "input": "p"}},
    {"timestamp": AT, "type": "response_item", "ordinal": 7, "payload": {
        "type": "custom_tool_call_output", "call_id": "c2",
        "output": [{"type": "input_text", "text": "patched build.py"}]}},
    {"timestamp": AT, "type": "compacted", "ordinal": 8, "payload": {
        "message": "Summary so far", "replacement_history": []}},
    {"timestamp": AT, "type": "response_item", "ordinal": 9, "payload": {
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "Cached the dependencies"}]}},
    {"timestamp": AT, "type": "event_msg", "ordinal": 10, "payload": {"type": "token_count"}},
]


def test_codex_records_keep_prompts_answers_tools_and_compactions(tmp_path):
    path = tmp_path / "rollout.jsonl"
    _write(path, CODEX)
    transcript = CodexTranscripts(tool_output_chars=10).read(_session("codex", path))
    assert [(r.kind, r.text) for r in transcript.records] == [
        ("user", "speed up the build"),
        ("tool", "shell: " + "y" * 10 + " [cut]"),
        ("tool", "apply_patch: patched bu [cut]"),
        ("compact", "Summary so far"),
        ("assistant", "Cached the dependencies"),
    ]


def test_a_users_own_markup_is_a_prompt_in_both_harnesses(tmp_path):
    claude, codex = tmp_path / "c.jsonl", tmp_path / "x.jsonl"
    xml = "<task>Fix PR #1234 in auth.py</task>"
    _write(claude, [{"type": "user", "timestamp": AT,
                     "message": {"role": "user", "content": xml}}])
    _write(codex, [{"timestamp": AT, "type": "response_item", "ordinal": 1, "payload": {
        "type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<user_instructions>injected</user_instructions>"},
            {"type": "input_text", "text": "# AGENTS.md instructions for /work\n\ninjected"},
            {"type": "input_text", "text": xml}]}}])
    assert [r.text for r in ClaudeTranscripts(tool_output_chars=100).read(
        _session("claude-code", claude)).records] == [xml]
    assert [r.text for r in CodexTranscripts(tool_output_chars=100).read(
        _session("codex", codex)).records] == [xml]


def test_the_pre_0_157_agents_md_header_with_no_for_clause_is_still_dropped(tmp_path):
    # codex-cli 0.156.1 writes "# AGENTS.md instructions\n\n<INSTRUCTIONS>", with no
    # "for <path>" clause -- the 0.157.1 shape memriver already knew
    codex = tmp_path / "x.jsonl"
    xml = "<task>Fix PR #1234 in auth.py</task>"
    _write(codex, [{"timestamp": AT, "type": "response_item", "ordinal": 1, "payload": {
        "type": "message", "role": "user", "content": [
            {"type": "input_text",
             "text": "# AGENTS.md instructions\n\n<INSTRUCTIONS>be terse</INSTRUCTIONS>"},
            {"type": "input_text", "text": xml}]}}])
    assert [r.text for r in CodexTranscripts(tool_output_chars=100).read(
        _session("codex", codex)).records] == [xml]


@pytest.mark.parametrize("hook_prompt", [
    "<hook_prompt session-start>run the checks</hook_prompt>",
    "<hook_prompt>run the checks</hook_prompt>",
    '<hook_prompt attr="x">run the checks</hook_prompt>',
])
def test_a_hook_prompt_block_is_dropped(tmp_path, hook_prompt):
    codex = tmp_path / "x.jsonl"
    xml = "<task>Fix PR #1234 in auth.py</task>"
    _write(codex, [{"timestamp": AT, "type": "response_item", "ordinal": 1, "payload": {
        "type": "message", "role": "user", "content": [
            {"type": "input_text", "text": hook_prompt},
            {"type": "input_text", "text": xml}]}}])
    assert [r.text for r in CodexTranscripts(tool_output_chars=100).read(
        _session("codex", codex)).records] == [xml]


@pytest.mark.parametrize("near_miss", [
    "<hook_prompt_examples>a real prompt that starts with this tag name</hook_prompt_examples>",
    "<hook_prompter>another real prompt sharing the tag's prefix</hook_prompter>",
])
def test_a_tag_that_merely_shares_the_hook_prompt_prefix_is_kept(tmp_path, near_miss):
    codex = tmp_path / "x.jsonl"
    _write(codex, [{"timestamp": AT, "type": "response_item", "ordinal": 1, "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": near_miss}]}}])
    assert [r.text for r in CodexTranscripts(tool_output_chars=100).read(
        _session("codex", codex)).records] == [near_miss]


def test_a_real_prompt_mentioning_agents_md_mid_text_is_kept(tmp_path):
    codex = tmp_path / "x.jsonl"
    text = "can you update the AGENTS.md instructions for the team while you're at it"
    _write(codex, [{"timestamp": AT, "type": "response_item", "ordinal": 1, "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": text}]}}])
    assert [r.text for r in CodexTranscripts(tool_output_chars=100).read(
        _session("codex", codex)).records] == [text]


@pytest.mark.parametrize("reader, harness, bad", [
    (ClaudeTranscripts, "claude-code",
     {"type": "user", "timestamp": AT, "message": {"role": "user", "content": 1}}),
    (ClaudeTranscripts, "claude-code", {"type": "user", "timestamp": AT, "message": "text"}),
    (CodexTranscripts, "codex", {"timestamp": AT, "type": "response_item",
                                 "payload": {"type": "message", "role": "user", "content": 1}}),
    (CodexTranscripts, "codex", {"timestamp": AT, "type": "response_item",
                                 "payload": {"type": "function_call_output", "call_id": 5,
                                             "output": {"x": 1}}}),
])
def test_a_record_of_an_unexpected_shape_is_skipped_and_the_next_one_read(tmp_path, reader,
                                                                          harness, bad):
    good = CLAUDE[0] if harness == "claude-code" else CODEX[2]
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps(bad) + "\n" + "[" * 100_000 + "]" * 100_000 + "\n"
                    + json.dumps(good) + "\n", encoding="utf-8")
    records = reader(tool_output_chars=100).read(_session(harness, path)).records
    assert records[-1].text in ("fix the login bug on branch fix-login", "speed up the build")


def test_the_harness_reader_dispatches_on_the_session_harness(tmp_path):
    claude, codex = tmp_path / "c.jsonl", tmp_path / "x.jsonl"
    _write(claude, CLAUDE[:1])
    _write(codex, CODEX)
    reader = HarnessTranscripts(tool_output_chars=100)
    assert reader.read(_session("claude-code", claude)).records[0].text.startswith("fix the")
    assert reader.read(_session("codex", codex)).records[0].text == "speed up the build"
