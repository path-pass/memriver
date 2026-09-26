"""Phase 1 over a real store, a scripted executor and synthetic transcripts."""

from __future__ import annotations

import re

from memriver_core.models import SessionKey, SummaryInput, now, timestamp_shift
from memriver_core.settings import SESSION_SUMMARY_MAX_CHARS
from memriver_dream.protocols import ExecutorResult, Record, Transcript
from memriver_dream.report import PhaseReport
from memriver_dream.run import run_dream
from memriver_dream.summarize import (
    CUT_MARK,
    NOTHING_KEPT,
    OMITTED,
    input_fingerprint,
    input_room,
    plan_chunks,
    run,
)

SECRET = "token ghp_" + "a" * 36
AT = "2026-09-25T10:00:00.000Z"


def _session(world, name: str = "s1") -> SessionKey:
    key = SessionKey("codex", name)
    world.service.start_session(key, source="startup", entry_dir=world.project.root,
                                transcript_path=None)
    return key


def _later(world, **overrides):
    """A run one idle period after the sessions went quiet."""
    return world.run(now=timestamp_shift(now(), minutes=61), **overrides)


def _transcript(*texts: str, fingerprint: str = "fp1", complete: bool = True) -> Transcript:
    return Transcript(tuple(Record("user" if i % 2 == 0 else "assistant", AT, text)
                            for i, text in enumerate(texts)), fingerprint, complete)


def _stored(world, key: SessionKey):
    return next(s for s in world.service.list_sessions() if s.key == key)


def _phase(world, **overrides) -> PhaseReport:
    phase = PhaseReport()
    run(_later(world, **overrides), phase)
    return phase


_IDENTIFIERS = re.compile(r"PR #\d+|branch [a-z-]+|[a-z_]+\.py")


def _echo(prompt, schema):
    """A model that keeps exactly the identifiers it was shown."""
    found = sorted(set(_IDENTIFIERS.findall(prompt))) or ["nothing"]
    summary = "; ".join(found)
    return ({"status": "ok", "summary": summary} if "status" in schema["properties"]
            else {"summary": summary})


# eighteen records that each fill most of a 100-token room: one chunk per record, so
# the session needs 18 map calls and a final one -- more than one run's 12 calls
LONG = [f"step {i}: PR #{100 + i} " + "x" * 180 for i in range(18)]
ROOM_100 = 100 - input_room(0)                 # the budget whose input room is 100 tokens


def test_a_short_session_takes_one_call_and_stores_its_summary(world):
    key = _session(world)
    world.transcripts.by_session["s1"] = _transcript("fix the flaky test in tests/test_x.py",
                                                     "fixed it")
    world.executor.replies = [{"status": "ok", "summary": "Fixed tests/test_x.py"}]
    phase = _phase(world)
    stored = _stored(world, key)
    assert (stored.summary_status, stored.summary) == ("ok", "Fixed tests/test_x.py")
    assert stored.summary_input == SummaryInput("fp1", 2, True)
    assert len(world.executor.calls) == 1
    assert "tests/test_x.py" in world.executor.calls[0]["prompt"]
    assert phase.items == [{"harness": "codex", "session_id": "s1", "status": "ok"}]


def test_empty_and_failed_outcomes_store_no_text_and_are_reported_as_stored(world):
    empty, missing = _session(world, "empty"), _session(world, "missing")
    world.transcripts.by_session["empty"] = _transcript("hi")
    world.executor.replies = [{"status": "empty", "summary": ""}]
    phase = _phase(world)
    assert (_stored(world, empty).summary_status, _stored(world, empty).summary) == ("empty",
                                                                                     None)
    assert _stored(world, missing).summary_status == "failed"
    assert phase.as_json()["outcomes"] == {"empty": 1, "failed": 1}
    assert phase.as_json()["failed"] == 1


def test_a_summary_the_policy_refuses_is_stored_and_reported_as_omitted(world):
    key = _session(world)
    world.transcripts.by_session["s1"] = _transcript("work")
    world.executor.replies = [{"status": "ok", "summary": "Used " + SECRET}]
    assert _phase(world).outcomes == {"omitted": 1}
    assert (_stored(world, key).summary_status, _stored(world, key).summary) == ("omitted", None)


