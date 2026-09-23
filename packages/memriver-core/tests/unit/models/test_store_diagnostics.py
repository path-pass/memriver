from memriver_core.models import (
    DiagnosticFinding,
    DiagnosticsReport,
    InspectedMemory,
    Memory,
    StoreFinding,
    StoreReport,
)

P = "aaaaaaaaaa"


def test_store_report_keeps_memory_projects_and_backend_relative_location():
    memory = Memory.new(body="b", type="user", project_id=P, source={})
    finding = StoreFinding(kind="unknown-project", project_id=P,
                           location_hint=f"memories/{memory.id}.md",
                           memory_id=memory.id, reason="r")
    report = StoreReport(initialized=True,
                         entries=(InspectedMemory(memory, f"memories/{memory.id}.md"),),
                         projects=(P,), findings=(finding,))
    assert report.entries[0].memory is memory
    assert report.projects == (P,)
    assert report.findings[0].project_id == P


def test_diagnostics_report_uses_project_ids():
    finding = DiagnosticFinding(kind="stale", memory_ids=("m",), project_ids=(P,),
                                location_hints=("memories/m.md",), reason="r", suggestion="s")
    assert DiagnosticsReport(state="degraded", findings=(finding,)).findings[0].project_ids == (P,)
