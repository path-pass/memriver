"""DiagnosticsService against a fake StoreInspector: the whole backend-neutral
policy (staleness, near-duplicates, shadowing, backend-finding mapping,
state derivation) without a filesystem.
"""

from __future__ import annotations

import pytest
from memriver_core.application.diagnostics import DiagnosticsService
from memriver_core.models import (
    InspectedMemory,
    Memory,
    ProjectId,
    Scope,
    StoreFinding,
    StoreReport,
)

FIXED_NOW = "2026-08-31T00:00:00Z"
PID = ProjectId("proj-one")
PID2 = ProjectId("proj-two")
GLOBAL = Scope.global_()


class FakeInspector:
    def __init__(self, report: StoreReport) -> None:
        self.report = report
        self.calls = 0

    def inspect(self) -> StoreReport:
        self.calls += 1
        return self.report


def _memory(id: str, *, scope: Scope = GLOBAL,
            body: str = "a body long enough to trigram", updated: str = FIXED_NOW) -> Memory:
    return Memory(id=id, type="project", scope=scope, sync=True, created=updated,
                  updated=updated, source={}, trust="agent", description="", body=body)


def inspected(id: str, *, scope: Scope = GLOBAL,
              body: str = "a body long enough to trigram", updated: str = FIXED_NOW,
              location_hint: str | None = None) -> InspectedMemory:
    return InspectedMemory(memory=_memory(id, scope=scope, body=body, updated=updated),
                           location_hint=location_hint or f"{scope.to_storage()}/{id}.md")


def store_finding(kind: str, *, scope: Scope | None = None,
                  location_hint: str = "global/broken.md", memory_id: str | None = "broken",
                  reason: str = "backend-authored reason") -> StoreFinding:
    return StoreFinding(kind=kind, scope=scope, location_hint=location_hint,
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
    inspector = FakeInspector(StoreReport(True, (), ()))
    with pytest.raises(ValueError):
        DiagnosticsService(inspector).run(**kwargs)
    assert inspector.calls == 0


def test_malformed_now_fails_before_inspection():
    inspector = FakeInspector(StoreReport(True, (), ()))
    with pytest.raises(ValueError):
        DiagnosticsService(inspector).run(now="not-a-timestamp")
    assert inspector.calls == 0


@pytest.mark.parametrize(
    ("report", "state"),
    [
        (StoreReport(False, (), ()), "uninitialized"),
        (StoreReport(True, (), ()), "empty"),
        (StoreReport(True, (inspected("a"),), ()), "healthy"),
        (StoreReport(True, (), (store_finding("unparsable"),)), "degraded"),
    ],
)
def test_state_is_derived_without_backend_guessing(report, state):
    assert DiagnosticsService(FakeInspector(report)).run().state == state


# --- Step 2: policy tests ----------------------------------------------------

def test_stale_entry_past_threshold_is_flagged():
    old = inspected("old-one", updated="2025-01-01T00:00:00Z")
    report = StoreReport(True, (old,), ())
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW, stale_days=90)
    stale = [f for f in result.findings if f.kind == "stale"]
    assert len(stale) == 1
    assert stale[0].memory_ids == ("old-one",)
    assert stale[0].scopes == (Scope.global_(),)


def test_recent_entry_is_not_stale():
    recent = inspected("fresh-one", updated=FIXED_NOW)
    report = StoreReport(True, (recent,), ())
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW, stale_days=90)
    assert not [f for f in result.findings if f.kind == "stale"]


def test_invalid_updated_produces_finding_and_does_not_abort():
    bad = inspected("bad-one", updated="not-a-timestamp")
    report = StoreReport(True, (bad,), ())
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW)
    invalid = [f for f in result.findings if f.kind == "invalid-updated"]
    assert len(invalid) == 1
    assert invalid[0].memory_ids == ("bad-one",)
    assert result.state == "degraded"


def test_naive_updated_is_invalid_not_a_crash():
    naive = inspected("naive-one", updated="2026-01-01T00:00:00")
    report = StoreReport(True, (naive,), ())
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW)
    invalid = [f for f in result.findings if f.kind == "invalid-updated"]
    assert len(invalid) == 1
    assert result.state == "degraded"


def test_backend_findings_precede_policy_findings():
    old = inspected("old-one", updated="2025-01-01T00:00:00Z")
    report = StoreReport(True, (old,), (store_finding("unparsable"),))
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW, stale_days=90)
    assert result.findings[0].kind == "unparsable"
    assert result.findings[1].kind == "stale"