def test_a_vanished_transcript_keeps_an_ok_summary_and_stops_being_due(world):
    key = _session(world)
    world.transcripts.by_session["s1"] = _transcript("work")
    world.executor.replies = [{"status": "ok", "summary": "Did the work"}]
    _phase(world)
    world.service.start_session(key, source="resume", entry_dir=world.project.root,
                                transcript_path=None)          # active again
    del world.transcripts.by_session["s1"]
    phase = _phase(world)
    assert phase.outcomes == {"kept": 1}
    assert _stored(world, key).summary == "Did the work"
    assert world.maintenance.sessions_due_for_summary(timestamp_shift(now(), minutes=61), 60,
                                                      10) == []


def test_an_unchanged_transcript_is_not_summarized_again(world):
    key = _session(world)
    world.transcripts.by_session["s1"] = _transcript("work")
    world.executor.replies = [{"status": "ok", "summary": "Did the work"}]
    _phase(world)
    world.service.start_session(key, source="resume", entry_dir=world.project.root,
                                transcript_path=None)
    phase = _phase(world)
    assert phase.outcomes == {"unchanged": 1}
    assert len(world.executor.calls) == 1


def test_a_failed_call_stores_nothing_and_the_session_stays_due(world):
    key = _session(world)
    world.transcripts.by_session["s1"] = _transcript("work")
    world.executor.replies = [ExecutorResult(error="quota")]
    phase = _phase(world)
    assert phase.outcomes == {"quota": 1}
    assert _stored(world, key).summary_status is None
    assert _stored(world, key).summary_attempted_at is not None


def test_activity_during_the_call_loses_the_compare_and_set(world):
    key = _session(world)
    world.transcripts.by_session["s1"] = _transcript("work")

    def touch_then_answer(prompt, schema):
        world.service.start_session(key, source="resume", entry_dir=world.project.root,
                                    transcript_path=None)
        return {"status": "ok", "summary": "Did the work"}

    world.executor.replies = [touch_then_answer]
    assert _phase(world).outcomes == {"moved": 1}
    assert _stored(world, key).summary_status is None


def test_a_secret_in_a_record_never_reaches_the_executor_prompt(world):
    _session(world)
    world.transcripts.by_session["s1"] = _transcript("deploy with " + SECRET, "done")
    world.executor.replies = [{"status": "ok", "summary": "Deployed"}]
    _phase(world)
    prompt = world.executor.calls[0]["prompt"]
    assert OMITTED in prompt and "ghp_" not in prompt


def test_a_long_multi_task_session_is_chunked_and_keeps_the_early_task_identifiers(world):
    key = _session(world)
    early = ["Task one: fix login on branch fix-login, see PR #101", "Patched auth.py"]
    filler = [f"step {i}: " + "routine output " * 20 for i in range(40)]
    late = ["Task two: speed up the build", "Cached deps in build.py"]
    world.transcripts.by_session["s1"] = _transcript(*early, *filler, *late)
    world.executor.default = _echo
    _phase(world, budget_tokens=900)
    assert 3 <= len(world.executor.calls) <= 12       # map calls, then the final call
    summary = _stored(world, key).summary
    for identifier in ("PR #101", "branch fix-login", "auth.py", "build.py"):
        assert identifier in summary


def test_a_session_over_one_runs_call_budget_is_checkpointed_and_finished_next_run(world):
    key = _session(world)
    world.transcripts.by_session["s1"] = _transcript(*LONG)
    world.executor.default = _echo
    assert _phase(world, budget_tokens=ROOM_100).outcomes == {"partial": 1}
    assert len(world.executor.calls) == 12
    progress = _stored(world, key).summary_progress
    assert (progress.fingerprint, progress.next_chunk) == (
        input_fingerprint([f"[{AT}] {r.kind}: {r.text}" for r in _transcript(*LONG).records]),
        12)
    assert "PR #100" in progress.partials[0]
    assert _phase(world, budget_tokens=ROOM_100).outcomes == {"ok": 1}
    assert len(world.executor.calls) == 12 + 7          # six more chunks and the final call
    stored = _stored(world, key)
    assert "PR #100" in stored.summary and "PR #117" in stored.summary
    assert stored.summary_progress is None


