"""Transcript readers for `memriver dream`: a harness's session file as records.

Both readers keep user prompts in full and assistant text, shorten tool
outputs, keep compaction summaries and titles as their own kinds, drop
sub-agent and harness-injected records, ignore an unfinished last line, and
never open anything a transcript names. Claude Code writes one JSON object
per line (user/assistant records carry `message.content`; `isSidechain`
marks sub-agent records, `isMeta` injected ones, `isCompactSummary` a
compaction summary; `ai-title` a title). Codex writes a rollout of
`{timestamp, type, payload}` lines (`response_item` messages and tool calls,
`compacted` summaries).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from typing import Any

from memriver_core.models import Session
from memriver_dream.calls import storable
from memriver_dream.protocols import Record, Transcript

CUT_MARK = " [cut]"
# the context Codex injects as user message blocks -- known wrappers only: a user's
# own markup (<task>...</task>) is a prompt like any other
_CODEX_INJECTED = ("<environment_context>", "<user_instructions>",
                   "# AGENTS.md instructions for ")


def _read(path: str | None) -> tuple[list[dict], str, bool] | None:
    """The file's complete JSON-object lines, a fingerprint of the bytes they came from,
    and whether the file ended on a line boundary; None when there is nothing to read."""
    if not path or not os.path.isabs(path):
        return None
    try:
        # O_NONBLOCK: a FIFO planted at the path must not hang the run
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        # checked on the descriptor before anything reads it: a directory or FIFO
        # is no transcript
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        # ponytail: the whole file in memory; fine for session transcripts --
        # stream it line by line if multi-gigabyte transcripts show up
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read()
    except OSError:
        return None
    finally:
        os.close(fd)
    complete = not data or data.endswith(b"\n")
    body = data if complete else data[:data.rfind(b"\n") + 1]
    objects: list[dict] = []
    for line in body.splitlines():
        try:
            value = json.loads(line)
        except (ValueError, RecursionError):
            continue                            # UnicodeDecodeError and nesting too deep included
        if isinstance(value, dict):
            objects.append(value)
    return objects, hashlib.sha256(body).hexdigest(), complete


def _texts(content: Any) -> str:
    """A content value as text: a string, or the `text` of each item of a list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(item["text"] for item in content
                         if isinstance(item, dict) and isinstance(item.get("text"), str))
    return ""


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


class _Reader:
    def __init__(self, *, tool_output_chars: int) -> None:
        self._tool_output_chars = tool_output_chars

    def _record(self, kind: str, at: str | None, text: str) -> Record | None:
        # a lone surrogate is valid JSON but no UTF-8 column takes it (memriver_dream
        # .calls.storable): such a record's shape is as unexpected as a missing field
        return Record(kind, at, text) if storable(text) else None

    def _tool(self, at: str | None, name: str, output: str) -> Record | None:
        limit = self._tool_output_chars
        text = output if len(output) <= limit else output[:limit] + CUT_MARK
        return self._record("tool", at, f"{name}: {text}")

    def read(self, session: Session) -> Transcript | None:
        read = _read(session.transcript_path)
        if read is None:
            return None
        objects, fingerprint, complete = read
        return Transcript(tuple(self._records(objects)), fingerprint, complete)

    def _records(self, objects: list[dict]) -> list[Record]:
        raise NotImplementedError


class ClaudeTranscripts(_Reader):
    def _records(self, objects: list[dict]) -> list[Record]:
        records: list[Record] = []
        tools: dict[str, str] = {}
        at: str | None = None
        for obj in objects:
            at = _string(obj.get("timestamp")) or at
            kind = obj.get("type")
            if kind == "ai-title" and _string(obj.get("aiTitle")):
                record = self._record("title", at, obj["aiTitle"])
                if record is not None:
                    records.append(record)
                continue
            if kind not in ("user", "assistant") or obj.get("isSidechain") is True:
                continue
            message = obj.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if kind == "user" and obj.get("isCompactSummary") is True:
                record = self._record("compact", at, _texts(content))
                if record is not None:
                    records.append(record)
                continue
            if kind == "user" and obj.get("isMeta") is True:
                continue                    # injected by the harness, not typed by the user
            if isinstance(content, str):
                if content.strip():
                    record = self._record(kind, at, content)
                    if record is not None:
                        records.append(record)
                continue
            for block in content if isinstance(content, list) else ():
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text" and (_string(block.get("text")) or "").strip():
                    record = self._record(kind, at, block["text"])
                    if record is not None:
                        records.append(record)
                elif block_type == "tool_use" and _string(block.get("id")):
                    tools[block["id"]] = _string(block.get("name")) or "tool"
                elif block_type == "tool_result":
                    name = tools.get(_string(block.get("tool_use_id")) or "", "tool")
                    record = self._tool(at, name, _texts(block.get("content")))
                    if record is not None:
                        records.append(record)
        return records


class CodexTranscripts(_Reader):
    def _records(self, objects: list[dict]) -> list[Record]:
        records: list[Record] = []
        tools: dict[str, str] = {}
        for obj in objects:
            at = _string(obj.get("timestamp"))
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            if obj.get("type") == "compacted" and _string(payload.get("message")):
                record = self._record("compact", at, payload["message"])
                if record is not None:
                    records.append(record)
                continue
            if obj.get("type") != "response_item":
                continue
            item = payload.get("type")
            if item == "message" and payload.get("role") in ("user", "assistant"):
                role = payload["role"]
                content = payload.get("content")
                for block in content if isinstance(content, list) else ():
                    text = _string(block.get("text")) if isinstance(block, dict) else None
                    if not text or not text.strip() or (
                            role == "user" and text.lstrip().startswith(_CODEX_INJECTED)):
                        continue
                    record = self._record(role, at, text)
                    if record is not None:
                        records.append(record)
            elif (item in ("function_call", "custom_tool_call")
                  and _string(payload.get("call_id"))):
                tools[payload["call_id"]] = _string(payload.get("name")) or "tool"
            elif (item in ("function_call_output", "custom_tool_call_output")
                  and _string(payload.get("call_id"))
                  and isinstance(payload.get("output"), (str, list))):
                name = tools.get(payload["call_id"], "tool")
                record = self._tool(at, name, _texts(payload["output"]))
                if record is not None:
                    records.append(record)
        return records


class HarnessTranscripts:
    """The reader for each session's own harness."""

    def __init__(self, *, tool_output_chars: int) -> None:
        self._readers: dict[str, _Reader] = {
            "claude-code": ClaudeTranscripts(tool_output_chars=tool_output_chars),
            "codex": CodexTranscripts(tool_output_chars=tool_output_chars)}

    def read(self, session: Session) -> Transcript | None:
        return self._readers[session.key.harness].read(session)
