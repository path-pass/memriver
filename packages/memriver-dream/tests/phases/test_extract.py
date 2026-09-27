"""The global layer's extraction (spec §6.5) over a real store and a scripted executor:
C4 tracing, every judgment's validation, the skip (§6.9) and the prompt rules."""

from __future__ import annotations

import pytest
from memriver_core import MemoryNotFound
from memriver_core.models.changes import Create, SourceRef, Update
from memriver_dream.phases import (
    EXTRACTION_SCOPE,
    PassResult,
    consolidate,
    extract,
    recheck,
)
from memriver_dream.store import DreamStore, input_digest


def _judgment(kind: str, *, id: str = "", type: str = "", description: str = "",
              body: str = "", source_ids=(), reason: str = "seen in two projects") -> dict:
    return {"kind": kind, "id": id, "type": type, "description": description, "body": body,
            "source_ids": list(source_ids), "reason": reason}


def _new(*source_ids: str, type: str = "feedback") -> dict:
    return _judgment("new", type=type, description="python tests",
                     body="Python projects prefer pytest for tests.", source_ids=source_ids)


def _answer(*judgments: dict) -> dict:
    return {"judgments": list(judgments)}


def _second(world) -> str:
    directory = world.root.parent / "second"
    directory.mkdir()
    return world.services.project.init_project(
        "second", world.services.project.plan_root(str(directory))).id


def _global(world, body: str, *sources: tuple[str, int]) -> str:
    change = world.services.memory.apply(
        [Create(project_id=world.global_id, type="feedback", description="principle",
                body=body, sources=tuple(SourceRef(i, v) for i, v in sources))],
        changed_by="test")
    return change.steps[0].memory_id


def _pass(world, **overrides) -> tuple[PassResult, str]:
    ctx = world.context(**overrides)
    result = extract.run(ctx)
    ctx.report.footer(status="completed", finished_at=world.now)
    text = ctx.report.path.read_text()
    ctx.report.path.unlink()
    return result, text


def _current(world, memory_id: str):
    return max(world.services.memory.versions(memory_id), key=lambda version: version.version)


def _globals(world) -> set[str]:
    return {memory.id for memory in world.services.memory.memories(world.global_id)}


def test_a_principle_from_two_projects_becomes_a_global_entry_citing_both(world):
    # §10 item 10 (new)
    second = _second(world)
    a = world.create(world.project.id, "this repo runs pytest for its tests")
    b = world.create(second, "tests here use pytest")
    world.executor.replies = [_answer(_new(a, b))]
    result, text = _pass(world)
    assert result == PassResult(finished=True, digest=input_digest([(a, 1), (b, 1)]))
    (created,) = world.services.memory.memories(world.global_id)
    assert (created.type, created.body) == ("feedback", "Python projects prefer pytest for tests.")
    assert set(_current(world, created.id).sources) == {SourceRef(a, 1), SourceRef(b, 1)}
    assert _current(world, created.id).change.changed_by == "dream"
    assert "applying new (creates a memory) -> change" in text
    assert [len(world.services.memory.versions(i)) for i in (a, b)] == [1, 1]   # sources stay


def test_every_project_and_global_are_sent_with_their_owner(world):
    second = _second(world)
    a = world.create(world.project.id, "demo fact")
    b = world.create(second, "second fact")
    entry = _global(world, "a principle", (a, 1))
    held = world.create(second, "held back by the policy scan")
    world.executor.replies = [_answer()]
    result, _ = _pass(world, excluded={held})
    prompt = world.executor.calls[0]["prompt"]
    assert f'"project": "{world.project.id}"' in prompt and f'"project": "{second}"' in prompt
    assert '"project": "global"' in prompt and entry in prompt
    assert held not in prompt and "held back" not in prompt
    assert result == PassResult(finished=True,
                                digest=input_digest([(a, 1), (b, 1), (entry, 1)]))


def test_a_global_source_counts_the_projects_its_cited_versions_trace_to(world):
    # §10 item 10: C4 tracing through a global source -- the cited version is followed,
    # not the source's current one, and recursively through global entries
    second = _second(world)
    a = world.create(world.project.id, "pytest in demo")
    b = world.create(second, "pytest in second")
    via = _global(world, "pytest is the runner in demo", (a, 1))
    deeper = _global(world, "pytest is the runner", (via, 1))
    world.services.memory.apply(
        [Update(memory_id=a, expected_version=1, body="pytest in demo, with xdist")],
        changed_by="test")
    world.executor.replies = [_answer(_new(deeper, b))]
    result, _ = _pass(world)
    assert result.finished
    (created,) = _globals(world) - {via, deeper}
    assert set(_current(world, created).sources) == {SourceRef(deeper, 1), SourceRef(b, 1)}


