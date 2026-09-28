"""Read facts, usage and the history reads over a real store (spec §3.5, §4.1)."""

from __future__ import annotations

import pytest
from memriver_core.bootstrap import build_services
from memriver_core.models import SessionKey
from memriver_core.models.changes import SourceRef, Update
from memriver_core.models.errors import MemoryNotFound, StorageFailure
from memriver_core.settings import Settings


def _reads(world, memory_id: str) -> list[tuple]:
    return world["sql"]("SELECT memory_version, harness, session_id FROM memory_reads "
                        "WHERE memory_id = ? ORDER BY read_at", memory_id)


def test_a_read_records_the_version_handed_out_the_harness_and_the_session(world):
    memory = world["memory"]
    written = memory.record(content="a fact", type="project", sync=True, harness="codex",
                            description="", context=world["context"])
    memory.update(written.id, "a newer fact", world["context"], expected_version=1)
    assert memory.read(written.id, world["context"], harness="cursor").version == 2
    key = SessionKey("claude-code", "session-9")
    session_context = world["services"].session.start_session(
        key, source="startup", entry_dir=str(world["work"]), transcript_path=None)
    memory.read(written.id, session_context, harness="claude-code")
    assert _reads(world, written.id) == [(2, "cursor", None), (2, "claude-code", "session-9")]
    assert world["sql"]("SELECT last_read_at IS NOT NULL FROM memories WHERE id = ?",
                        written.id) == [(1,)]


def test_a_read_never_creates_a_version_or_a_change(world):
    memory_id = world["create"]()
    counts = world["sql"]("SELECT (SELECT count(*) FROM memory_versions), "
                          "(SELECT count(*) FROM changes)")
    world["memory"].read(memory_id, world["context"], harness="codex")
    assert world["sql"]("SELECT (SELECT count(*) FROM memory_versions), "
                        "(SELECT count(*) FROM changes)") == counts


def test_recording_a_read_is_best_effort(world):
    memory_id = world["create"]()
    # a harness longer than the column allows fails the insert; the read still answers,
    # and the high-water mark still advances even though the read fact itself is dropped
    assert world["memory"].read(memory_id, world["context"], harness="h" * 65).id == memory_id
    assert _reads(world, memory_id) == []
    assert world["sql"]("SELECT last_read_at IS NOT NULL FROM memories WHERE id = ?",
                        memory_id) == [(1,)]


def test_usage_counts_reads_and_reports_the_stored_high_water_mark(world):
    memory = world["memory"]
    read_twice, never_read = world["create"]("one"), world["create"]("two")
    memory.read(read_twice, world["context"], harness="codex")
    memory.read(read_twice, world["context"], harness="codex")
    usage = memory.usage([read_twice, never_read, "zzzzzzzzzz"])
    assert set(usage) == {read_twice, never_read}
    assert usage[read_twice].reads == 2 and usage[read_twice].last_read_at is not None
    assert (usage[never_read].reads, usage[never_read].last_read_at) == (0, None)


def test_prune_reads_applies_the_retention_and_is_a_no_op_when_unset(world, tmp_path):
    memory_id = world["create"]()
    world["memory"].read(memory_id, world["context"], harness="codex")
    world["sql"]("INSERT INTO memory_reads (memory_id, memory_version, read_at, harness) "
                 "VALUES (?, 1, '2020-01-01T00:00:00.000000Z', 'codex')", memory_id)
    assert world["memory"].prune_reads() == 0
    assert len(_reads(world, memory_id)) == 2
    retaining = build_services(Settings(root=world["store"], memory_reads_retention_days=1),
                               root=world["store"], home=tmp_path / "home")
    assert retaining.memory.prune_reads() == 1
    assert len(_reads(world, memory_id)) == 1


