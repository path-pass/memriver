//! The diagnosis over a report made by hand: staleness, near-duplicates, the mapping of
//! the inspector's findings and the state, without a store.

use std::fs;
use std::sync::LazyLock;

use memriver_test_support::Sandbox;

use super::*;
use crate::models::diagnostics::{InspectedProject, RootState};
use crate::models::memory::{Memory, MemoryType, Trust};
use crate::settings::Settings;

const FIXED_NOW: &str = "2026-08-31T00:00:00Z";
const PID: &str = "aaaaaaaaaa";
const PID2: &str = "bbbbbbbbbb";

fn inspected(id: &str, project_id: &str, body: &str, updated: &str) -> InspectedMemory {
    InspectedMemory {
        memory: Memory {
            id: id.to_string(),
            project_id: project_id.to_string(),
            type_: MemoryType::Project,
            source_harness: "t".to_string(),
            source_method: "agent".to_string(),
            trust: Trust::Agent,
            sync: true,
            created: updated.to_string(),
            updated: updated.to_string(),
            description: String::new(),
            body: body.to_string(),
            version: 1,
            deleted_at: None,
            last_read_at: None,
            level: None,
            tags: Vec::new(),
        },
        location_hint: format!("memories/{id}"),
    }
}

/// A memory last changed at `at`; its body is too short to be like another.
fn updated(id: &str, at: &str) -> InspectedMemory {
    inspected(id, PID, "ab", at)
}

fn bodied(id: &str, project_id: &str, body: &str) -> InspectedMemory {
    inspected(id, project_id, body, FIXED_NOW)
}

fn store_finding(kind: &str) -> StoreFinding {
    StoreFinding {
        kind: kind.to_string(),
        project_id: None,
        location_hint: "memories/broken.md".to_string(),
        memory_id: Some("broken".to_string()),
        reason: "backend-authored reason".to_string(),
    }
}

fn report(
    initialized: bool,
    entries: Vec<InspectedMemory>,
    findings: Vec<StoreFinding>,
) -> StoreReport {
    StoreReport {
        initialized,
        entries,
        projects: Vec::new(),
        findings,
        incomplete_changes: Vec::new(),
    }
}

fn diagnosed(report: StoreReport, stale_days: u32) -> DiagnosticsReport {
    diagnosis(
        report,
        timestamp(FIXED_NOW).unwrap(),
        stale_days,
        JACCARD_THRESHOLD,
    )
}

fn of_entries(entries: Vec<InspectedMemory>) -> DiagnosticsReport {
    diagnosed(report(true, entries, Vec::new()), 90)
}

/// A service over a store whose inspection fails: its root is a file.
fn failing() -> (Sandbox, MaintenanceService) {
    let sandbox = Sandbox::new();
    let root = sandbox.dir.path().join("file");
    fs::write(&root, "").unwrap();
    let services = crate::build_services(
        &Settings::light(root),
        &sandbox.home,
        &Default::default(),
        None,
    );
    (sandbox, services.maintenance)
}

fn value(result: Result<DiagnosticsReport, WriteError>) -> String {
    match result {
        Err(WriteError::Value(err)) => err.to_string(),
        other => panic!("not a caller's error: {other:?}"),
    }
}

#[test]
fn invalid_limits_fail_before_inspection() {
    let (_sandbox, service) = failing();
    assert_eq!(
        service.diagnose(Some(FIXED_NOW), 90),
        Err(WriteError::Core(CoreError::StorageFailure))
    );
    assert_eq!(
        value(service.diagnose(Some(FIXED_NOW), 0)),
        "stale_days must be a positive number of days"
    );
}

#[test]
fn malformed_now_fails_before_inspection() {
    let (_sandbox, service) = failing();
    assert_eq!(
        value(service.diagnose(Some("not-a-timestamp"), 90)),
        "Invalid isoformat string: 'not-a-timestamp'"
    );
    assert_eq!(
        value(service.diagnose(Some("2026-08-31T00:00:00"), 90)),
        "timestamp must include a timezone"
    );
    assert_eq!(
        value(service.diagnose(Some("0001-01-01T00:00:00+14:00"), 90)),
        "date value out of range"
    );
}

#[test]
fn now_none_uses_the_clock() {
    let sandbox = Sandbox::new();
    let services = crate::build_services(
        &Settings::light(sandbox.root.clone()),
        &sandbox.home,
        &Default::default(),
        None,
    );
    let empty = services.maintenance.diagnose(None, 90).unwrap();
    assert_eq!(empty.state, DiagnosticsState::Uninitialized);
    // the clock's reading is one the diagnosis itself reads
    assert!(timestamp(&now()).is_ok());
    let fresh = report(true, vec![updated("a", &now())], Vec::new());
    let read = diagnosis(fresh, timestamp(&now()).unwrap(), 90, JACCARD_THRESHOLD);
    assert_eq!(read.state, DiagnosticsState::Healthy);
}

