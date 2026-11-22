//! The memory service: single memories on the agent path, search and the index.
//!
//! The memory store owns single-memory actions and the project store owns
//! collections; this service sequences the policy checks and renders the index. The
//! session facts a write needs (a pending session is refused; a save marks the
//! session) come through two callbacks its composer hands in, so this service never
//! reads the session store. Nothing here touches a file, a table or settings: every
//! limit is handed in.

use std::cell::LazyCell;
use std::collections::BTreeMap;
use std::rc::Rc;

use crate::content_policy::{ContentClassifier, Policy, Verdict};
use crate::models::changes::{MetadataCaps, Op};
use crate::models::context::ProjectContext;
use crate::models::errors::{CoreError, WriteError};
use crate::models::helpers::{LINE_BREAKS, cut, now, py_strip, single_line};
use crate::models::memory::Memory;
use crate::repository::sqlite::memory_store::MemoryStore;
use crate::repository::sqlite::project_store::ProjectStore;
use crate::settings::{DEFAULT_MAX_BODY_CHARS, INDEX_CUE_CHARS, POLICY_RULE_CACHE, Settings};

/// What `MemoryService::index` returns when nothing is visible -- the single source
/// transports compare against.
pub const EMPTY_INDEX: &str = "(no memories yet)";
/// The index of a tag no current global memory carries: a line, not an error.
pub const NO_SUCH_TAG: &str = "(no such tag)";

/// Who an agent's write is recorded as written by.
const AGENT: &str = "mcp";

/// The content policy, loaded when a text is first checked and not before: a caller
/// that only reads (a hook, doctor, the human views) never pays for the rules. One is
/// shared by every service of a composition.
pub type PolicySource = Rc<LazyCell<Policy, fn() -> Policy>>;

pub fn policy_source() -> PolicySource {
    Rc::new(LazyCell::new(|| {
        Policy::embedded().with_cache(POLICY_RULE_CACHE)
    }))
}

/// The limits the service keeps, read once when the services are composed: a changed
/// setting applies to the next process, never to one already running.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MemoryLimits {
    pub max_body_chars: usize,
    /// Metadata keeps its own budget, so that lowering the configured body limit does
    /// not silently tighten what a harness label or a description may be.
    pub metadata_max_chars: usize,
    pub search_limit_default: usize,
    pub search_limit_max: usize,
    pub index_budget_lines: usize,
    pub index_cue_chars: usize,
    pub caps: MetadataCaps,
}

impl MemoryLimits {
    pub fn of(settings: &Settings) -> MemoryLimits {
        MemoryLimits {
            max_body_chars: settings.max_body_chars,
            metadata_max_chars: DEFAULT_MAX_BODY_CHARS,
            search_limit_default: settings.search_limit_default,
            search_limit_max: settings.search_limit_max,
            index_budget_lines: settings.index_budget_lines,
            index_cue_chars: INDEX_CUE_CHARS,
            caps: MetadataCaps {
                standing_max_count: settings.standing_max_count,
                standing_max_chars: settings.standing_max_chars,
                tags_max: settings.tags_max,
                tags_per_memory_max: settings.tags_per_memory_max,
            },
        }
    }
}

/// Refuses a write from a session that is still pending.
pub type RefusePending = Box<dyn Fn(&ProjectContext) -> Result<(), CoreError>>;
/// Moves a session's save watermark; best effort, it never fails the save.
pub type MarkSaved = Box<dyn Fn(&ProjectContext)>;

pub struct MemoryService {
    memory_store: MemoryStore,
    project_store: ProjectStore,
    policy: PolicySource,
    limits: MemoryLimits,
    /// None: no classifier. Asked about new text before any write transaction opens.
    classifier: Option<Box<dyn ContentClassifier>>,
    refuse_pending: RefusePending,
    mark_saved: MarkSaved,
}

fn rejection(message: String, rule_id: &str, detail: &str) -> CoreError {
    CoreError::ContentRejected {
        message,
        rule_id: rule_id.to_string(),
        memory_id: None,
        detail: detail.to_string(),
    }
}

