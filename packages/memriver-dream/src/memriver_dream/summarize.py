"""Phase 1: a searchable summary for each session that has gone idle (spec §6).

Long sessions are chunked by time within the input room, each chunk is
summarized, and the partial summaries are merged. A session that needs more
calls than one run allows keeps a checkpoint and is finished by later runs;
a summary is stored only when every chunk was covered.
"""

from __future__ import annotations

import dataclasses
import hashlib

from memriver_core import ContentRejected
from memriver_core.models import Session, SummaryInput, SummaryProgress
from memriver_core.settings import (
    DREAM_CHUNK_SUMMARY_CHARS,
    DREAM_MAX_CALLS_PER_SESSION,
    DREAM_MAX_ROOM_HALVINGS,
    DREAM_SUMMARY_MAX_CHARS,
)

from .budget import cut, estimate_tokens
from .calls import DATA_RULE, PROMPT_VERSION, call
from .protocols import Record, Run
from .report import PhaseReport

OMITTED = "[omitted]"
CUT_MARK = " [cut]"
# stands in a checkpoint for chunks with nothing worth keeping (a checkpoint holds at
# least one partial); never sent to a model
NOTHING_KEPT = "[nothing kept]"

SYSTEM_PROMPT = (
    "You summarize one coding-agent session so that it can be found and resumed later. "
    "Cover the goal, what was done, the results and what is still open. Keep file names, "
    "branches, PR numbers, commands and error names exactly as written. Write in the "
    "session's own language. Keep what was only planned apart from what was done.")
CHUNK_PROMPT = ("Summarize this consecutive part of the session in at most {limit} "
                "characters; answer an empty summary when nothing in it is worth finding "
                "again.\n\n<session-part>\n{body}\n</session-part>")
MERGE_PROMPT = ("Merge these consecutive partial summaries, in order, into one summary of at "
                "most {limit} characters.\n\n<partial-summaries>\n{body}\n</partial-summaries>")
FINAL_PROMPT = ("Write the summary of the whole session in at most {limit} characters. Answer "
                "status \"empty\" with an empty summary when nothing in it is worth finding "
                "again.\n\n<{tag}>\n{body}\n</{tag}>")
CHUNK_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["summary"],
                "properties": {"summary": {"type": "string"}}}
FINAL_SCHEMA = {"type": "object", "additionalProperties": False,
                "required": ["status", "summary"],
                "properties": {"status": {"type": "string", "enum": ["ok", "empty"]},
                               "summary": {"type": "string"}}}


class _Stop(Exception):
    """Ends one session's attempt with an outcome; nothing final is stored."""

    def __init__(self, outcome: str) -> None:
        super().__init__(outcome)
        self.outcome = outcome


def input_room(budget_tokens: int) -> int:
    """The tokens one call may spend on the session itself, after the fixed prompts."""
    return budget_tokens - estimate_tokens(SYSTEM_PROMPT + DATA_RULE + FINAL_PROMPT)


def plan_chunks(lines: list[str], room: int) -> list[str]:
    """Lines in order, packed into chunks of at most `room` tokens; a line longer than
    a chunk is cut at character boundaries, each piece marked."""
    chunks: list[str] = []
    current: list[str] = []
    used = 0
    for line in lines:
        pieces = [line] if estimate_tokens(line) < room else [
            piece + CUT_MARK for piece in cut(line, room - estimate_tokens(CUT_MARK) - 1)]
        for piece in pieces:
            cost = estimate_tokens(piece) + 1            # and the newline joining it
            if current and used + cost > room:
                chunks.append("\n".join(current))
                current, used = [], 0
            current.append(piece)
            used += cost
    if current:
        chunks.append("\n".join(current))
    return chunks


def _sent(run: Run, text: str, fallback: str) -> str:
    """`text` as it may be sent: a lone surrogate (no codec takes it) becomes "?", and
    text the content policy refuses becomes `fallback`."""
    text = text.encode("utf-8", "replace").decode("utf-8")
    return text if run.maintenance.text_passes_policy(text) else fallback


def _line(run: Run, record: Record) -> str:
    return f"[{_sent(run, record.at or '-', '-')}] {record.kind}: " \
           f"{_sent(run, record.text, OMITTED)}"


