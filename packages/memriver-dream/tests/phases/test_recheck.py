"""The global layer's source re-check (spec §6.6, U10) over a real store and a
scripted executor: eligibility, what is sent (cited version, current state,
successors), the source_checks gate, every decision's validation, policy skips and
independence from the extraction skip."""

from __future__ import annotations

import json

import pytest
from memriver_core.models.changes import Create, SoftDelete, SourceRef, Update
from memriver_dream.phases import PassResult, recheck
from memriver_dream.protocols import ExecutorResult
from memriver_dream.store import DreamStore, input_digest


def _second(world) -> str:
    directory = world.root.parent / "second"
    directory.mkdir()
    return world.services.project.init_project(
        "second", world.services.project.plan_root(str(directory))).id


def _create(world, project_id: str, body: str, *sources: tuple[str, int]) -> str:
    change = world.services.memory.apply(
        [Create(project_id=project_id, type="feedback", description="principle", body=body,
                sources=tuple(SourceRef(i, v) for i, v in sources))], changed_by="test")
    return change.steps[0].memory_id


def _update(world, memory_id: str, version: int, body: str) -> None:
    world.services.memory.apply([Update(memory_id=memory_id, expected_version=version,
                                        body=body)], changed_by="test")


def _setup(world) -> tuple[str, str, str]:
    """A demo memory, a second-project memory and the global entry citing both."""
    second = _second(world)
    a = world.create(world.project.id, "demo runs pytest")
    b = world.create(second, "second runs pytest")
    entry = _create(world, world.global_id, "Python projects prefer pytest for tests.",
                    (a, 1), (b, 1))
    return a, b, entry


def _retire_into(world, a: str) -> str:
    """Merge-like: a new demo memory citing `a`, and `a` soft-deleted, in one change."""
    change = world.services.memory.apply(
        [Create(project_id=world.project.id, type="feedback", description="merged",
                body="demo runs pytest with xdist", sources=(SourceRef(a, 1),)),
         SoftDelete(memory_id=a, expected_version=1)], changed_by="test")
    return next(step.memory_id for step in change.steps if step.op == "create")


def _decision(decision: str, entry: str, *, replacements=(), description: str = "",
              body: str = "", reason: str = "the change does not touch it") -> dict:
    return {"decision": decision, "id": entry, "description": description, "body": body,
            "replacements": [{"source": s, "by": b} for s, b in replacements],
            "reason": reason}


def _pass(world, **overrides) -> tuple[PassResult, str]:
    ctx = world.context(**overrides)
    result = recheck.run(ctx)
    ctx.report.footer(status="completed", finished_at=world.now)
    text = ctx.report.path.read_text()
    ctx.report.path.unlink()
    return result, text


def _current(world, memory_id: str):
    return max(world.services.memory.versions(memory_id), key=lambda version: version.version)


def _checked(world, entry: str) -> str | None:
    return DreamStore(world.root / "dream" / "dream.db").source_check(entry)


def _entry_of(prompt: str) -> str:
    """The id of the global entry a re-check prompt is about."""
    return json.loads(prompt.split("<global-entry>\n")[1].split("\n</global-entry>")[0])["id"]


def _changes(prompt: str) -> dict[str, dict]:
    body = prompt.split("<changed-sources>\n")[1].split("\n</changed-sources>")[0]
    return {item["source"]: item for item in map(json.loads, body.splitlines())}


def test_an_entry_whose_sources_did_not_change_is_not_sent(world):
    _setup(world)
    result, text = _pass(world)
    assert result == PassResult(finished=True)
    assert world.executor.calls == []
    assert text.startswith("no global entry with a changed source\n")