/// A harness label is stored as it is given, so without this it is a channel for
/// secrets or megabytes of text that no policy looks at: letters, digits, `.`, `_`
/// and `-`, at most 64 of them.
fn is_harness_label(harness: &str) -> bool {
    (1..=64).contains(&harness.len())
        && harness
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
}

/// A classifier's category or detail: a short ASCII label only, never a word carried
/// over from the checked content or from a model's answer -- one that is not becomes
/// a fixed placeholder instead of being echoed into the refusal.
fn is_verdict_label(label: &str) -> bool {
    (1..=32).contains(&label.len())
        && label
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-')
}

impl MemoryService {
    pub fn new(
        memory_store: MemoryStore,
        project_store: ProjectStore,
        policy: PolicySource,
        limits: MemoryLimits,
        classifier: Option<Box<dyn ContentClassifier>>,
        refuse_pending: RefusePending,
        mark_saved: MarkSaved,
    ) -> MemoryService {
        MemoryService {
            memory_store,
            project_store,
            policy,
            limits,
            classifier,
            refuse_pending,
            mark_saved,
        }
    }

    // --- single memories ---

    /// The agent write: one new memory in the context's project. `harness` is recorded
    /// as what the change came through.
    pub fn record(
        &self,
        content: &str,
        type_: &str,
        sync: bool,
        harness: &str,
        description: &str,
        context: &ProjectContext,
    ) -> Result<Memory, WriteError> {
        (self.refuse_pending)(context)?;
        let read_write_set = &context.read_write_set;
        let Some(project_id) = &read_write_set.project_id else {
            // no reason: the transport resolved the project, so it says why there is
            // none
            return Err(CoreError::ProjectUnavailable {
                reason: String::new(),
            }
            .into());
        };
        if !is_harness_label(harness) {
            return Err(rejection(
                "invalid harness identifier (allowed: letters, digits, ., _, -, max 64 chars)"
                    .to_string(),
                "invalid-harness",
                "",
            )
            .into());
        }
        self.check(harness, self.limits.metadata_max_chars)?;
        // checked here first so an agent reads the policy's own words; the store
        // checks the resulting state again inside its transaction
        self.check_text(Some(description), Some(content))?;
        self.classify(Some(description), Some(content))?;
        let mut create = Op::create(project_id, type_, description, content);
        if let Op::Create { sync: stored, .. } = &mut create {
            *stored = sync;
        }
        let memory = self.write(&create, context, Some(harness))?;
        (self.mark_saved)(context);
        Ok(memory)
    }

    /// The agent read; records who read which version (best effort, never fails the
    /// read). The session is the context's, when it has one.
    pub fn read(
        &self,
        memory_id: &str,
        context: &ProjectContext,
        harness: &str,
    ) -> Result<Memory, CoreError> {
        let memory = self.memory_store.read(memory_id, &context.read_write_set)?;
        let session_id = context.session_key.as_ref().map(|key| key.session_id());
        match self
            .memory_store
            .touch_read(&memory.id, &now(), memory.version, harness, session_id)
        {
            Ok(()) | Err(CoreError::StorageFailure) => Ok(memory),
            Err(other) => Err(other),
        }
    }

    /// The agent edit; the memory as this call wrote it. `description` None keeps the
    /// stored one. Text equal to the current text is a checked no-op: the rights, the
    /// existence, the deleted state and the version are checked in the write
    /// transaction, and the memory as checked is returned with no version or change.
    pub fn update(
        &self,
        memory_id: &str,
        expected_version: i64,
        content: &str,
        description: Option<&str>,
        context: &ProjectContext,
        changed_via: Option<&str>,
    ) -> Result<Memory, WriteError> {
        (self.refuse_pending)(context)?;
        self.check_text(description, Some(content))?;
        self.classify(description, Some(content))?;
        let mut edit = Op::update(memory_id, expected_version);
        if let Op::Update {
            description: new_description,
            body,
            ..
        } = &mut edit
        {
            *new_description = description.map(str::to_string);
            *body = Some(content.to_string());
        }
        let memory = self.write(&edit, context, changed_via)?;
        (self.mark_saved)(context);
        Ok(memory)
    }