def test_a_checkpoint_for_other_input_is_ignored(world):
    key = _session(world)
    world.transcripts.by_session["s1"] = _transcript(*LONG)
    world.executor.default = _echo
    _phase(world, budget_tokens=ROOM_100)
    first = _stored(world, key).summary_progress.fingerprint
    world.transcripts.by_session["s1"] = _transcript(*LONG[:-1], LONG[-1] + " more",
                                                     fingerprint="fp2")
    assert _phase(world, budget_tokens=ROOM_100).outcomes == {"partial": 1}
    assert len(world.executor.calls) == 24                # started over
    assert _stored(world, key).summary_progress.fingerprint != first


def test_a_checkpointed_partial_the_policy_now_refuses_is_never_sent_again(world,
                                                                           monkeypatch):
    key = _session(world)
    world.transcripts.by_session["s1"] = _transcript(*LONG)
    world.executor.default = _echo
    _phase(world, budget_tokens=ROOM_100)
    assert _stored(world, key).summary_progress.partials[0] == "PR #100"
    # the policy changes: the first stored partial is refused, no record is
    monkeypatch.setattr(world.maintenance, "text_passes_policy",
                        lambda text: text != "PR #100")
    assert _phase(world, budget_tokens=ROOM_100).outcomes == {"rejected": 1}
    later = world.executor.calls[12:]
    assert len(later) == 1                                # started over, from chunk one
    assert all("<partial-summaries>" not in call["prompt"] for call in later)


def test_a_policy_change_that_moves_chunk_boundaries_starts_over_and_covers_the_rest(
        world, monkeypatch):
    key = _session(world)
    world.transcripts.by_session["s1"] = _transcript(*LONG)
    world.executor.default = _echo
    _phase(world, budget_tokens=ROOM_100)
    first = _stored(world, key).summary_progress.fingerprint
    # a record past the checkpoint is now refused: it becomes "[omitted]" and joins its
    # neighbour's chunk; every stored partial still passes, only the input changed
    monkeypatch.setattr(world.maintenance, "text_passes_policy",
                        lambda text: "PR #115" not in text)
    assert _phase(world, budget_tokens=ROOM_100).outcomes == {"partial": 1}
    assert "step 0:" in world.executor.calls[12]["prompt"]     # from the first chunk again
    assert _stored(world, key).summary_progress.fingerprint != first
    assert _phase(world, budget_tokens=ROOM_100).outcomes == {"ok": 1}
    summary = _stored(world, key).summary
    assert all(f"PR #{100 + i}" in summary for i in range(18) if i != 15)
    assert "PR #115" not in summary


def test_a_partial_summary_the_policy_refuses_is_never_fed_on_or_stored(world):
    key = _session(world)
    world.transcripts.by_session["s1"] = _transcript(*LONG)
    world.executor.default = lambda prompt, schema: {"summary": "Used " + SECRET}
    assert _phase(world, budget_tokens=ROOM_100).outcomes == {"rejected": 1}
    assert len(world.executor.calls) == 1
    stored = _stored(world, key)
    assert (stored.summary_status, stored.summary_progress) == (None, None)


def test_a_failed_session_whose_transcript_comes_back_is_summarized_without_new_activity(
        world):
    key = _session(world)
    assert _phase(world).outcomes == {"failed": 1}
    world.transcripts.by_session["s1"] = _transcript("work")
    world.executor.replies = [{"status": "ok", "summary": "Did the work"}]
    assert _phase(world).outcomes == {"ok": 1}
    assert _stored(world, key).summary == "Did the work"