@pytest.mark.parametrize("case", [
    "one project plus global", "a global entry citing nothing", "two memories of one project"])
def test_fewer_than_two_projects_is_refused_and_goes_to_needs_you(world, case):
    # §10 item 10: one project plus global is refused; global never counts as a project.
    # A rule refusal is deterministic: reported, and the pass still finishes
    a1 = world.create(world.project.id, "pytest in demo")
    a2 = world.create(world.project.id, "demo's CI runs pytest")
    via = _global(world, "pytest is the runner", (a2, 1))
    bare = _global(world, "an imported entry without sources")
    sources = {"one project plus global": (a1, via),
               "a global entry citing nothing": (a1, bare),
               "two memories of one project": (a1, a2)}[case]
    world.executor.replies = [_answer(_new(*sources))]
    result, text = _pass(world)
    assert result == PassResult(finished=True, digest=input_digest(
        [(a1, 1), (a2, 1), (via, 1), (bare, 1)]))
    assert "refused new: traces to 1 project(s)\n" in text
    needs = text.split("== Needs you ==\n")[1]
    assert (f"extraction refused: new from {' '.join(sources)} (traces to 1 project(s)): "
            "seen in two projects\n") in needs
    assert _globals(world) == {via, bare}


def test_supplement_rewrites_a_global_entry_citing_its_sources_plus_the_new(world):
    # §10 item 10 (supplement)
    second = _second(world)
    a = world.create(world.project.id, "pytest in demo")
    b = world.create(second, "pytest in second, run with coverage")
    target = _global(world, "Python projects prefer pytest.", (a, 1))
    world.executor.replies = [_answer(_judgment(
        "supplement", id=target, description="python tests",
        body="Python projects prefer pytest, often with coverage.", source_ids=(b,)))]
    result, text = _pass(world)
    assert result.finished
    current = _current(world, target)
    assert (current.version, current.body) == (
        2, "Python projects prefer pytest, often with coverage.")
    assert set(current.sources) == {SourceRef(a, 1), SourceRef(b, 1)}
    assert f"applying supplement {target} -> change" in text


def test_add_sources_cites_the_new_memories_and_keeps_the_text(world):   # §10 item 10
    second = _second(world)
    a = world.create(world.project.id, "pytest in demo")
    b = world.create(second, "pytest in second")
    target = _global(world, "Python projects prefer pytest.", (a, 1))
    world.executor.replies = [_answer(_judgment("add_sources", id=target, source_ids=(b,)))]
    result, text = _pass(world)
    assert result.finished
    current = _current(world, target)
    assert (current.version, current.body) == (2, "Python projects prefer pytest.")
    assert set(current.sources) == {SourceRef(a, 1), SourceRef(b, 1)}
    assert '  description: "principle"\n' in text


@pytest.mark.parametrize(("kind", "case", "outcome", "field"), [
    ("new", "unknown source", "invalid", "source_ids"),
    ("supplement", "unknown target", "invalid", "id"),
    ("add_sources", "held back", "invalid", "source_ids"),
    ("new", "unstorable body", "invalid", "text"),
    ("new", "no type", "refused", "type"), ("new", "no source", "refused", "source_ids"),
    ("new", "empty body", "refused", "text"),
    ("supplement", "a project memory as target", "refused", "id"),
    ("supplement", "itself as a source", "refused", "source_ids"),
    ("supplement", "nothing new", "refused", "source_ids"),
    ("supplement", "empty body", "refused", "text"),
    ("add_sources", "nothing new", "refused", "source_ids")])
