from typing import get_type_hints

from memriver_core.models import (
    DiagnosticFinding,
    DiagnosticsReport,
    InspectedMemory,
    Memory,
    Scope,
    StoreFinding,
    StoreReport,
)
from memriver_core.repository.inspection_protocol import StoreInspector


def test_store_report_keeps_memory_and_backend_relative_location():
    memory = Memory.new(
        id="mise", body="mise manages runtimes", type="project",
        scope=Scope.global_(), source={},
    )
    inspected = InspectedMemory(memory=memory, location_hint="global/entries/mise.md")
    finding = StoreFinding(
        kind="unaddressable-id", scope=Scope.global_(),
        location_hint="global/entries/Bad_Name.md",
        memory_id="Bad_Name", reason="entry id cannot be addressed",
    )

    report = StoreReport(initialized=True, entries=(inspected,), findings=(finding,))

    assert report.entries[0].memory is memory
    assert not report.entries[0].location_hint.startswith("/")
    assert get_type_hints(StoreInspector.inspect)["return"] is StoreReport


def test_diagnostics_report_uses_backend_neutral_finding_fields():
    finding = DiagnosticFinding(
        kind="stale", memory_ids=("mise",), scopes=(Scope.global_(),),
        location_hints=("global/entries/mise.md",),
        reason="entry has not been confirmed recently",
        suggestion="verify the fact and update or delete it",
    )
    assert DiagnosticsReport(state="degraded", findings=(finding,)).state == "degraded"
