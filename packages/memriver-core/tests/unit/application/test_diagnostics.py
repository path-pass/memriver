"""DiagnosticsService against a fake StoreInspector: the whole backend-neutral
policy (staleness, near-duplicates, backend-finding mapping, state
derivation) without a filesystem.
"""

from __future__ import annotations

import pytest
from memriver_core.application import diagnostics
from memriver_core.application.diagnostics import DiagnosticsService
from memriver_core.models import (
    InspectedMemory,
    Memory,
    StoreFinding,
    StoreReport,
)

FIXED_NOW = "2026-08-31T00:00:00Z"
PID = "aaaaaaaaaa"
PID2 = "bbbbbbbbbb"


class FakeInspector:
    def __init__(self, report: StoreReport) -> None:
        self.report = report
        self.calls = 0

    def inspect(self) -> StoreReport:
        self.calls += 1
        return self.report


def _memory(id: str, *, project_id: str = PID,
            body: str = "a body long enough to trigram", updated: str = FIXED_NOW) -> Memory:
    return Memory(id=id, project_id=project_id, type="project", source={}, trust="agent",
                  sync=True, created=updated, updated=updated, description="", body=body)


def inspected(id: str, *, project_id: str = PID,
              body: str = "a body long enough to trigram", updated: str = FIXED_NOW,
              location_hint: str | None = None) -> InspectedMemory:
    return InspectedMemory(memory=_memory(id, project_id=project_id, body=body, updated=updated),
                           location_hint=location_hint or f"memories/{id}")


def store_finding(kind: str, *, project_id: str | None = None,
                  location_hint: str = "memories/broken.md", memory_id: str | None = "broken",
                  reason: str = "backend-authored reason") -> StoreFinding:
    return StoreFinding(kind=kind, project_id=project_id, location_hint=location_hint,
                        memory_id=memory_id, reason=reason)


# --- Step 1: validation + state derivation ----------------------------------

@pytest.mark.parametrize(
    "kwargs",
    [
        {"stale_days": 0},
        {"stale_days": -1},
        {"jaccard_threshold": 0},
        {"jaccard_threshold": 1.01},
    ],
)
def test_invalid_limits_fail_before_inspection(kwargs):
    inspector = FakeInspector(StoreReport(True, (), (), ()))
    with pytest.raises(ValueError):
        DiagnosticsService(inspector).run(**kwargs)
    assert inspector.calls == 0


def test_malformed_now_fails_before_inspection():
    inspector = FakeInspector(StoreReport(True, (), (), ()))
    with pytest.raises(ValueError):
        DiagnosticsService(inspector).run(now="not-a-timestamp")
    assert inspector.calls == 0


@pytest.mark.parametrize(
    ("report", "state"),
    [
        (StoreReport(False, (), (), ()), "uninitialized"),
        (StoreReport(True, (), (), ()), "empty"),
        (StoreReport(True, (inspected("a"),), (), ()), "healthy"),
        (StoreReport(True, (), (), (store_finding("unparsable"),)), "degraded"),
        (StoreReport(False, (), (), (store_finding("legacy-layout"),)), "degraded"),
    ],
)
def test_state_is_derived_without_backend_guessing(report, state):
    # a fixed `now` isolates state derivation from staleness policy: the
    # fixture entries carry `updated=FIXED_NOW`, and without an explicit
    # `now` the real clock would eventually flag them stale, flipping the
    # expected "healthy" case here to "degraded".
    assert DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW).state == state


# --- Step 2: policy tests ----------------------------------------------------

def test_stale_entry_past_threshold_is_flagged():
    old = inspected("old-one", updated="2025-01-01T00:00:00Z")
    report = StoreReport(True, (old,), (), ())
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW, stale_days=90)
    stale = [f for f in result.findings if f.kind == "stale"]
    assert len(stale) == 1
    assert stale[0].memory_ids == ("old-one",)
    assert stale[0].project_ids == (PID,)