def test_an_extraction_that_fails_validation_changes_nothing(world, kind, case, outcome,
                                                             field):
    # §10 items 9 and 10: malformed output (an id not sent, unstorable text) does not
    # finish the pass; a well-formed judgment a rule refuses goes to Needs you and does
    second = _second(world)
    a = world.create(world.project.id, "pytest in demo")
    b = world.create(second, "pytest in second")
    held = world.create(second, "held back")
    target = _global(world, "Python projects prefer pytest.", (a, 1))
    judgment = {
        ("new", "no type"): _new(a, b, type=""),
        ("new", "no source"): _new(),
        ("new", "unknown source"): _new(a, "zzzzzzzzzz"),
        ("new", "empty body"): _judgment("new", type="feedback", description="d", body=" ",
                                         source_ids=(a, b)),
        ("new", "unstorable body"): _judgment("new", type="feedback", description="d",
                                              body="x" + chr(0xD800), source_ids=(a, b)),
        ("supplement", "a project memory as target"): _judgment(
            "supplement", id=a, description="d", body="b", source_ids=(b,)),
        ("supplement", "unknown target"): _judgment(
            "supplement", id="zzzzzzzzzz", description="d", body="b", source_ids=(b,)),
        ("supplement", "itself as a source"): _judgment(
            "supplement", id=target, description="d", body="b", source_ids=(target, b)),
        ("supplement", "nothing new"): _judgment(
            "supplement", id=target, description="d", body="reworded", source_ids=(a,)),
        ("supplement", "empty body"): _judgment(
            "supplement", id=target, description="d", body="", source_ids=(b,)),
        ("add_sources", "nothing new"): _judgment("add_sources", id=target, source_ids=(a,)),
        ("add_sources", "held back"): _judgment("add_sources", id=target, source_ids=(held,)),
    }[(kind, case)]
    world.executor.replies = [_answer(judgment)]
    result, text = _pass(world, excluded={held})
    assert result == PassResult(finished=outcome == "refused",
                                digest=input_digest([(a, 1), (b, 1), (target, 1)]))
    assert f"{outcome} {kind}: {field}\n" in text
    assert ("extraction refused:" in text) is (outcome == "refused")
    assert _globals(world) == {target} and len(world.services.memory.versions(target)) == 1


def test_a_supplement_that_stays_in_one_project_is_refused(world):   # §10 item 10
    a1 = world.create(world.project.id, "pytest in demo")
    a2 = world.create(world.project.id, "pytest in demo's CI")
    target = _global(world, "Python projects prefer pytest.", (a1, 1))
    world.executor.replies = [_answer(_judgment("supplement", id=target, description="d",
                                                body="b", source_ids=(a2,)))]
    result, text = _pass(world)
    assert result.finished
    assert (f"extraction refused: supplement {target} from {a2} (traces to 1 project(s))"
            in text)
    assert len(world.services.memory.versions(target)) == 1


def test_a_refused_extraction_stores_its_digest_and_the_next_run_skips(world):   # §10 item 9
    a1 = world.create(world.project.id, "pytest in demo")
    a2 = world.create(world.project.id, "demo's CI runs pytest")
    world.executor.replies = [_answer(_new(a1, a2))]
    world.run(phases={"extract"})
    store = DreamStore(world.root / "dream" / "dream.db")
    assert store.scope_digest(EXTRACTION_SCOPE) == input_digest([(a1, 1), (a2, 1)])
    row = world.run(phases={"extract"})
    assert len(world.executor.calls) == 1
    assert "== Global layer: extraction ==\nunchanged input; skipped\n" in world.report_text(row)


def test_an_unchanged_input_is_skipped_without_a_call(world):   # §10 item 9
    a = world.create(world.project.id, "demo fact")
    world.context().store.put_scope_pass(EXTRACTION_SCOPE, input_digest([(a, 1)]), world.now)
    result, text = _pass(world)
    assert result == PassResult(finished=True)
    assert world.executor.calls == [] and text.startswith("unchanged input; skipped\n")


def test_a_reason_the_policy_refuses_is_neither_acted_on_nor_reported(world):
    # §10 item 16 (reason)
    second = _second(world)
    a = world.create(world.project.id, "pytest in demo")
    b = world.create(second, "pytest in second")
    world.executor.replies = [_answer({**_new(a, b), "reason": "as " + world.secret})]
    result, text = _pass(world)
    assert not result.finished
    assert "rejected new: reason\n" in text and "ghp_" not in text
    assert _globals(world) == set()


