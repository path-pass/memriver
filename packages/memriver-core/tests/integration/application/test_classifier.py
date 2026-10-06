"""The content classifier port: MemoryService asks it about new text before the write
transaction; a block, or a classifier that cannot decide, refuses the write."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing

import memriver_core
import pytest
from memriver_core import ContentRejected, StorageFailure, Verdict, bootstrap
from memriver_core.bootstrap import build_services
from memriver_core.models.changes import Create, SoftDelete, SourceRef, Update
from memriver_core.settings import Settings

SECRET = "aws key AKIAIOSFODNN7EXAMPLE ok"          # from the secret-scanner tests
BLOCKED = "content rejected by the content classifier (instruction); no change was made"
UNAVAILABLE = ("the content classifier could not check this text (timeout); no change was "
               "made; see memriver doctor")


class Recorder:
    """A ContentClassifier that records every call and answers `answer(text)`."""

    def __init__(self, answer=lambda text: None) -> None:
        self.answer, self.calls = answer, []

    def classify(self, text: str, *, changed_by: str) -> Verdict | None:
        self.calls.append((text, changed_by))
        return self.answer(text)


@pytest.fixture
def classified(world, tmp_path):
    """Services over the world's store with a Recorder as their classifier."""
    def build(answer=lambda text: None):
        recorder = Recorder(answer)
        services = build_services(Settings(root=world["store"]), root=world["store"],
                                  home=tmp_path / "home", classifier=recorder)
        context = services.project.open_project_context(str(world["work"]))
        return services, context, recorder
    return build


def _count(world) -> int:
    return world["sql"]("SELECT count(*) FROM memories")[0][0]


def _record(services, context, content="uv manages python", description="python tooling"):
    return services.memory.record(content=content, type="project", sync=True, harness="codex",
                                  description=description, context=context)


def test_the_port_is_part_of_the_public_core_surface():
    assert {"ContentClassifier", "Verdict"} <= set(memriver_core.__all__)
    assert Verdict("instruction") == Verdict("instruction", detail="")


def test_record_sends_the_description_and_body_joined_by_a_blank_line(classified):
    services, context, recorder = classified()
    _record(services, context)
    assert recorder.calls == [("python tooling\n\nuv manages python", "mcp")]


def test_a_blank_description_is_not_sent(classified):
    services, context, recorder = classified()
    _record(services, context, description="  ")
    assert recorder.calls == [("uv manages python", "mcp")]


def test_update_sends_its_new_text_and_delete_sends_nothing(classified, world):
    services, context, recorder = classified()
    memory_id = world["create"]("old fact")
    services.memory.update(memory_id, "new fact", context, expected_version=1)
    services.memory.update(memory_id, "newer fact", context, expected_version=2,
                           description="cue two")
    services.memory.delete(memory_id, context, expected_version=3)
    assert recorder.calls == [("new fact", "mcp"), ("cue two\n\nnewer fact", "mcp")]


def test_apply_checks_creates_and_text_updates_in_op_order_only(classified, world):
    services, _, recorder = classified()
    first, second, source, gone = (world["create"](body)
                                   for body in ("one", "two", "a source", "gone"))
    services.memory.apply(
        [Create(world["mine"], "project", "cue", "created fact"),
         Update(first, 1, description="new cue"),
         Update(second, 1, sources=(SourceRef(source, 1),)),
         SoftDelete(gone, 1)],
        changed_by="test")
    assert recorder.calls == [("cue\n\ncreated fact", "test"), ("new cue", "test")]


def test_restore_and_undo_send_nothing(classified, world):
    services, _, recorder = classified()
    memory_id = world["create"]("first text")
    change = world["memory"].apply([Update(memory_id, 1, body="second text")],
                                   changed_by="test")
    services.memory.undo(change.change_id, changed_by="test")              # v3 = v1's text
    services.memory.restore(memory_id, 2, expected_version=3, changed_by="test")   # v4
    assert recorder.calls == []


def test_a_block_refuses_the_write_with_the_category_and_writes_nothing(classified, world):
    services, context, _ = classified(lambda text: Verdict("instruction"))
    before = _count(world)
    with pytest.raises(ContentRejected) as caught:
        _record(services, context)
    assert (str(caught.value), caught.value.rule_id) == (BLOCKED, "classifier-instruction")
    assert caught.value.detail == ""
    assert _count(world) == before


def test_an_undecided_classifier_refuses_with_its_reason_and_points_at_doctor(classified,
                                                                              world):
    services, context, _ = classified(lambda text: Verdict("unavailable", detail="timeout"))
    memory_id = world["create"]("old fact")
    with pytest.raises(ContentRejected) as caught:
        services.memory.update(memory_id, "new fact", context, expected_version=1)
    assert (str(caught.value), caught.value.rule_id) == (UNAVAILABLE, "classifier-unavailable")
    assert caught.value.detail == "timeout"
    assert [v.version for v in services.memory.versions(memory_id)] == [1]


def test_the_first_block_refuses_the_whole_batch(classified, world):
    services, _, recorder = classified(
        lambda text: Verdict("injection") if "bad" in text else None)
    before = _count(world)
    with pytest.raises(ContentRejected) as caught:
        services.memory.apply([Create(world["mine"], "project", "cue", "good fact"),
                               Create(world["mine"], "project", "cue", "bad fact"),
                               Create(world["mine"], "project", "cue", "never asked")],
                              changed_by="test")
    assert caught.value.rule_id == "classifier-injection"
    assert [text for text, _ in recorder.calls] == ["cue\n\ngood fact", "cue\n\nbad fact"]
    assert _count(world) == before