def test_recent_entry_is_not_stale():
    recent = inspected("fresh-one", updated=FIXED_NOW)
    report = StoreReport(True, (recent,), (), ())
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW, stale_days=90)
    assert not [f for f in result.findings if f.kind == "stale"]


def test_invalid_updated_produces_finding_and_does_not_abort():
    bad = inspected("bad-one", updated="not-a-timestamp")
    report = StoreReport(True, (bad,), (), ())
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW)
    invalid = [f for f in result.findings if f.kind == "invalid-updated"]
    assert len(invalid) == 1
    assert invalid[0].memory_ids == ("bad-one",)
    assert result.state == "degraded"


def test_naive_updated_is_invalid_not_a_crash():
    naive = inspected("naive-one", updated="2026-01-01T00:00:00")
    report = StoreReport(True, (naive,), (), ())
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW)
    invalid = [f for f in result.findings if f.kind == "invalid-updated"]
    assert len(invalid) == 1
    assert result.state == "degraded"


def test_timestamp_conversion_overflow_is_invalid_not_a_crash():
    # syntactically valid ISO 8601, but astimezone(UTC) overflows datetime's
    # representable range -- must be reported as one invalid-updated finding,
    # not raise past the doctor boundary.
    overflow = inspected("overflow-one", updated="0001-01-01T00:00:00+14:00")
    report = StoreReport(True, (overflow,), (), ())
    inspector = FakeInspector(report)
    result = DiagnosticsService(inspector).run(now=FIXED_NOW)
    invalid = [f for f in result.findings if f.kind == "invalid-updated"]
    assert len(invalid) == 1
    assert invalid[0].memory_ids == ("overflow-one",)
    assert result.state == "degraded"
    assert inspector.calls == 1


def test_backend_findings_precede_policy_findings():
    old = inspected("old-one", updated="2025-01-01T00:00:00Z")
    report = StoreReport(True, (old,), (), (store_finding("unparsable"),))
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW, stale_days=90)
    assert result.findings[0].kind == "unparsable"
    assert result.findings[1].kind == "stale"


def test_huge_stale_days_on_empty_store_does_not_overflow():
    # the cutoff (now - stale_days) underflows datetime's representable range
    # well before an empty, initialized store has any entry to compare it
    # against; clamping to datetime.min must keep this a plain "empty" run,
    # not an OverflowError bubbling past the doctor boundary.
    report = StoreReport(True, (), (), ())
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW, stale_days=1_000_000)
    assert result.state == "empty"


def test_now_none_uses_current_time_and_does_not_raise(monkeypatch):
    # exercises the default-clock path (`now=None`) without depending on the
    # real wall clock's distance from the fixture entries' fixed
    # `updated=FIXED_NOW`: fixing `_default_now` keeps this test's outcome
    # independent of when it runs.
    monkeypatch.setattr(diagnostics, "_default_now", lambda: FIXED_NOW)
    result = DiagnosticsService(FakeInspector(StoreReport(True, (inspected("a"),), (), ()))).run()
    assert result.state == "healthy"


def test_near_duplicate_bodies_are_flagged():
    a = inspected("dup-a", body="The Quick Brown Fox Jumps Over The Lazy Dog")
    b = inspected("dup-b", body="the   quick brown FOX jumps over the lazy dog")
    report = StoreReport(True, (a, b), (), ())
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW, jaccard_threshold=0.6)
    dupes = [f for f in result.findings if f.kind == "near-duplicate"]
    assert len(dupes) == 1
    assert dupes[0].memory_ids == ("dup-a", "dup-b")


def test_short_bodies_do_not_divide_by_zero_or_pair():
    a = inspected("short-a", body="ab")
    b = inspected("short-b", body="cd")
    report = StoreReport(True, (a, b), (), ())
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW)
    assert not [f for f in result.findings if f.kind == "near-duplicate"]