def test_an_incomplete_snapshot_whose_file_completes_unchanged_is_marked_complete(world):
    key = _session(world)
    world.transcripts.by_session["s1"] = _transcript("work", fingerprint="fpA", complete=False)
    world.executor.replies = [{"status": "ok", "summary": "Did the work"}]
    _phase(world)
    # the open last line was dropped: the same complete records, now a complete file
    world.transcripts.by_session["s1"] = _transcript("work", fingerprint="fpA")
    assert _phase(world).outcomes == {"unchanged": 1}
    assert len(world.executor.calls) == 1
    stored = _stored(world, key)
    assert (stored.summary, stored.summary_input) == ("Did the work",
                                                      SummaryInput("fpA", 1, True))
    assert world.maintenance.sessions_due_for_summary(timestamp_shift(now(), minutes=61), 60,
                                                      10) == []


def test_a_restamp_the_policy_now_refuses_is_reported_as_the_stored_omitted(world):
    key = _session(world)
    world.transcripts.by_session["s1"] = _transcript("work")
    world.executor.replies = [{"status": "ok", "summary": "Did the work"}]
    _phase(world)
    # a stored summary the current policy refuses (planted past the write gate)
    world.sql("UPDATE sessions SET summary = ? WHERE session_id = ?", "Used " + SECRET, "s1")
    world.service.start_session(key, source="resume", entry_dir=world.project.root,
                                transcript_path=None)
    assert _phase(world).outcomes == {"omitted": 1}
    assert (_stored(world, key).summary_status, _stored(world, key).summary) == ("omitted",
                                                                                 None)


def test_an_incomplete_transcript_waits_then_is_summarized_once_it_completes(world):
    key = _session(world)
    world.transcripts.by_session["s1"] = _transcript("work", fingerprint="fpA", complete=False)
    world.executor.replies = [{"status": "ok", "summary": "Started the work"},
                              {"status": "ok", "summary": "Did the work"}]
    assert _phase(world).outcomes == {"ok": 1}
    assert _phase(world).outcomes == {"waiting": 1}           # the last line is still open
    world.transcripts.by_session["s1"] = _transcript("work", "done", fingerprint="fpB")
    assert _phase(world).outcomes == {"ok": 1}
    assert _stored(world, key).summary_input == SummaryInput("fpB", 2, True)


def test_sessions_that_keep_failing_do_not_keep_a_new_one_out(world):
    for name in ("a", "b", "c"):
        _session(world, name)                     # no transcripts: each attempt fails
    _phase(world)
    new = _session(world, "new")
    limited = world.dream.model_copy(update={"max_sessions_per_run": 1})
    assert _phase(world, dream=limited).items == [
        {"harness": "codex", "session_id": "new", "status": "failed"}]
    assert _stored(world, new).summary_status == "failed"


def test_a_transcript_source_that_raises_fails_only_its_session(world):
    _session(world, "bad")
    good = _session(world, "good")

    class Flaky:
        def read(self, session):
            if session.key.session_id == "bad":
                raise TypeError("a transcript shape nobody expected")
            return _transcript("work")

    world.executor.replies = [{"status": "ok", "summary": "Did the work"}]
    phase = _phase(world, transcripts=Flaky())
    assert phase.as_json()["outcomes"] == {"ok": 1, "unreadable": 1}
    assert _stored(world, good).summary == "Did the work"


def test_a_too_large_answer_shrinks_the_input(world):
    _session(world)
    world.transcripts.by_session["s1"] = _transcript(*LONG[:4])
    world.executor.replies = [ExecutorResult(error="too-large")]
    world.executor.default = _echo
    # four ~60-token records fit one 300-token room; the halved room takes two per chunk
    assert _phase(world, budget_tokens=300 - input_room(0)).outcomes == {"ok": 1}
    first, second = world.executor.calls[0]["prompt"], world.executor.calls[1]["prompt"]
    assert len(second) < len(first)


def test_plan_chunks_packs_lines_in_order_and_cuts_one_too_large_for_a_chunk():
    chunks = plan_chunks(["a" * 40, "b" * 40, "c" * 400], room=30)
    assert chunks[0].startswith("a") and "b" * 40 in "".join(chunks)
    assert any(CUT_MARK in chunk for chunk in chunks)
    assert "".join(chunks).replace(CUT_MARK, "").count("c") == 400