#[test]
fn state_precedence_degraded_uninitialized_empty_healthy() {
    let entry = || vec![updated("a", FIXED_NOW)];
    let cases = [
        (report(false, Vec::new(), Vec::new()), "uninitialized"),
        (report(true, Vec::new(), Vec::new()), "empty"),
        (report(true, entry(), Vec::new()), "healthy"),
        (
            report(true, Vec::new(), vec![store_finding("unparsable")]),
            "degraded",
        ),
        (
            report(false, Vec::new(), vec![store_finding("legacy-layout")]),
            "degraded",
        ),
        (
            report(true, entry(), vec![store_finding("orphan")]),
            "degraded",
        ),
    ];
    for (report, state) in cases {
        assert_eq!(diagnosed(report, 90).state.as_str(), state);
    }
}

#[test]
fn a_legacy_store_is_degraded_and_still_says_it_is_not_initialized() {
    let result = diagnosed(
        report(false, Vec::new(), vec![store_finding("legacy-layout")]),
        90,
    );
    assert_eq!(
        (result.state, result.initialized),
        (DiagnosticsState::Degraded, false)
    );
}

#[test]
fn stale_after_90_days() {
    // 90 days before the instant is 2026-06-02T00:00:00Z: older than that is stale
    let result = of_entries(vec![
        updated("old-one", "2025-01-01T00:00:00Z"),
        updated("just-past", "2026-06-01T23:59:59.999999Z"),
        updated("on-the-day", "2026-06-02T00:00:00.000000Z"),
        updated("fresh-one", FIXED_NOW),
        // the same instant in another zone is the same instant
        updated("zoned-fresh", "2026-06-02T05:30:00+05:30"),
        updated("zoned-stale", "2026-06-02T05:29:59+05:30"),
    ]);
    let stale: Vec<_> = result
        .findings
        .iter()
        .map(|found| {
            (
                found.kind.as_str(),
                found.memory_ids.join(","),
                found.project_ids.join(","),
                found.location_hints.join(","),
                found.reason.as_str(),
                found.suggestion.as_str(),
            )
        })
        .collect();
    let finding = |id: &'static str| {
        (
            "stale",
            id.to_string(),
            PID.to_string(),
            format!("memories/{id}"),
            "not updated in over 90 days",
            "review and refresh, or delete, this memory",
        )
    };
    assert_eq!(
        stale,
        [
            finding("old-one"),
            finding("just-past"),
            finding("zoned-stale")
        ]
    );
    assert_eq!(result.state, DiagnosticsState::Degraded);
    // the number of days is the caller's, and is said
    let result = diagnosed(
        report(true, vec![updated("a", "2026-08-29T00:00:00Z")], Vec::new()),
        1,
    );
    assert_eq!(result.findings[0].reason, "not updated in over 1 days");
}

#[test]
fn an_updated_value_that_is_no_instant_is_a_finding_and_does_not_abort() {
    let result = of_entries(vec![
        updated("bad-one", "not-a-timestamp"),
        // without a zone
        updated("naive-one", "2026-01-01T00:00:00"),
        // a time that cannot be moved to UTC
        updated("overflow-one", "0001-01-01T00:00:00+14:00"),
        updated("good-one", FIXED_NOW),
    ]);
    let invalid: Vec<_> = result
        .findings
        .iter()
        .map(|found| {
            (
                found.kind.as_str(),
                found.memory_ids.join(","),
                found.project_ids.join(","),
                found.location_hints.join(","),
                found.reason.as_str(),
                found.suggestion.as_str(),
            )
        })
        .collect();
    let finding = |id: &'static str| {
        (
            "invalid-updated",
            id.to_string(),
            PID.to_string(),
            format!("memories/{id}"),
            "stored 'updated' value is not a valid timezone-aware timestamp",
            "fix or remove the malformed 'updated' timestamp",
        )
    };
    assert_eq!(
        invalid,
        [
            finding("bad-one"),
            finding("naive-one"),
            finding("overflow-one")
        ]
    );
}

#[test]
fn backend_findings_precede_policy_findings() {
    let entries = vec![
        bodied("dup-a", PID, "alpha beta gamma delta epsilon"),
        bodied("dup-b", PID, "alpha beta gamma delta epsilon"),
        updated("old-one", "2025-01-01T00:00:00Z"),
    ];
    let result = diagnosed(report(true, entries, vec![store_finding("unparsable")]), 90);
    let kinds: Vec<_> = result.findings.iter().map(|f| f.kind.as_str()).collect();
    assert_eq!(kinds, ["unparsable", "stale", "near-duplicate"]);
}