def test_now_none_uses_current_time_and_does_not_raise():
    result = DiagnosticsService(FakeInspector(StoreReport(True, (inspected("a"),), ()))).run()
    assert result.state == "healthy"


def test_near_duplicate_bodies_are_flagged():
    a = inspected("dup-a", body="The Quick Brown Fox Jumps Over The Lazy Dog")
    b = inspected("dup-b", body="the   quick brown FOX jumps over the lazy dog")
    report = StoreReport(True, (a, b), ())
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW, jaccard_threshold=0.6)
    dupes = [f for f in result.findings if f.kind == "near-duplicate"]
    assert len(dupes) == 1
    assert dupes[0].memory_ids == ("dup-a", "dup-b")


def test_short_bodies_do_not_divide_by_zero_or_pair():
    a = inspected("short-a", body="ab")
    b = inspected("short-b", body="cd")
    report = StoreReport(True, (a, b), ())
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW)
    assert not [f for f in result.findings if f.kind == "near-duplicate"]


def test_duplicate_pair_order_is_deterministic_by_scope_then_id():
    # inserted in reverse order on purpose
    z = inspected("zzz", body="alpha beta gamma delta epsilon")
    a = inspected("aaa", body="alpha beta gamma delta epsilon")
    report = StoreReport(True, (z, a), ())
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW, jaccard_threshold=0.6)
    dupes = [f for f in result.findings if f.kind == "near-duplicate"]
    assert len(dupes) == 1
    assert dupes[0].memory_ids == ("aaa", "zzz")


def test_duplicate_pair_order_uses_scope_before_id():
    # id order and scope order disagree on purpose: "zzz" < "aaa" is false,
    # but "global" < "project:proj-one" is true, so only the scope key can
    # produce (zzz, aaa) here.
    g = inspected("zzz", scope=GLOBAL, body="alpha beta gamma delta epsilon")
    p = inspected("aaa", scope=Scope.project(PID), body="alpha beta gamma delta epsilon")
    report = StoreReport(True, (p, g), ())
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW, jaccard_threshold=0.6)
    dupes = [f for f in result.findings if f.kind == "near-duplicate"]
    assert len(dupes) == 1
    assert dupes[0].memory_ids == ("zzz", "aaa")
    assert dupes[0].scopes == (GLOBAL, Scope.project(PID))


def test_mixed_empty_and_nonempty_trigram_pair_yields_no_finding():
    a = inspected("short", body="ab")
    b = inspected("long", body="alpha beta gamma delta epsilon")
    report = StoreReport(True, (a, b), ())
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW)
    assert not [f for f in result.findings if f.kind == "near-duplicate"]


def test_global_plus_project_same_id_is_shadowing():
    g = inspected("shared", scope=Scope.global_())
    p = inspected("shared", scope=Scope.project(PID))
    report = StoreReport(True, (g, p), ())
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW)
    shadows = [f for f in result.findings if f.kind == "shadowing"]
    assert len(shadows) == 1
    assert shadows[0].memory_ids == ("shared",)
    assert shadows[0].scopes == (Scope.global_(), Scope.project(PID))


def test_two_projects_same_id_without_global_is_not_shadowing():
    p1 = inspected("shared", scope=Scope.project(PID))
    p2 = inspected("shared", scope=Scope.project(PID2))
    report = StoreReport(True, (p1, p2), ())
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW)
    assert not [f for f in result.findings if f.kind == "shadowing"]


def test_backend_finding_fields_are_copied_without_absolute_paths():
    finding = store_finding("unparsable", scope=Scope.global_(),
                            location_hint="global/broken.md", memory_id="broken",
                            reason="entry file is not decodable memory markdown")
    report = StoreReport(True, (), (finding,))
    result = DiagnosticsService(FakeInspector(report)).run(now=FIXED_NOW)
    assert len(result.findings) == 1
    mapped = result.findings[0]
    assert mapped.kind == "unparsable"
    assert mapped.memory_ids == ("broken",)
    assert mapped.scopes == (Scope.global_(),)
    assert mapped.location_hints == ("global/broken.md",)
    assert mapped.reason == "entry file is not decodable memory markdown"
    assert not mapped.location_hints[0].startswith("/")