def test_an_updated_source_is_sent_both_ways_and_a_keep_is_recorded_once(world):
    # §10 item 10: keep re-check recorded and not repeated
    a, _, entry = _setup(world)
    _update(world, a, 1, "demo runs pytest with xdist")
    world.executor.replies = [_decision("keep", entry)]
    result, text = _pass(world)
    assert result == PassResult(finished=True)
    prompt = world.executor.calls[0]["prompt"]
    assert "Python projects prefer pytest for tests." in prompt
    change = _changes(prompt)[a]
    assert (change["cited"]["version"], change["cited"]["body"]) == (1, "demo runs pytest")
    assert (change["current"]["version"], change["current"]["deleted"],
            change["current"]["body"]) == (2, False, "demo runs pytest with xdist")
    assert set(_changes(prompt)) == {a}                  # b did not change
    assert _checked(world, entry) == input_digest([(entry, 1), (a, 1), (a, 2)])
    assert f"keep {entry}: the change does not touch it\n" in text
    result, text = _pass(world)
    assert len(world.executor.calls) == 1
    assert f"{entry}: unchanged since its last keep\n" in text


def test_a_source_changing_again_after_a_keep_reruns_the_recheck(world):   # §10 item 9
    a, _, entry = _setup(world)
    _update(world, a, 1, "demo runs pytest with xdist")
    world.executor.replies = [_decision("keep", entry), _decision("keep", entry)]
    _pass(world)
    _update(world, a, 2, "demo runs pytest with xdist and coverage")
    _pass(world)
    assert len(world.executor.calls) == 2
    assert _checked(world, entry) == input_digest([(entry, 1), (a, 1), (a, 3)])


def test_refresh_points_an_updated_source_at_its_current_version(world):   # §10 item 10
    a, b, entry = _setup(world)
    _update(world, a, 1, "demo runs pytest with xdist")
    world.executor.replies = [_decision("refresh", entry, replacements=[(a, a)])]
    result, text = _pass(world)
    assert result.finished
    current = _current(world, entry)
    assert (current.version, current.body) == (2, "Python projects prefer pytest for tests.")
    assert set(current.sources) == {SourceRef(a, 2), SourceRef(b, 1)}
    assert f"applying refresh {entry} -> change" in text
    assert _checked(world, entry) is None


def test_a_deleted_source_is_sent_with_its_successors_and_refresh_may_cite_one(world):
    # §10 item 10; §6.6's successor rule
    a, b, entry = _setup(world)
    moved_on = _create(world, world.project.id, "once cited a", (a, 1))
    hidden = _create(world, world.project.id, "held back, cites a", (a, 1))
    other_global = _create(world, world.global_id, "another principle", (a, 1))
    successor = _retire_into(world, a)
    world.services.memory.apply([Update(memory_id=moved_on, expected_version=1, sources=())],
                                changed_by="test")
    # other_global has the same changed source, so it is re-checked in the same pass;
    # the order of the two calls follows the ids
    def answer(prompt, schema):
        if _entry_of(prompt) == entry:
            return _decision("refresh", entry, replacements=[(a, successor)])
        return _decision("keep", other_global)

    world.executor.default = answer
    result, _ = _pass(world, excluded={hidden})
    calls = {_entry_of(call["prompt"]): call for call in world.executor.calls}
    assert set(calls) == {entry, other_global}
    change = _changes(calls[entry]["prompt"])[a]
    assert change["current"]["deleted"] is True and change["current"]["body"] == ""
    # current, not deleted, not excluded, not the entry itself, citing a in its current set
    assert {item["id"] for item in change["successors"]} == {successor, other_global}
    assert result.finished
    assert set(_current(world, entry).sources) == {SourceRef(successor, 1), SourceRef(b, 1)}


@pytest.mark.parametrize(("case", "outcome", "field"), [
    ("an unchanged source", "invalid", "replacements"),
    ("an updated source to another memory", "invalid", "replacements"),
    ("another entry's id", "invalid", "id"),
    ("no replacement", "refused", "replacements"),
    ("a source replaced twice", "refused", "replacements"),
    ("revise without text", "refused", "text")])