#[test]
fn the_largest_number_of_days_does_not_overflow() {
    // the cutoff lies before the calendar starts: nothing is stale, nothing overflows
    for days in [1_000_000, 3_652_059, u32::MAX] {
        let entries = vec![updated("first", "0001-01-01T00:00:00Z")];
        let result = diagnosed(report(true, entries, Vec::new()), days);
        assert_eq!(result.state, DiagnosticsState::Healthy, "{days}");
        let result = diagnosed(report(true, Vec::new(), Vec::new()), days);
        assert_eq!(result.state, DiagnosticsState::Empty);
    }
    // one day less than the calendar is long, and its first instant is stale
    let now = timestamp("9999-12-31T23:59:59.999999Z").unwrap();
    let entries = || vec![updated("first", "0001-01-01T00:00:00Z")];
    let stale = |days| {
        let result = diagnosis(report(true, entries(), Vec::new()), now, days, 0.6);
        result.findings.len()
    };
    assert_eq!((stale(3_652_058), stale(3_652_059)), (1, 0));
}

#[test]
fn near_duplicate_bodies_are_flagged() {
    let result = of_entries(vec![
        bodied("dup-a", PID, "The Quick Brown Fox Jumps Over The Lazy Dog"),
        bodied(
            "dup-b",
            PID2,
            "the   quick brown FOX jumps over the lazy dog",
        ),
        bodied("other", PID, "entirely different words here"),
    ]);
    let found: Vec<_> = result.findings.iter().collect();
    let [only] = &found[..] else {
        panic!("one finding: {found:?}")
    };
    assert_eq!(
        **only,
        DiagnosticFinding {
            kind: "near-duplicate".to_string(),
            memory_ids: vec!["dup-a".to_string(), "dup-b".to_string()],
            project_ids: vec![PID.to_string(), PID2.to_string()],
            location_hints: vec!["memories/dup-a".to_string(), "memories/dup-b".to_string()],
            reason: "bodies are 100% similar (>= 60% threshold)".to_string(),
            suggestion: "merge or remove the near-duplicate memory".to_string(),
        }
    );
}

#[test]
fn near_duplicate_reason_uses_round_half_even_percent() {
    // five of eight trigrams in common is 62.5 %, seven of eight 87.5 %: each is
    // written as the even number beside it
    let reason = |a: &str, b: &str| {
        let result = of_entries(vec![bodied("a", PID, a), bodied("b", PID, b)]);
        let reasons: Vec<_> = result.findings.into_iter().map(|f| f.reason).collect();
        reasons.join("|")
    };
    assert_eq!(
        reason("abcdefghij", "abcdefg"),
        "bodies are 62% similar (>= 60% threshold)"
    );
    assert_eq!(
        reason("abcdefghij", "abcdefghi"),
        "bodies are 88% similar (>= 60% threshold)"
    );
    // three of five is the threshold itself, and counts
    assert_eq!(
        reason("abcde", "abcdefg"),
        "bodies are 60% similar (>= 60% threshold)"
    );
    // below the threshold: no finding
    assert_eq!(reason("abcdefghijk", "abcdefg x"), "");
    assert_eq!(
        (percent(0.005), percent(0.015), percent(0.025)),
        ("0%".to_string(), "2%".to_string(), "2%".to_string())
    );
}

#[test]
fn the_trigrams_are_of_the_lowered_body_with_its_blanks_folded() {
    // a separator `trim` and `split_whitespace` do not know, and a sigma at the end of
    // a word, which lower-cases to its final form
    let mut found: Vec<String> = trigrams("ΑΣ\u{1f}ΒΣΑ  ΟΔΟΣ")
        .into_iter()
        .map(String::from_iter)
        .collect();
    found.sort();
    assert_eq!(
        found,
        [
            " βσ", " οδ", "α ο", "ας ", "βσα", "δος", "οδο", "ς β", "σα "
        ]
    );
    assert!(trigrams("ab").is_empty() && trigrams(" a  b ").len() == 1);
}

#[test]
fn short_bodies_do_not_divide_by_zero_or_pair() {
    let result = of_entries(vec![
        bodied("short-a", PID, "ab"),
        bodied("short-b", PID, "cd"),
        bodied("same-a", PID, "ab"),
        bodied("long", PID, "alpha beta gamma delta epsilon"),
    ]);
    assert_eq!(result.findings, Vec::new());
}