def test_a_secret_is_refused_by_the_policy_before_the_classifier_sees_it(classified, world):
    services, context, recorder = classified()
    with pytest.raises(ContentRejected):
        _record(services, context, content=SECRET)
    with pytest.raises(ContentRejected):
        services.memory.apply([Create(world["mine"], "project", "cue", SECRET)],
                              changed_by="test")
    assert recorder.calls == []


def test_a_policy_refusal_in_apply_has_the_same_shape_with_or_without_a_classifier(classified,
                                                                                   world):
    """`apply`'s pre-check (run only when a classifier is configured) must not change
    what a policy refusal looks like: the same rule id, the same memory_id, the same
    message as the kernel's own refusal (the path taken when there is no classifier)."""
    services, _, recorder = classified()
    memory_id = world["create"]("old fact")
    with pytest.raises(ContentRejected) as with_classifier:
        services.memory.apply([Update(memory_id, 1, body=SECRET)], changed_by="test")
    with pytest.raises(ContentRejected) as without_classifier:
        world["memory"].apply([Update(memory_id, 1, body=SECRET)], changed_by="test")
    assert (with_classifier.value.rule_id, with_classifier.value.memory_id) == \
           (without_classifier.value.rule_id, memory_id)
    assert str(with_classifier.value) == str(without_classifier.value)
    assert recorder.calls == []


def test_a_malformed_category_from_the_classifier_reads_as_invalid(classified):
    services, context, _ = classified(lambda text: Verdict("Bad Câtégory!"))
    with pytest.raises(ContentRejected) as caught:
        _record(services, context)
    assert (str(caught.value), caught.value.rule_id) == (
        "content rejected by the content classifier (invalid); no change was made",
        "classifier-invalid")
    assert caught.value.detail == ""


def test_an_empty_detail_from_the_classifier_reads_as_unknown(classified):
    services, context, _ = classified(lambda text: Verdict("unavailable", detail=""))
    with pytest.raises(ContentRejected) as caught:
        _record(services, context)
    assert (str(caught.value), caught.value.rule_id) == (
        ("the content classifier could not check this text (unknown); no change was "
         "made; see memriver doctor"),
        "classifier-unavailable")
    assert caught.value.detail == "unknown"


def test_a_malformed_detail_from_the_classifier_reads_as_unknown(classified):
    services, context, _ = classified(lambda text: Verdict("unavailable", detail="Not A Label"))
    with pytest.raises(ContentRejected) as caught:
        _record(services, context)
    assert caught.value.detail == "unknown"


def test_a_policy_refusal_carries_no_detail(classified):
    services, context, _ = classified()
    with pytest.raises(ContentRejected) as caught:
        _record(services, context, content=SECRET)
    assert caught.value.detail == ""


def test_an_exception_from_the_classifier_is_a_bug_and_propagates(classified):
    def broken(text):
        raise RuntimeError("classifier bug")

    services, context, _ = classified(broken)
    with pytest.raises(RuntimeError, match="^classifier bug$"):
        _record(services, context)


def test_without_a_classifier_writes_behave_as_before(world):
    memory = world["memory"].record(content="uv manages python", type="project", sync=True,
                                    harness="codex", description="cue",
                                    context=world["context"])
    assert memory.body == "uv manages python"


def test_a_held_write_lock_really_times_out_a_writer(world, tmp_path, monkeypatch):
    # the control for the test below: with the busy timeout patched to 200 ms, a writer
    # that meets a held write lock fails -- so the next test cannot pass vacuously
    monkeypatch.setattr(bootstrap, "BUSY_TIMEOUT_MS", 200)
    plain = build_services(Settings(root=world["store"]), root=world["store"],
                           home=tmp_path / "home")
    with closing(sqlite3.connect(world["store"] / "memriver.db")) as holder:
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(StorageFailure):
            plain.memory.apply([Create(world["mine"], "project", "cue", "blocked")],
                               changed_by="test")
        holder.rollback()


def test_the_classifier_never_runs_under_the_write_lock(world, tmp_path, monkeypatch):
    # a classifier slower than the busy timeout must not make a concurrent writer fail:
    # it runs before the write transaction opens, so nothing holds the lock meanwhile
    monkeypatch.setattr(bootstrap, "BUSY_TIMEOUT_MS", 200)
    entered, release = threading.Event(), threading.Event()

    def slow(text):
        entered.set()
        release.wait(10)
        return None  # noqa: RET501, PLR1711 - matches the answer(text) signature above

    store, home = world["store"], tmp_path / "home"
    slow_services = build_services(Settings(root=store), root=store, home=home,
                                   classifier=Recorder(slow))
    plain = build_services(Settings(root=store), root=store, home=home)
    errors: list[BaseException] = []

    def write_slowly() -> None:
        try:
            slow_services.memory.apply([Create(world["mine"], "project", "cue", "slow fact")],
                                       changed_by="test")
        except BaseException as err:  # noqa: BLE001 - reported by the assert below
            errors.append(err)

    worker = threading.Thread(target=write_slowly)
    worker.start()
    try:
        assert entered.wait(5)
        change = plain.memory.apply([Create(world["mine"], "project", "cue", "fast fact")],
                                    changed_by="test")
    finally:
        release.set()
        worker.join(10)
    assert change.steps and errors == []