def test_another_projects_change_reruns_its_own_pass_and_extraction_only(world):
    # §10 item 9: another project's change reruns extraction only (not this project's pass)
    second = _second(world)
    a = world.create(world.project.id, "demo fact")
    b = world.create(second, "second fact")
    world.executor.default = lambda prompt, schema: {"judgments": []}
    phases = {"consolidate", "extract"}
    world.run(phases=phases)
    first = len(world.executor.calls)
    assert first == 3          # demo, second, extraction; global has nothing to send
    world.services.memory.apply(
        [Update(memory_id=b, expected_version=1, body="second fact, revised")],
        changed_by="test")
    world.run(phases=phases)
    later = world.executor.calls[first:]
    assert len(later) == 2
    assert later[0]["system_prompt"].startswith(consolidate.SYSTEM_PROMPT)
    assert b in later[0]["prompt"] and a not in later[0]["prompt"]
    assert later[1]["system_prompt"].startswith(extract.SYSTEM_PROMPT)


def test_a_memory_hard_deleted_before_the_call_leaves_extraction_unfinished(world,
                                                                             monkeypatch):
    # extraction is retried next run; the re-check still runs this run
    second = _second(world)
    a = world.create(world.project.id, "pytest in demo")
    b = world.create(second, "pytest in second")
    victim = world.create(second, "deleted by the user meanwhile")
    entry = _global(world, "Python projects prefer pytest.", (a, 1), (b, 1))
    world.services.memory.apply(
        [Update(memory_id=a, expected_version=1, body="pytest in demo, with xdist")],
        changed_by="test")
    listed = world.services.memory.memories
    pending = [victim]

    def memories_then_hard_delete(project_id=None, **options):
        found = listed(project_id, **options)
        if project_id is None and not options.get("include_deleted") and pending:
            plan = world.services.maintenance.plan_hard_delete(pending.pop())
            world.services.maintenance.hard_delete(plan.target, code=plan.code)
        return found

    monkeypatch.setattr(world.services.memory, "memories", memories_then_hard_delete)
    world.executor.replies = [{"decision": "keep", "id": entry, "description": "", "body": "",
                               "replacements": [], "reason": "still holds"}]
    row = world.run(phases={"extract"})
    assert row.status == "completed"
    assert ("== Global layer: extraction ==\ninput changed; not processed\n"
            in world.report_text(row))
    (call,) = world.executor.calls                     # only the re-check was asked
    assert call["system_prompt"].startswith(recheck.SYSTEM_PROMPT)
    store = DreamStore(world.root / "dream" / "dream.db")
    assert store.scope_digest(EXTRACTION_SCOPE) is None
    world.executor.replies = [_answer()]
    world.run(phases={"extract"})
    assert len(world.executor.calls) == 2
    assert world.executor.calls[1]["system_prompt"].startswith(extract.SYSTEM_PROMPT)
    assert victim not in world.executor.calls[1]["prompt"]


def test_a_global_source_gone_while_tracing_is_input_changed_not_a_refusal(world,
                                                                          monkeypatch):
    # a vanished memory is never traced as one without sources
    second = _second(world)
    a = world.create(world.project.id, "pytest in demo")
    b = world.create(second, "pytest in second")
    via = _global(world, "pytest is the runner in demo", (a, 1))
    versions = world.services.memory.versions
    reads = {via: 0}

    def versions_then_gone(memory_id):
        if memory_id == via:
            reads[via] += 1
            if reads[via] > 1:                # read for the input, gone when traced
                raise MemoryNotFound(memory_id)
        return versions(memory_id)

    monkeypatch.setattr(world.services.memory, "versions", versions_then_gone)
    world.executor.replies = [_answer(_new(via, b))]
    result, text = _pass(world)
    assert result == PassResult(finished=False, digest=input_digest([(a, 1), (b, 1), (via, 1)]))
    assert "new: input changed; not processed\n" in text and "refused" not in text
    assert _globals(world) == {via}


def test_the_prompt_states_the_extraction_rules():   # spec §6.5
    prompt = extract.SYSTEM_PROMPT
    assert '"Python projects prefer pytest for tests", never "pytest -q"' in prompt
    assert "Keep the condition under which a principle holds" in prompt
    assert "reads right in any project" in prompt
    assert "never make a project-local requirement global without its condition" in prompt
    assert "at least two projects" in prompt
    assert "Prefer supplementing" in prompt
    assert "never reword an entry without new evidence" in prompt
    assert "When in doubt, answer no_change" in prompt