    /// The agent soft delete; the new version. Never marks the session saved.
    pub fn delete(
        &self,
        memory_id: &str,
        expected_version: i64,
        context: &ProjectContext,
        changed_via: Option<&str>,
    ) -> Result<i64, WriteError> {
        (self.refuse_pending)(context)?;
        let delete = Op::soft_delete(memory_id, expected_version);
        Ok(self.write(&delete, context, changed_via)?.version)
    }

    /// The one call to the store's agent entry point, for every method above.
    fn write(
        &self,
        op: &Op,
        context: &ProjectContext,
        changed_via: Option<&str>,
    ) -> Result<Memory, WriteError> {
        self.memory_store.write(
            op,
            &context.read_write_set,
            AGENT,
            changed_via,
            &|description, body| self.check_state(description, body),
            &self.limits.caps,
        )
    }

    fn check(&self, text: &str, max_chars: usize) -> Result<(), CoreError> {
        match self.policy.check(text, max_chars) {
            Some(refused) => Err(rejection(refused.message, &refused.rule_id, "")),
            None => Ok(()),
        }
    }

    /// The body against the body budget; the description, when given and not blank,
    /// against metadata's -- refused in the policy's own words. None means "keep the
    /// current value": nothing new to check.
    fn check_text(&self, description: Option<&str>, body: Option<&str>) -> Result<(), CoreError> {
        if let Some(body) = body {
            self.check(body, self.limits.max_body_chars)?;
        }
        match description {
            Some(description) if !py_strip(description).is_empty() => {
                self.check(description, self.limits.metadata_max_chars)
            }
            _ => Ok(()),
        }
    }

    /// The content policy on one resulting state, as the store asks for it: a rule id
    /// or None.
    fn check_state(&self, description: &str, body: &str) -> Option<String> {
        match self.check_text(Some(description), Some(body)) {
            Err(CoreError::ContentRejected { rule_id, .. }) if rule_id.is_empty() => {
                Some("rejected".to_string())
            }
            Err(CoreError::ContentRejected { rule_id, .. }) => Some(rule_id),
            _ => None,
        }
    }

    /// The content classifier on a write's new text, outside any transaction: a call
    /// can take seconds, and the store's write lock must never wait on it. Only the
    /// candidate text is sent -- its description, when not blank, and its body, joined
    /// by a blank line -- never another memory, a project or an id.
    fn classify(&self, description: Option<&str>, body: Option<&str>) -> Result<(), CoreError> {
        let Some(classifier) = &self.classifier else {
            return Ok(());
        };
        let text = [description, body]
            .into_iter()
            .flatten()
            .filter(|part| !py_strip(part).is_empty())
            .collect::<Vec<_>>()
            .join("\n\n");
        let unavailable = |detail: &str| {
            let detail = if is_verdict_label(detail) {
                detail
            } else {
                "unknown"
            };
            rejection(
                format!(
                    "the content classifier could not check this text ({detail}); no change \
                     was made; see memriver doctor"
                ),
                "classifier-unavailable",
                detail,
            )
        };
        match classifier.classify(&text, AGENT) {
            Verdict::Allowed => Ok(()),
            Verdict::Unavailable { detail } => Err(unavailable(&detail)),
            // the category that says "could not decide" is that answer, with no reason
            Verdict::Blocked { category } if category == "unavailable" => Err(unavailable("")),
            Verdict::Blocked { category } => {
                let category = if is_verdict_label(&category) {
                    category.as_str()
                } else {
                    "invalid"
                };
                Err(rejection(
                    format!(
                        "content rejected by the content classifier ({category}); no change \
                         was made"
                    ),
                    &format!("classifier-{category}"),
                    "",
                ))
            }
        }
    }

    // --- collections ---