def test_a_refresh_or_revise_that_fails_validation_changes_nothing(world, case, outcome,
                                                                   field):
    # §10 item 10: naming what was not sent is malformed; none or a double replacement
    # and blank text are refused
    a, b, entry = _setup(world)
    _update(world, a, 1, "demo runs pytest with xdist")
    decision = {
        "no replacement": _decision("refresh", entry),
        "an unchanged source": _decision("refresh", entry, replacements=[(b, b)]),
        "an updated source to another memory": _decision("refresh", entry,
                                                         replacements=[(a, b)]),
        "a source replaced twice": _decision("refresh", entry, replacements=[(a, a), (a, a)]),
        "another entry's id": _decision("refresh", a, replacements=[(a, a)]),
        "revise without text": _decision("revise", entry, replacements=[(a, a)]),
    }[case]
    world.executor.replies = [decision]
    result, text = _pass(world)
    assert result == PassResult(finished=outcome == "refused")
    assert f"{outcome} {decision['decision']} {entry}: {field}\n" in text
    assert len(world.services.memory.versions(entry)) == 1 and _checked(world, entry) is None


def test_revise_with_bad_text_and_no_replacement_is_invalid_not_refused(world):
    # §10 item 10: text is checked before the replacements rule, so malformed output
    # from the model is never hidden behind a `refused` outcome that lets the pass finish
    a, _, entry = _setup(world)
    _update(world, a, 1, "demo runs pytest with xdist")
    world.executor.replies = [_decision(
        "revise", entry, description="d", body="x" + chr(0xD800))]   # no replacement -- refused
    result, text = _pass(world)
    assert not result.finished
    assert f"invalid revise {entry}: text\n" in text


def test_a_deleted_source_may_only_be_replaced_by_a_successor_sent(world):   # §10 item 10
    a, _, entry = _setup(world)
    _retire_into(world, a)
    unrelated = world.create(world.project.id, "an unrelated memory")
    world.executor.replies = [_decision("refresh", entry, replacements=[(a, unrelated)])]
    result, text = _pass(world)
    assert not result.finished
    assert f"invalid refresh {entry}: replacements\n" in text


def test_revise_rewrites_the_entry_on_the_new_evidence(world):   # §10 item 10
    a, b, entry = _setup(world)
    _update(world, a, 1, "demo runs pytest with xdist")
    world.executor.replies = [_decision(
        "revise", entry, replacements=[(a, a)], description="python tests",
        body="Python projects prefer pytest for tests, often with xdist.")]
    result, text = _pass(world)
    assert result.finished
    current = _current(world, entry)
    assert (current.version, current.description, current.body) == (
        2, "python tests", "Python projects prefer pytest for tests, often with xdist.")
    assert set(current.sources) == {SourceRef(a, 2), SourceRef(b, 1)}
    assert 'description: "python tests"\n' in text


def test_overturned_goes_to_needs_you_and_records_nothing(world):   # §10 item 10
    a, _, entry = _setup(world)
    _update(world, a, 1, "demo dropped pytest for unittest")
    world.executor.replies = [_decision("overturned", entry, reason="demo left pytest")]
    result, text = _pass(world)
    assert result == PassResult(finished=True)
    assert f"overturned global entry {entry}: demo left pytest\n" in (
        text.split("== Needs you ==\n")[1])
    assert len(world.services.memory.versions(entry)) == 1 and _checked(world, entry) is None
    _pass(world)
    assert len(world.executor.calls) == 2              # asked again: nothing was stored


def test_a_keep_followed_by_a_successors_new_version_rechecks(world):   # §10 item 16
    a, _, entry = _setup(world)
    successor = _retire_into(world, a)
    world.executor.replies = [_decision("keep", entry), _decision("keep", entry)]
    _pass(world)
    assert _checked(world, entry) == input_digest(
        [(entry, 1), (a, 1), (a, 2), (successor, 1)])
    _pass(world)
    assert len(world.executor.calls) == 1              # the keep holds
    _update(world, successor, 1, "demo runs pytest with xdist and coverage")
    _pass(world)
    assert len(world.executor.calls) == 2
    change = _changes(world.executor.calls[1]["prompt"])[a]
    assert [(item["id"], item["version"]) for item in change["successors"]] == [(successor, 2)]