#[test]
fn duplicate_pair_order_is_by_project_then_id() {
    let same = "alpha beta gamma delta epsilon";
    let result = of_entries(vec![bodied("zzz", PID, same), bodied("aaa", PID, same)]);
    assert_eq!(result.findings[0].memory_ids, ["aaa", "zzz"]);
    let result = of_entries(vec![bodied("aaa", PID2, same), bodied("zzz", PID, same)]);
    assert_eq!(result.findings[0].memory_ids, ["zzz", "aaa"]);
    assert_eq!(result.findings[0].project_ids, [PID, PID2]);
    // three alike: every pair, the first of each in order
    let entries = ["c", "a", "b"].map(|id| bodied(id, PID, same)).to_vec();
    let pairs: Vec<_> = of_entries(entries)
        .findings
        .into_iter()
        .map(|found| found.memory_ids.join(""))
        .collect();
    assert_eq!(pairs, ["ab", "ac", "bc"]);
}

#[test]
fn every_backend_kind_has_its_own_suggestion() {
    let backup = "restore the database from a backup";
    let suggestions = [
        (
            "unknown-schema",
            "restore the database from a backup made by this memriver version",
        ),
        ("unsafe-database", "replace it with the real database file"),
        ("integrity", backup),
        (
            "orphan",
            "restore the missing project from a backup; until then the memory stays hidden",
        ),
        ("invalid-row", "fix or remove the row"),
        (
            "non-canonical-root",
            "bind the real directory with memriver project unbind and adopt",
        ),
        (
            "unverifiable-root",
            "restore access to the directory, then run memriver doctor again",
        ),
        (
            "root-conflict",
            "unbind one of them with memriver project unbind",
        ),
        ("legacy-layout", "migrate it, or remove it once migrated"),
        (
            "session-orphan",
            "restore the missing project from a backup; until then the session's project is \
             unreachable",
        ),
        ("session-move-damaged", backup),
        ("version-gap", backup),
        ("version-mismatch", backup),
        ("unrecorded-version", backup),
        ("dangling-source", backup),
        ("source-cycle", backup),
        (
            "a-kind-of-tomorrow",
            "inspect this entry manually; its finding kind is unrecognized",
        ),
    ];
    for (kind, suggestion) in suggestions {
        let result = diagnosed(report(true, Vec::new(), vec![store_finding(kind)]), 90);
        assert_eq!(result.findings[0].suggestion, suggestion, "{kind}");
    }
}

#[test]
fn backend_finding_fields_are_copied() {
    let mut finding = store_finding("invalid-row");
    finding.project_id = Some(PID.to_string());
    let result = diagnosed(report(true, Vec::new(), vec![finding]), 90);
    assert_eq!(
        result.findings,
        [DiagnosticFinding {
            kind: "invalid-row".to_string(),
            memory_ids: vec!["broken".to_string()],
            project_ids: vec![PID.to_string()],
            location_hints: vec!["memories/broken.md".to_string()],
            reason: "backend-authored reason".to_string(),
            suggestion: "fix or remove the row".to_string(),
        }]
    );
    // a finding about no memory and no project names none
    let mut finding = store_finding("legacy-layout");
    finding.memory_id = None;
    let result = diagnosed(report(false, Vec::new(), vec![finding]), 90);
    let found = &result.findings[0];
    assert!(found.memory_ids.is_empty() && found.project_ids.is_empty());
}

#[test]
fn the_inspectors_projects_and_incomplete_changes_pass_through() {
    let project = InspectedProject {
        id: PID.to_string(),
        name: "demo".to_string(),
        root: Some("/w".to_string()),
        is_global: false,
        root_state: RootState::Ok,
        active_memories: 0,
        deleted_memories: 0,
    };
    let mut report = report(true, Vec::new(), Vec::new());
    report.projects = vec![project.clone()];
    report.incomplete_changes = vec!["cccccccccc".to_string()];
    let result = diagnosed(report, 90);
    assert_eq!(result.projects, [project]);
    assert_eq!(result.incomplete_changes, ["cccccccccc"]);
    assert_eq!(result.state, DiagnosticsState::Empty);
}

#[test]
fn maintenance_shares_the_one_content_policy_and_a_diagnosis_does_not_load_it() {
    let sandbox = Sandbox::new();
    let services = crate::build_services(
        &Settings::light(sandbox.root.clone()),
        &sandbox.home,
        &Default::default(),
        None,
    );
    let maintenance = &services.maintenance;
    assert!(std::sync::Arc::ptr_eq(
        &maintenance.policy,
        services.memory.policy_source()
    ));
    let loaded = || LazyLock::get(&maintenance.policy).is_some();
    maintenance.diagnose(None, 90).unwrap();
    assert_eq!(maintenance.scan_policy(), Ok(Vec::new()));
    assert!(maintenance.plan_hard_delete("zzzzzzzzzz").is_err());
    assert!(!loaded());
    assert_eq!(maintenance.check_text("a plain sentence"), None);
    assert!(loaded());
}