    /// The one clamp for a limit an agent gives: the default without one, then at
    /// least one and at most the largest.
    pub fn normalize_search_limit(&self, limit: Option<i64>) -> usize {
        let limit = match limit {
            None => self.limits.search_limit_default,
            // a limit below one is one; one too large for this machine is its largest
            Some(limit) => usize::try_from(limit.max(1)).unwrap_or(usize::MAX),
        };
        limit.min(self.limits.search_limit_max).max(1)
    }

    /// The current project first, then global, in one budget.
    ///
    /// One clamp for the whole answer, then two single-project searches; a spent
    /// budget skips global rather than being clamped back to one.
    pub fn search(
        &self,
        query: &str,
        limit: Option<i64>,
        context: &ProjectContext,
    ) -> Result<Vec<Memory>, CoreError> {
        let read_write_set = &context.read_write_set;
        let mut remaining = self.normalize_search_limit(limit);
        let mut hits = Vec::new();
        for project_id in [
            &read_write_set.project_id,
            &read_write_set.global_project_id,
        ] {
            let Some(project_id) = project_id else {
                continue;
            };
            if remaining == 0 {
                continue;
            }
            let found = self.project_store.search(
                project_id,
                Some(read_write_set),
                Some(query),
                Some(remaining),
            )?;
            remaining -= found.len();
            hits.extend(found);
        }
        Ok(hits)
    }

    fn index_line(&self, memory: &Memory, tag: &str) -> String {
        // an empty body must not break the index, and every stored field is put on
        // one line on its own before the line is composed, so one field's characters
        // never land in another's budget
        let first_line = || memory.body.split(LINE_BREAKS).next().unwrap_or("");
        let raw_cue = if memory.description.is_empty() {
            first_line()
        } else {
            &memory.description
        };
        let cue = single_line(raw_cue);
        format!(
            "- [{}{tag}] {}: {} ({})",
            memory.type_,
            single_line(&memory.id),
            cut(&cue, self.limits.index_cue_chars),
            single_line(cut(&memory.updated, 10))
        )
    }

    /// A standing rule in full, on one line; a body past `standing_max_chars` (stored
    /// before the limit was lowered) is cut there with an ellipsis, never dropped.
    fn rule_line(&self, memory: &Memory) -> String {
        let body = single_line(&memory.body);
        let most = self.limits.caps.standing_max_chars;
        let ellipsis = if body.chars().count() > most {
            "\u{2026}"
        } else {
            ""
        };
        format!(
            "- [rule, global] {}: {}{ellipsis}",
            single_line(&memory.id),
            cut(&body, most)
        )
    }

    fn tag_line(&self, name: &str, count: usize, description: Option<&String>) -> String {
        let about = description.map_or_else(String::new, |description| {
            format!(
                ": {}",
                cut(&single_line(description), self.limits.index_cue_chars)
            )
        });
        format!("- [tag, global] {name}{about} ({count} entries; memory_index tag={name})")
    }

    /// `lines` within the line budget, with the one notice of how many were left out.
    fn budgeted(&self, mut lines: Vec<String>) -> String {
        if lines.is_empty() {
            return EMPTY_INDEX.to_string();
        }
        let all = lines.len();
        lines.truncate(self.limits.index_budget_lines);
        if all > lines.len() {
            lines.push(format!(
                "\u{2026} ({} more entries omitted; use memory_search)",
                all - lines.len()
            ));
        }
        lines.join("\n")
    }