def test_run_dream_runs_the_summarize_phase(world):
    key = _session(world)
    world.transcripts.by_session["s1"] = _transcript("work")
    world.executor.replies = [{"status": "ok", "summary": "Did the work"}]
    report = run_dream(world.maintenance, world.executor, world.transcripts, world.store,
                       world.dream, timestamp_shift(now(), minutes=61),
                       phases=("summarize",))
    assert report.phases["summarize"].outcomes == {"ok": 1}
    assert _stored(world, key).summary == "Did the work"


def _refuse_first_partial(world, monkeypatch):
    """A first run leaves a checkpoint, then the policy refuses its first partial."""
    key = _session(world)
    world.transcripts.by_session["s1"] = _transcript(*LONG)
    world.executor.default = _echo
    _phase(world, budget_tokens=ROOM_100)
    assert _stored(world, key).summary_progress.partials[0] == "PR #100"
    monkeypatch.setattr(world.maintenance, "text_passes_policy",
                        lambda text: text != "PR #100")
    return key


def test_a_checkpoint_with_a_refused_partial_is_discarded_even_when_the_run_fails(
        world, monkeypatch):
    key = _refuse_first_partial(world, monkeypatch)
    world.executor.replies = [ExecutorResult(error="quota")]
    assert _phase(world, budget_tokens=ROOM_100).outcomes == {"quota": 1}
    assert _stored(world, key).summary_progress is None


def test_a_checkpoint_for_other_input_is_discarded_even_when_the_run_fails(world):
    key = _session(world)
    world.transcripts.by_session["s1"] = _transcript(*LONG)
    world.executor.default = _echo
    _phase(world, budget_tokens=ROOM_100)
    world.transcripts.by_session["s1"] = _transcript(*LONG[:-1], fingerprint="fp2")
    world.executor.replies = [ExecutorResult(error="quota")]
    assert _phase(world, budget_tokens=ROOM_100).outcomes == {"quota": 1}
    assert _stored(world, key).summary_progress is None


def test_discarding_a_checkpoint_of_a_session_that_moved_on_writes_nothing(world,
                                                                          monkeypatch):
    key = _refuse_first_partial(world, monkeypatch)
    kept = _stored(world, key).summary_progress
    calls = len(world.executor.calls)

    class Touching:
        def read(self, session):
            world.service.start_session(key, source="resume", entry_dir=world.project.root,
                                        transcript_path=None)
            return _transcript(*LONG)

    assert _phase(world, budget_tokens=ROOM_100,
                  transcripts=Touching()).outcomes == {"moved": 1}
    assert _stored(world, key).summary_progress == kept
    assert len(world.executor.calls) == calls


def _empty_where(marker: str):
    """A model with nothing to keep from any part containing `marker`."""
    def answer(prompt, schema):
        if "status" not in schema["properties"] and marker in prompt \
                and "<session-part>" in prompt:
            return {"summary": ""}
        return _echo(prompt, schema)
    return answer


def test_a_chunk_with_nothing_worth_keeping_is_covered_and_the_rest_is_kept(world):
    key = _session(world)
    world.transcripts.by_session["s1"] = _transcript(*LONG)
    world.executor.default = _empty_where("step 0:")
    assert _phase(world, budget_tokens=ROOM_100).outcomes == {"partial": 1}
    assert _phase(world, budget_tokens=ROOM_100).outcomes == {"ok": 1}
    summary = _stored(world, key).summary
    assert "PR #100" not in summary and all(f"PR #{100 + i}" in summary
                                            for i in range(1, 18))


def test_a_session_with_nothing_worth_keeping_anywhere_ends_empty(world):
    key = _session(world)
    world.transcripts.by_session["s1"] = _transcript(*LONG)
    world.executor.default = _empty_where("step ")
    assert _phase(world, budget_tokens=ROOM_100).outcomes == {"partial": 1}
    assert _phase(world, budget_tokens=ROOM_100).outcomes == {"empty": 1}
    assert len(world.executor.calls) == 18               # every chunk once, no final call
    stored = _stored(world, key)
    assert (stored.summary_status, stored.summary_progress) == ("empty", None)
    assert all(NOTHING_KEPT not in call["prompt"] for call in world.executor.calls)