def test_duplicate_pair_order_is_deterministic_by_project_then_id():
    z = inspected("zzz", body="alpha beta gamma delta epsilon")
    a = inspected("aaa", body="alpha beta gamma delta epsilon")
    result = DiagnosticsService(FakeInspector(StoreReport(True, (z, a), (), ()))).run(
        now=FIXED_NOW, jaccard_threshold=0.6)
    dupes = [f for f in result.findings if f.kind == "near-duplicate"]
    assert [d.memory_ids for d in dupes] == [("aaa", "zzz")]


def test_duplicate_pair_order_uses_project_before_id():
    first = inspected("zzz", project_id=PID, body="alpha beta gamma delta epsilon")
    second = inspected("aaa", project_id=PID2, body="alpha beta gamma delta epsilon")
    result = DiagnosticsService(FakeInspector(StoreReport(True, (second, first), (), ()))).run(
        now=FIXED_NOW, jaccard_threshold=0.6)
    dupes = [f for f in result.findings if f.kind == "near-duplicate"]
    assert dupes[0].memory_ids == ("zzz", "aaa")
    assert dupes[0].project_ids == (PID, PID2)


def test_no_shadowing_finding_exists_any_more():
    a = inspected("same-body-1", project_id=PID)
    b = inspected("same-body-2", project_id=PID2, body="entirely different words here")
    result = DiagnosticsService(FakeInspector(StoreReport(True, (a, b), (), ()))).run(now=FIXED_NOW)
    assert "shadowing" not in {f.kind for f in result.findings}
    assert not hasattr(diagnostics, "_shadowing_findings")


def test_a_legacy_store_is_degraded_and_still_says_it_is_not_initialized():
    report = StoreReport(False, (), (),
                         (store_finding("legacy-layout", location_hint="memories",
                                       memory_id=None),))
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW)
    assert (result.state, result.initialized) == ("degraded", False)


@pytest.mark.parametrize(
    ("kind", "suggestion"),
    [
        ("unknown-schema", "restore the database from a backup made by this memriver version"),
        ("unsafe-database", "replace it with the real database file"),
        ("integrity", "restore the database from a backup"),
        ("orphan", "restore the missing project from a backup; until then the memory stays hidden"),
        ("invalid-row", "fix or remove the row"),
        ("non-canonical-root", "bind the real directory with memriver project unbind and adopt"),
        ("unverifiable-root", "restore access to the directory, then run memriver doctor again"),
        ("root-conflict", "unbind one of them with memriver project unbind"),
        ("legacy-layout", "migrate it, or remove it once migrated"),
    ],
)
def test_new_backend_kinds_get_their_own_suggestion(kind, suggestion):
    report = StoreReport(True, (), (), (store_finding(kind),))
    mapped = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW).findings[0]
    assert mapped.suggestion == suggestion


def test_mixed_empty_and_nonempty_trigram_pair_yields_no_finding():
    a = inspected("short", body="ab")
    b = inspected("long", body="alpha beta gamma delta epsilon")
    report = StoreReport(True, (a, b), (), ())
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW)
    assert not [f for f in result.findings if f.kind == "near-duplicate"]


def test_backend_finding_fields_are_copied_without_absolute_paths():
    finding = store_finding("invalid-row", project_id=PID,
                            location_hint="memories/broken.md", memory_id="broken",
                            reason="memory file is not decodable memory markdown")
    report = StoreReport(True, (), (), (finding,))
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW)
    assert len(result.findings) == 1
    mapped = result.findings[0]
    assert mapped.kind == "invalid-row"
    assert mapped.memory_ids == ("broken",)
    assert mapped.project_ids == (PID,)
    assert mapped.location_hints == ("memories/broken.md",)
    assert mapped.reason == "memory file is not decodable memory markdown"
    assert not mapped.location_hints[0].startswith("/")


def test_the_inspectors_projects_pass_through_to_the_report():
    from memriver_core.models import InspectedProject

    project = InspectedProject(id=PID, name="demo", root="/w", is_global=False,
                               root_state="ok", active_memories=0, deleted_memories=0)
    report = StoreReport(initialized=True, entries=(), projects=(project,), findings=())
    assert DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW).projects == (project,)