@pytest.mark.parametrize(("days", "kept"), [
    # the cutoff lands near year 931: compared as fixed-width text it drops only year 500
    (400_000, ["2020-01-01T00:00:00.000000Z"]),
    # before year 1, and past what a timedelta holds: both saturate, nothing is older
    (800_000, ["0500-01-01T00:00:00.000000Z", "2020-01-01T00:00:00.000000Z"]),
    (10**9, ["0500-01-01T00:00:00.000000Z", "2020-01-01T00:00:00.000000Z"]),
])
def test_prune_reads_saturates_and_compares_fixed_width_times(world, tmp_path, days, kept):
    memory_id = world["create"]()
    for read_at in ("0500-01-01T00:00:00.000000Z", "2020-01-01T00:00:00.000000Z"):
        world["sql"]("INSERT INTO memory_reads (memory_id, memory_version, read_at, harness) "
                     "VALUES (?, 1, ?, 'codex')", memory_id, read_at)
    retaining = build_services(Settings(root=world["store"], memory_reads_retention_days=days),
                               root=world["store"], home=tmp_path / "home")
    retaining.memory.prune_reads()
    assert [row[0] for row in world["sql"]("SELECT read_at FROM memory_reads "
                                           "ORDER BY read_at")] == kept


def test_versions_lists_every_version_with_its_state_sources_and_change(world):
    memory = world["memory"]
    source = world["create"]("source")
    memory_id = world["create"]("v1")
    memory.apply([Update(memory_id, 1, body="v2", sources=(SourceRef(source, 1),))],
                 changed_by="human", changed_via="cli")
    memory.delete(memory_id, world["context"], expected_version=2)
    versions = memory.versions(memory_id)
    assert [(v.version, v.body, v.deleted, v.sources) for v in versions] == [
        (1, "v1", False, ()), (2, "v2", False, (SourceRef(source, 1),)),
        (3, "v2", True, (SourceRef(source, 1),))]
    assert [(v.change.changed_by, v.change.changed_via, v.change.steps) for v in versions] == \
        [("human", None, ()), ("human", "cli", ()), ("mcp", None, ())]
    assert all(v.change.at for v in versions)


def test_an_imported_version_has_no_change(world):
    memory_id = world["create"]()
    world["sql"]("UPDATE memory_versions SET change_id = NULL WHERE memory_id = ?", memory_id)
    [imported] = world["memory"].versions(memory_id)
    assert imported.change is None


def test_versions_of_an_unknown_id_is_not_found(world):
    with pytest.raises(MemoryNotFound):
        world["memory"].versions("zzzzzzzzzz")


def test_a_damaged_history_row_is_a_storage_failure_never_bytes(world):
    memory_id = world["create"]()
    world["sql"]("UPDATE memory_versions SET body = CAST(X'80' AS TEXT) WHERE memory_id = ?",
                memory_id)
    with pytest.raises(StorageFailure):
        world["memory"].versions(memory_id)


def test_a_version_with_an_undecodable_source_id_is_a_storage_failure_never_bytes(world):
    memory_id = world["create"]()
    world["sql"]("INSERT INTO memory_sources (memory_id, version, source_id, source_version) "
                "VALUES (?, 1, CAST(X'80' AS TEXT), 1)", memory_id)
    with pytest.raises(StorageFailure):
        world["memory"].versions(memory_id)


def test_memories_lists_current_states_across_projects_and_global(world):
    memory = world["memory"]
    mine, global_one = world["create"]("mine"), world["create"]("g", project_id=world["global"])
    theirs, gone = world["create"]("theirs", project_id=world["theirs"]), world["create"]("gone")
    memory.delete(gone, world["context"], expected_version=1)
    assert {m.id for m in memory.memories()} == {mine, global_one, theirs}
    assert {m.id for m in memory.memories(world["mine"])} == {mine}
    assert {m.id for m in memory.memories(world["mine"], include_deleted=True)} == {mine, gone}


def test_citing_lists_the_versions_of_other_memories_that_cite_any_version(world):
    memory = world["memory"]
    source = world["create"]("source")
    citing = world["create"]("citing", sources=(SourceRef(source, 1),))
    memory.apply([Update(citing, 1, body="citing, edited")], changed_by="human")
    assert [(c.memory_id, c.version, c.source_version, c.current)
            for c in memory.citing(source)] == [(citing, 1, 1, False), (citing, 2, 1, True)]
    assert memory.citing(citing) == []