def test_a_secret_in_a_record_timestamp_never_reaches_the_executor_prompt(world):
    _session(world)
    world.transcripts.by_session["s1"] = Transcript((Record("user", SECRET, "work"),), "fp1",
                                                    True)
    world.executor.replies = [{"status": "ok", "summary": "Did the work"}]
    assert _phase(world).outcomes == {"ok": 1}
    assert "ghp_" not in world.executor.calls[0]["prompt"]


def test_a_lone_surrogate_in_a_record_fails_no_other_session(world):
    bad, good = _session(world, "bad"), _session(world, "good")
    world.transcripts.by_session["bad"] = _transcript("work " + chr(0xD800))
    world.transcripts.by_session["good"] = _transcript("work")
    world.executor.default = {"status": "ok", "summary": "Did the work"}
    assert _phase(world).outcomes == {"ok": 2}
    assert _stored(world, bad).summary == _stored(world, good).summary == "Did the work"


def test_a_final_summary_with_a_lone_surrogate_is_invalid_not_a_storage_failure(world):
    # a model may answer with a lone surrogate (valid JSON, no UTF-8 codec takes it);
    # it must never reach write_summary, which would raise StorageFailure and fail the
    # whole run (phases 2-3 never run)
    bad, good = _session(world, "bad"), _session(world, "good")
    world.transcripts.by_session["bad"] = _transcript("bad work")
    world.transcripts.by_session["good"] = _transcript("good work")

    def answer(prompt, schema):
        if "bad work" in prompt:
            return {"status": "ok", "summary": "x" + chr(0xD800)}
        return {"status": "ok", "summary": "Did the work"}

    world.executor.default = answer
    report = run_dream(world.maintenance, world.executor, world.transcripts, world.store,
                       world.dream, timestamp_shift(now(), minutes=61),
                       phases=("summarize",))
    assert report.status == "completed"
    assert report.phases["summarize"].outcomes == {"invalid": 1, "ok": 1}
    assert _stored(world, bad).summary_status is None
    assert _stored(world, good).summary == "Did the work"


def test_a_checkpointed_partial_with_a_lone_surrogate_is_invalid_not_a_storage_failure(world):
    # same rule for a partial (map) summary: it must never be checkpointed via
    # write_summary_progress, which would raise StorageFailure on the same text (and
    # crash the run, along with every other session's summary)
    bad, good = _session(world, "bad"), _session(world, "good")
    world.transcripts.by_session["bad"] = _transcript(*LONG)
    world.transcripts.by_session["good"] = _transcript("good work")
    seen = {"n": 0}

    def answer(prompt, schema):
        if "<session-part>" in prompt and "step 0:" in prompt:
            seen["n"] += 1
            if seen["n"] == 1:
                return {"summary": "x" + chr(0xD800)}
        return _echo(prompt, schema)

    world.executor.default = answer
    phase = _phase(world, budget_tokens=ROOM_100)
    assert phase.outcomes == {"invalid": 1, "ok": 1}
    assert _stored(world, bad).summary_status is None
    assert _stored(world, bad).summary_progress is None
    assert _stored(world, good).summary is not None


def test_a_final_summary_with_trailing_whitespace_at_the_limit_is_stored_stripped(world):
    # the length check strips the text but the raw text (with its trailing
    # whitespace) used to be the one stored
    key = _session(world)
    world.transcripts.by_session["s1"] = _transcript("work")
    text = "x" * (SESSION_SUMMARY_MAX_CHARS - 1) + " "
    assert len(text) == SESSION_SUMMARY_MAX_CHARS
    world.executor.replies = [{"status": "ok", "summary": text}]
    assert _phase(world).outcomes == {"ok": 1}
    assert _stored(world, key).summary == text.strip()
