//! The maintenance service: the store's diagnosis, the policy scan and the hard delete.
//!
//! `diagnose` owns the checks that are no matter of storage -- staleness, near-duplicate
//! bodies -- and gives what the inspector found the same shape, each kind with its
//! suggestion. It never touches a file or a table itself.

use std::collections::HashSet;

use super::memory::PolicySource;
use crate::models::changes::{Confirmation, HardDeletePlan, PolicyHit};
use crate::models::diagnostics::{
    DiagnosticFinding, DiagnosticsReport, DiagnosticsState, InspectedMemory, StoreFinding,
    StoreReport,
};
use crate::models::errors::{CoreError, ValueError, WriteError};
use crate::models::helpers::{now, py_repr, py_split};
use crate::models::isoformat::{Refused, utc_micros};
use crate::repository::sqlite::inspector::StoreInspector;
use crate::repository::sqlite::memory_store::MemoryStore;
use crate::settings::JACCARD_THRESHOLD;

const DAY_MICROS: i128 = 86_400_000_000;

/// Fixed wording per kind the inspector reports; a kind it does not know yet still gets
/// a safe, generic suggestion.
fn suggestion(kind: &str) -> &'static str {
    match kind {
        "unknown-schema" => "restore the database from a backup made by this memriver version",
        "unsafe-database" => "replace it with the real database file",
        "orphan" => "restore the missing project from a backup; until then the memory stays hidden",
        "invalid-row" => "fix or remove the row",
        "non-canonical-root" => "bind the real directory with memriver project unbind and adopt",
        "unverifiable-root" => "restore access to the directory, then run memriver doctor again",
        "root-conflict" => "unbind one of them with memriver project unbind",
        "legacy-layout" => "migrate it, or remove it once migrated",
        "session-orphan" => {
            "restore the missing project from a backup; until then the session's project is \
             unreachable"
        }
        "integrity"
        | "session-move-damaged"
        | "version-gap"
        | "version-mismatch"
        | "unrecorded-version"
        | "dangling-source"
        | "source-cycle" => "restore the database from a backup",
        _ => "inspect this entry manually; its finding kind is unrecognized",
    }
}

fn mapped(finding: StoreFinding) -> DiagnosticFinding {
    DiagnosticFinding {
        suggestion: suggestion(&finding.kind).to_string(),
        kind: finding.kind,
        memory_ids: finding.memory_id.into_iter().collect(),
        project_ids: finding.project_id.into_iter().collect(),
        location_hints: vec![finding.location_hint],
        reason: finding.reason,
    }
}

/// The instant `value` names, in microseconds since 1970; an error in Python's words for
/// what `datetime.fromisoformat` refuses, a time without a zone and one that cannot be
/// moved to UTC.
fn timestamp(value: &str) -> Result<i64, ValueError> {
    utc_micros(value).map_err(|refused| {
        ValueError::new(match refused {
            Refused::Invalid => format!("Invalid isoformat string: {}", py_repr(value)),
            Refused::Naive => "timestamp must include a timezone".to_string(),
            Refused::OutOfRange => "date value out of range".to_string(),
        })
    })
}

fn about(
    entry: &InspectedMemory,
    kind: &str,
    reason: String,
    suggestion: &str,
) -> DiagnosticFinding {
    DiagnosticFinding {
        kind: kind.to_string(),
        memory_ids: vec![entry.memory.id.clone()],
        project_ids: vec![entry.memory.project_id.clone()],
        location_hints: vec![entry.location_hint.clone()],
        reason,
        suggestion: suggestion.to_string(),
    }
}

fn staleness(report: &StoreReport, now: i64, stale_days: u32) -> Vec<DiagnosticFinding> {
    // in 128 bits no number of days overflows; a cutoff before the calendar starts has
    // nothing before it, so nothing is stale
    let cutoff = i128::from(now) - i128::from(stale_days) * DAY_MICROS;
    let mut findings = Vec::new();
    for entry in &report.entries {
        match utc_micros(&entry.memory.updated) {
            Err(_) => findings.push(about(
                entry,
                "invalid-updated",
                "stored 'updated' value is not a valid timezone-aware timestamp".to_string(),
                "fix or remove the malformed 'updated' timestamp",
            )),
            Ok(updated) if i128::from(updated) < cutoff => findings.push(about(
                entry,
                "stale",
                format!("not updated in over {stale_days} days"),
                "review and refresh, or delete, this memory",
            )),
            Ok(_) => {}
        }
    }
    findings
}

/// The character trigrams of a body in lower case with its blanks folded.
fn trigrams(body: &str) -> HashSet<[char; 3]> {
    let lowered = body.to_lowercase();
    let normalized: Vec<char> = py_split(&lowered)
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .collect();
    normalized
        .windows(3)
        .map(|gram| [gram[0], gram[1], gram[2]])
        .collect()
}

/// A share as Python's `{:.0%}` writes it.
fn percent(share: f64) -> String {
    format!("{:.0}%", share * 100.0)
}