def input_fingerprint(lines: list[str]) -> str:
    """What a checkpoint is bound to: the filtered lines actually chunked, so a policy
    change that alters what is sent (and where chunks break) invalidates it."""
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


class _Attempt:
    """One pass over a session's chunks at one room size, within one run's calls."""

    def __init__(self, run: Run, session: Session, fingerprint: str, room: int,
                 calls: list[int]) -> None:
        self.run, self.session, self.fingerprint, self.room = run, session, fingerprint, room
        self.calls = calls                  # [made], shared across room sizes
        self.next_chunk = 0
        self.partials: list[str] = []

    def _spend(self) -> None:
        """One call more, or -- out of calls -- keep what is covered and stop."""
        if self.calls[0] >= DREAM_MAX_CALLS_PER_SESSION:
            self._checkpoint()
            raise _Stop("partial")
        self.calls[0] += 1

    def _checkpoint(self) -> None:
        if self.next_chunk == 0:            # out of calls before covering anything here
            raise _Stop("incomplete")
        progress = SummaryProgress(self.fingerprint, PROMPT_VERSION, self.room,
                                   self.next_chunk, tuple(self.partials) or (NOTHING_KEPT,))
        try:
            written = self.run.maintenance.write_summary_progress(
                self.session.key, expected_last_active_at=self.session.last_active_at,
                progress=progress)
        except ContentRejected as err:      # every partial passed already; belt and braces
            raise _Stop("rejected") from err
        if not written:
            raise _Stop("moved")

    def _partial(self, template: str, body: str, *, may_be_empty: bool = False) -> str:
        """One partial summary; empty only where `may_be_empty` (a chunk with nothing
        worth keeping)."""
        self._spend()
        result = call(self.run.executor, system_prompt=SYSTEM_PROMPT,
                      prompt=template.format(limit=DREAM_CHUNK_SUMMARY_CHARS, body=body),
                      schema=CHUNK_SCHEMA)
        if isinstance(result, str):
            raise _Stop(result)
        text = result["summary"]
        if not text.strip() and may_be_empty:
            return ""
        if not text.strip() or len(text) > DREAM_CHUNK_SUMMARY_CHARS:
            raise _Stop("schema")
        # nothing a policy refuses is fed to another call or stored (spec §6)
        if not self.run.maintenance.text_passes_policy(text):
            raise _Stop("rejected")
        return text

    def _merge(self) -> None:
        self.partials = [self._partial(MERGE_PROMPT, "\n".join(self.partials))]

    def summarize(self, lines: list[str]) -> dict:
        chunks = plan_chunks(lines, self.room)
        if len(chunks) == 1:
            return self._final(chunks[0], "session")
        progress = self.session.summary_progress
        # a checkpoint that reached here is for this input and prompt (_resumable); it
        # is resumed only at the room it was planned with
        if progress is not None and progress.room == self.room \
                and progress.next_chunk <= len(chunks):
            self.next_chunk = progress.next_chunk
            self.partials = [p for p in progress.partials if p != NOTHING_KEPT]
        while self.next_chunk < len(chunks):
            if partial := self._partial(CHUNK_PROMPT, chunks[self.next_chunk],
                                        may_be_empty=True):
                self.partials.append(partial)
            self.next_chunk += 1
            # bounded: the partials are merged before they could outgrow half the room
            if len(self.partials) > 1 \
                    and estimate_tokens("\n".join(self.partials)) > self.room // 2:
                self._merge()
        if not self.partials:               # every chunk covered, none worth keeping
            return {"status": "empty", "summary": ""}
        while len(rounds := plan_chunks(self.partials, self.room)) > 1:
            self.partials = [self._partial(MERGE_PROMPT, chunk) for chunk in rounds]
        return self._final(rounds[0], "partial-summaries")

    def _final(self, body: str, tag: str) -> dict:
        self._spend()
        result = call(self.run.executor, system_prompt=SYSTEM_PROMPT,
                      prompt=FINAL_PROMPT.format(limit=DREAM_SUMMARY_MAX_CHARS, tag=tag,
                                                 body=body),
                      schema=FINAL_SCHEMA)
        if isinstance(result, str):
            raise _Stop(result)
        if result["status"] == "ok" and not (
                0 < len(result["summary"].strip()) <= DREAM_SUMMARY_MAX_CHARS):
            raise _Stop("schema")
        return result