@pytest.mark.parametrize("held", ["current", "cited"])
def test_an_entry_with_a_held_back_source_is_skipped_then_rechecked_once_usable(world, held):
    # §10 item 16: re-checked as soon as the source is usable, even with no version change
    a, _, entry = _setup(world)
    _update(world, a, 1, "demo runs pytest with xdist")
    overrides = {"excluded": {a}} if held == "current" else {"history_hits": {a: {1}}}
    result, text = _pass(world, **overrides)
    assert result == PassResult(finished=False)
    assert world.executor.calls == []
    assert f"{entry}: skipped (policy: {a})\n" in text
    assert _checked(world, entry) is None
    world.executor.replies = [_decision("keep", entry)]
    _pass(world)
    assert len(world.executor.calls) == 1 and _checked(world, entry) is not None


def test_an_excluded_entry_is_not_rechecked(world):
    a, _, entry = _setup(world)
    _update(world, a, 1, "demo runs pytest with xdist")
    _, text = _pass(world, excluded={entry})
    assert world.executor.calls == [] and entry not in text


def test_a_reason_the_policy_refuses_is_neither_acted_on_nor_reported(world):
    # §10 item 16 (reason): blocked before the report and before dream.db
    a, _, entry = _setup(world)
    _update(world, a, 1, "demo runs pytest with xdist")
    world.executor.replies = [_decision("keep", entry, reason="as " + world.secret)]
    result, text = _pass(world)
    assert not result.finished
    assert f"rejected keep {entry}: reason\n" in text and "ghp_" not in text
    assert _checked(world, entry) is None


def test_an_executor_failure_records_nothing(world):
    a, _, entry = _setup(world)
    _update(world, a, 1, "demo runs pytest with xdist")
    world.executor.replies = [ExecutorResult(error="timeout")]
    result, text = _pass(world)
    assert not result.finished
    assert f"{entry}: not processed: timeout\n" in text and _checked(world, entry) is None


def test_the_recheck_runs_every_run_even_when_extraction_is_skipped(world):   # §6.6
    a, _, entry = _setup(world)
    _update(world, a, 1, "demo runs pytest with xdist")
    world.executor.replies = [{"judgments": []}, ExecutorResult(error="timeout")]
    world.run(phases={"extract"})
    world.executor.replies = [_decision("keep", entry)]
    row = world.run(phases={"extract"})
    assert len(world.executor.calls) == 3              # extraction once, the re-check twice
    assert world.executor.calls[2]["system_prompt"].startswith(recheck.SYSTEM_PROMPT)
    text = world.report_text(row)
    assert "== Global layer: extraction ==\nunchanged input; skipped\n" in text
    assert f"== Global layer: source re-check ==\nkeep {entry}:" in text
    assert _checked(world, entry) is not None


def test_an_entry_hard_deleted_during_the_run_is_input_changed_and_the_others_run(
        world, monkeypatch):
    # the run goes on with the other entries
    second = _second(world)
    a = world.create(world.project.id, "demo runs pytest")
    c = world.create(world.project.id, "demo lints with ruff")
    b = world.create(second, "second runs pytest")
    first = _create(world, world.global_id, "Python projects prefer pytest.", (a, 1), (b, 1))
    other = _create(world, world.global_id, "Python projects lint with ruff.", (c, 1), (b, 1))
    _update(world, a, 1, "demo runs pytest with xdist")
    _update(world, c, 1, "demo lints with ruff and mypy")
    listed = world.services.memory.memories
    pending = [a]              # hard-deleting a takes `first`, which cites it, along

    def memories_then_hard_delete(project_id=None, **options):
        found = listed(project_id, **options)
        if options.get("include_deleted") and pending:
            plan = world.services.maintenance.plan_hard_delete(pending.pop())
            world.services.maintenance.hard_delete(plan.target, code=plan.code)
        return found

    monkeypatch.setattr(world.services.memory, "memories", memories_then_hard_delete)
    world.executor.replies = [_decision("keep", other)]
    result, text = _pass(world)
    assert result == PassResult(finished=False)
    assert f"{first}: input changed; not processed\n" in text
    assert len(world.executor.calls) == 1 and _checked(world, other) is not None


def test_the_prompt_states_the_recheck_rules():   # spec §6.6
    prompt = recheck.SYSTEM_PROMPT
    assert "must be supported by the new versions or successors given" in prompt
    assert "When in doubt, answer keep or overturned" in prompt
    assert "write principles, not concrete commands" in prompt