fn duplicates(report: &StoreReport, threshold: f64) -> Vec<DiagnosticFinding> {
    let mut ordered: Vec<&InspectedMemory> = report.entries.iter().collect();
    ordered.sort_by_key(|entry| {
        (
            &entry.memory.project_id,
            &entry.memory.id,
            &entry.location_hint,
        )
    });
    let grams: Vec<_> = ordered
        .iter()
        .map(|entry| trigrams(&entry.memory.body))
        .collect();
    let mut findings = Vec::new();
    // ponytail: O(n^2) pairwise scan over the whole store, no comparison cap -- local-store
    // scale keeps this sub-second. Re-enter with time-bounding or MinHash if team-scale
    // stores make it slow.
    for (first, grams_a) in grams.iter().enumerate() {
        for (second, grams_b) in grams.iter().enumerate().skip(first + 1) {
            if grams_a.is_empty() || grams_b.is_empty() {
                continue;
            }
            let common = grams_a.intersection(grams_b).count();
            let jaccard = common as f64 / (grams_a.len() + grams_b.len() - common) as f64;
            if jaccard >= threshold {
                let (a, b) = (ordered[first], ordered[second]);
                findings.push(DiagnosticFinding {
                    kind: "near-duplicate".to_string(),
                    memory_ids: vec![a.memory.id.clone(), b.memory.id.clone()],
                    project_ids: vec![a.memory.project_id.clone(), b.memory.project_id.clone()],
                    location_hints: vec![a.location_hint.clone(), b.location_hint.clone()],
                    reason: format!(
                        "bodies are {} similar (>= {} threshold)",
                        percent(jaccard),
                        percent(threshold)
                    ),
                    suggestion: "merge or remove the near-duplicate memory".to_string(),
                });
            }
        }
    }
    findings
}

/// What the inspector's report comes to at the instant `now`.
fn diagnosis(report: StoreReport, now: i64, stale_days: u32, threshold: f64) -> DiagnosticsReport {
    let stale = staleness(&report, now, stale_days);
    let alike = duplicates(&report, threshold);
    let initialized = report.initialized;
    let none = report.entries.is_empty();
    let findings: Vec<_> = (report.findings.into_iter().map(mapped))
        .chain(stale)
        .chain(alike)
        .collect();
    // a finding outranks everything: a damaged store must never read as merely "not
    // initialized yet"
    let state = match () {
        () if !findings.is_empty() => DiagnosticsState::Degraded,
        () if !initialized => DiagnosticsState::Uninitialized,
        () if none => DiagnosticsState::Empty,
        () => DiagnosticsState::Healthy,
    };
    DiagnosticsReport {
        state,
        findings,
        initialized,
        projects: report.projects,
        incomplete_changes: report.incomplete_changes,
    }
}

pub struct MaintenanceService {
    inspector: StoreInspector,
    memory_store: MemoryStore,
    /// Loaded by the first text that is checked: a diagnosis alone never pays for it.
    policy: PolicySource,
}

impl MaintenanceService {
    pub fn new(
        inspector: StoreInspector,
        memory_store: MemoryStore,
        policy: PolicySource,
    ) -> MaintenanceService {
        MaintenanceService {
            inspector,
            memory_store,
            policy,
        }
    }

    /// The state of the store and everything found in it. `now` is the instant
    /// staleness is counted from (None: the clock), `stale_days` how long a memory may
    /// go without a change.
    pub fn diagnose(
        &self,
        now: Option<&str>,
        stale_days: u32,
    ) -> Result<DiagnosticsReport, WriteError> {
        if stale_days == 0 {
            return Err(ValueError::new("stale_days must be a positive number of days").into());
        }
        // a malformed `now` is the caller's error, and fails before the store is walked
        let now = match now {
            Some(now) => timestamp(now)?,
            None => timestamp(&self::now())?,
        };
        let report = self.inspector.inspect()?;
        Ok(diagnosis(report, now, stale_days, JACCARD_THRESHOLD))
    }

    /// Every stored version of every memory against today's rules; no side effect.
    pub fn scan_policy(&self) -> Result<Vec<PolicyHit>, CoreError> {
        self.memory_store.scan(&|text| self.check_text(text))
    }

    /// The content policy on text not stored yet: the id of the rule it breaks, or None.
    ///
    /// No length rule applies here, and blank text has nothing to withhold.
    pub fn check_text(&self, text: &str) -> Option<String> {
        let refused = self.policy.check(text, text.chars().count())?;
        match refused.rule_id.as_str() {
            "empty" => None,
            "" => Some("rejected".to_string()),
            _ => Some(refused.rule_id),
        }
    }

    pub fn plan_hard_delete(&self, memory_id: &str) -> Result<HardDeletePlan, CoreError> {
        self.memory_store.plan_hard_delete(memory_id)
    }

    /// Deletes every member of the plan, given the set that was shown or its code.
    pub fn hard_delete(
        &self,
        memory_id: &str,
        confirmation: Confirmation<'_>,
    ) -> Result<Vec<String>, CoreError> {
        self.memory_store.hard_delete(memory_id, confirmation)
    }
}

#[cfg(test)]
mod tests;