def _resumable(run: Run, progress: SummaryProgress, fingerprint: str) -> bool:
    return (progress.fingerprint, progress.prompt_version) == (fingerprint, PROMPT_VERSION) \
        and all(run.maintenance.text_passes_policy(p) for p in progress.partials)


def _summarize(run: Run, session: Session, lines: list[str], fingerprint: str) -> dict | str:
    """The final {status, summary}, or the outcome of an attempt that stored nothing
    final ("partial" after keeping a checkpoint)."""
    progress = session.summary_progress
    # discarded, not only ignored, when it can never be resumed: other input or prompt,
    # or a partial the current policy refuses (it would be sent again); a checkpoint
    # planned at another room is kept, since a "too-large" answer may shrink this
    # run's room to it
    if progress is not None and not _resumable(run, progress, fingerprint):
        if not run.maintenance.write_summary_progress(
                session.key, expected_last_active_at=session.last_active_at, progress=None):
            return "moved"
        session = dataclasses.replace(session, summary_progress=None)
    room, calls = input_room(run.budget_tokens), [0]
    for _ in range(DREAM_MAX_ROOM_HALVINGS + 1):
        try:
            return _Attempt(run, session, fingerprint, room, calls).summarize(lines)
        except _Stop as stop:
            if stop.outcome != "too-large":
                return stop.outcome
            room //= 2                      # the estimate fell short: a smaller room
    return "too-large"


def _store(run: Run, session: Session, status: str, text: str | None,
           snapshot: SummaryInput) -> str:
    stored = run.maintenance.write_summary(session.key,
                                           expected_last_active_at=session.last_active_at,
                                           summary=text, status=status,
                                           summary_input=snapshot)
    return stored or "moved"                # the outcome actually stored


def _restamp(run: Run, session: Session, outcome: str,
             snapshot: SummaryInput | None = None) -> str:
    """Keep the stored outcome but mark it current, so the session stops being due;
    report what was actually stored (the policy may refuse a kept summary now)."""
    stored = run.maintenance.write_summary(session.key,
                                           expected_last_active_at=session.last_active_at,
                                           summary=session.summary,
                                           status=session.summary_status,
                                           summary_input=snapshot or session.summary_input)
    if stored is None:
        return "moved"
    return outcome if stored == session.summary_status else stored


def summarize_session(run: Run, session: Session) -> str:
    run.maintenance.mark_summary_attempt(session.key)
    try:
        transcript = run.transcripts.read(session)
    except Exception:  # noqa: BLE001 - a bad transcript fails its own session, never the run
        return "unreadable"
    if transcript is None:
        if session.summary_status == "ok":
            return _restamp(run, session, "kept")    # the transcript went; the summary stays
        return _store(run, session, "failed", None, SummaryInput("", 0, False))
    stored = session.summary_input
    snapshot = SummaryInput(transcript.fingerprint, len(transcript.records),
                            transcript.complete)
    if stored is not None and stored.fingerprint == transcript.fingerprint:
        if stored.complete:
            return _restamp(run, session, "unchanged")
        if transcript.complete:             # the open line went: same records, now whole
            return _restamp(run, session, "unchanged", snapshot)
        return "waiting"                    # the last line is still open: retried later
    lines = [_line(run, record) for record in transcript.records]
    if not lines:
        return _store(run, session, "empty", None, snapshot)
    result = _summarize(run, session, lines, input_fingerprint(lines))
    if isinstance(result, str):
        return result                       # retried by the next run
    status = result["status"]
    return _store(run, session, status, result["summary"] if status == "ok" else None, snapshot)


def run(run: Run, phase: PhaseReport) -> None:
    for session in run.maintenance.sessions_due_for_summary(run.now, run.dream.idle_minutes,
                                                            run.dream.max_sessions_per_run):
        outcome = summarize_session(run, session)
        key = session.key
        phase.record(outcome, {"harness": key.harness, "session_id": key.session_id,
                               "status": outcome})
        run.log(f"summarize {key.harness}/{key.session_id}: {outcome}")
