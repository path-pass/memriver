from memriver_core.models import (
    DiagnosticFinding,
    DiagnosticsReport,
    InspectedMemory,
    InspectedProject,
    Memory,
    StoreFinding,
    StoreReport,
)

P = "aaaaaaaaaa"


def _project() -> InspectedProject:
    return InspectedProject(id=P, name="demo", root="/w", is_global=False, root_state="ok",
                            active_memories=1, deleted_memories=0)


def test_store_report_keeps_memories_projects_and_findings():
    memory = Memory.new(body="b", type="user", project_id=P, source={})
    finding = StoreFinding(kind="orphan", project_id=P, location_hint=f"memories/{memory.id}",
                           memory_id=memory.id, reason="r")
    report = StoreReport(initialized=True,
                         entries=(InspectedMemory(memory, f"memories/{memory.id}"),),
                         projects=(_project(),), findings=(finding,))
    assert report.entries[0].memory is memory
    assert report.projects[0].root_state == "ok"
    assert report.findings[0].project_id == P


def test_diagnostics_report_carries_projects_with_an_empty_default():
    finding = DiagnosticFinding(kind="stale", memory_ids=("m",), project_ids=(P,),
                                location_hints=("memories/m",), reason="r", suggestion="s")
    assert DiagnosticsReport(state="degraded", findings=(finding,)).projects == ()
    assert DiagnosticsReport(state="healthy", findings=(), projects=(_project(),)).projects[0].id == P