    /// Global's standing rules in full, a line per global tag, global's unjudged
    /// entries, then the current project's entries, in one line budget: a harness
    /// that cuts the end cuts project lines first. A global entry of level 2 or lower
    /// appears only through its tags, or as a plain line when it carries none.
    ///
    /// With `tag`, the current global entries carrying that name instead, as index
    /// lines (a standing one marked `rule`), or `NO_SUCH_TAG`.
    pub fn index(&self, context: &ProjectContext, tag: Option<&str>) -> Result<String, CoreError> {
        let read_write_set = &context.read_write_set;
        let listed = |project_id: &Option<String>| match project_id {
            Some(project_id) => {
                self.project_store
                    .search(project_id, Some(read_write_set), None, None)
            }
            None => Ok(Vec::new()),
        };
        let mine = listed(&read_write_set.project_id)?;
        let shared = listed(&read_write_set.global_project_id)?;
        if let Some(tag) = tag {
            let carriers: Vec<String> = shared
                .iter()
                .filter(|memory| memory.tags.iter().any(|name| name == tag))
                .map(|memory| {
                    let marks = if memory.level == Some(1) {
                        ", global, rule"
                    } else {
                        ", global"
                    };
                    self.index_line(memory, marks)
                })
                .collect();
            if carriers.is_empty() {
                return Ok(NO_SUCH_TAG.to_string());
            }
            return Ok(self.budgeted(carriers));
        }
        // newest first; past the count limit (lowered since) a rule shows as an entry
        // of a lower level does
        let rules: Vec<&Memory> = shared
            .iter()
            .filter(|memory| memory.level == Some(1))
            .take(self.limits.caps.standing_max_count)
            .collect();
        let ruled = |memory: &Memory| rules.iter().any(|rule| rule.id == memory.id);
        let mut counts: BTreeMap<&str, usize> = BTreeMap::new();
        for name in shared.iter().flat_map(|memory| &memory.tags) {
            *counts.entry(name).or_default() += 1;
        }
        let described = match &read_write_set.global_project_id {
            Some(global_id) if !counts.is_empty() => {
                self.memory_store.tag_descriptions(global_id)?
            }
            _ => Default::default(),
        };
        let mut lines: Vec<String> = rules.iter().map(|rule| self.rule_line(rule)).collect();
        lines.extend(
            counts
                .iter()
                .map(|(name, count)| self.tag_line(name, *count, described.get(*name))),
        );
        lines.extend(
            shared
                .iter()
                .filter(|memory| {
                    !ruled(memory) && (memory.level.is_none() || memory.tags.is_empty())
                })
                .map(|memory| self.index_line(memory, ", global")),
        );
        if read_write_set.project_id != read_write_set.global_project_id {
            lines.extend(mine.iter().map(|memory| self.index_line(memory, "")));
        }
        Ok(self.budgeted(lines))
    }
}

#[cfg(test)]
mod tests {
    use std::fs;

    use memriver_test_support::Sandbox;

    use super::*;
    use crate::models::context::{ProjectContextState, ReadWriteSet};
    use crate::models::project::Project;

    #[test]
    fn the_content_policy_is_loaded_only_when_a_write_needs_it() {
        let sandbox = Sandbox::new();
        let tmp = fs::canonicalize(sandbox.dir.path()).unwrap();
        let (root, home, work) = (tmp.join("store"), tmp.join("home"), tmp.join("work"));
        fs::create_dir(&work).unwrap();
        let projects = ProjectStore::with_busy_timeout(&root, &home, 2000);
        let project = Project::new("work", 120).unwrap();
        projects
            .create(&project, &projects.plan_root(&work, None).unwrap())
            .unwrap();
        let service = crate::build_services(&Settings::light(root), &home, None).memory;
        let context = ProjectContext {
            state: ProjectContextState::Registered,
            header: String::new(),
            read_write_set: ReadWriteSet {
                project_id: Some(project.id),
                global_project_id: None,
            },
            project: None,
            diagnostic: None,
            session_key: None,
        };
        let loaded = || LazyCell::get(&service.policy).is_some();
        // what only reads, and what is refused before any text is looked at
        service.index(&context, None).unwrap();
        service.search("anything", None, &context).unwrap();
        assert!(service.read("zzzzzzzzzz", &context, "codex").is_err());
        assert!(service.delete("zzzzzzzzzz", 1, &context, None).is_err());
        assert!(
            service
                .record("b", "user", true, "not a label", "", &context)
                .is_err()
        );
        assert!(!loaded());
        service
            .record("b", "user", true, "codex", "", &context)
            .unwrap();
        assert!(loaded());
    }
}
