//! Schema v6: the one definition a fresh store runs.
//!
//! The text of each statement, SQL comments and indentation included, is what SQLite
//! keeps in `sqlite_master.sql`; stores written by either implementation carry the
//! same text, so no byte of it may change.

pub const SCHEMA_VERSION: i64 = 6;

pub const SCHEMA: [&str; 18] = [
    r#"CREATE TABLE projects (
      id        TEXT PRIMARY KEY NOT NULL CHECK (length(id) = 10),
      name      TEXT NOT NULL CHECK (length(name) >= 1),
      root      TEXT UNIQUE,
      is_global INTEGER NOT NULL DEFAULT 0 CHECK (is_global IN (0, 1)),
      CHECK (is_global = 0 OR root IS NULL)
    ) STRICT"#,
    r#"CREATE UNIQUE INDEX projects_one_global ON projects(is_global) WHERE is_global = 1"#,
    r#"CREATE TABLE memories (
      id             TEXT PRIMARY KEY NOT NULL CHECK (length(id) = 10),
      project_id     TEXT NOT NULL REFERENCES projects(id),
      source_harness TEXT NOT NULL,          -- set at creation, never changed afterwards
      source_method  TEXT NOT NULL,          -- set at creation, never changed afterwards
      created        TEXT NOT NULL,
      version        INTEGER NOT NULL CHECK (version >= 1),   -- the current version
      type           TEXT NOT NULL CHECK (type IN ('user','feedback','project','reference')),
      trust          TEXT NOT NULL CHECK (trust IN ('user','agent','untrusted-derived')),
      sync           INTEGER NOT NULL CHECK (sync IN (0, 1)),
      description    TEXT NOT NULL,
      body           TEXT NOT NULL,
      updated        TEXT NOT NULL,          -- time of the last content or state change
      deleted_at     TEXT,                   -- set while the current state is deleted
      last_read_at   TEXT,
      level          INTEGER CHECK (level IS NULL OR level >= 1),  -- 1 standing, NULL not judged
      tags           TEXT NOT NULL DEFAULT '[]'                    -- JSON array of tag names
    ) STRICT"#,
    r#"CREATE INDEX memories_active_by_project ON memories(project_id, updated DESC) WHERE deleted_at IS NULL"#,
    r#"CREATE TABLE changes (
      change_id    TEXT PRIMARY KEY NOT NULL CHECK (length(change_id) = 10),
      at           TEXT NOT NULL,
      changed_by   TEXT NOT NULL CHECK (length(changed_by) BETWEEN 1 AND 32),
      changed_via  TEXT CHECK (changed_via IS NULL OR length(changed_via) BETWEEN 1 AND 64),
      step_count   INTEGER NOT NULL CHECK (step_count >= 1),   -- immutable
      undoes       TEXT REFERENCES changes(change_id)          -- set when this change is an undo
    ) STRICT"#,
    r#"CREATE TABLE memory_versions (
      memory_id    TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
      version      INTEGER NOT NULL CHECK (version >= 1),
      type         TEXT NOT NULL,
      trust        TEXT NOT NULL,
      sync         INTEGER NOT NULL CHECK (sync IN (0, 1)),
      description  TEXT NOT NULL,
      body         TEXT NOT NULL,
      deleted      INTEGER NOT NULL CHECK (deleted IN (0, 1)),
      change_id    TEXT REFERENCES changes(change_id),   -- NULL for an imported version
      level        INTEGER CHECK (level IS NULL OR level >= 1),
      tags         TEXT NOT NULL DEFAULT '[]',
      PRIMARY KEY (memory_id, version)
    ) STRICT"#,
    r#"CREATE TABLE memory_sources (
      memory_id       TEXT NOT NULL,
      version         INTEGER NOT NULL,
      source_id       TEXT NOT NULL,
      source_version  INTEGER NOT NULL,
      PRIMARY KEY (memory_id, version, source_id),
      FOREIGN KEY (memory_id, version) REFERENCES memory_versions(memory_id, version)
        ON DELETE CASCADE,
      FOREIGN KEY (source_id, source_version) REFERENCES memory_versions(memory_id, version)
        DEFERRABLE INITIALLY DEFERRED
    ) STRICT"#,
    r#"CREATE INDEX memory_sources_by_source ON memory_sources(source_id, source_version)"#,
    r#"CREATE TABLE change_steps (
      change_id      TEXT NOT NULL REFERENCES changes(change_id),
      step           INTEGER NOT NULL CHECK (step >= 1),
      memory_id      TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
      op             TEXT NOT NULL CHECK (op IN ('create','update','soft_delete','restore')),
      before_version INTEGER,                                  -- NULL for create
      after_version  INTEGER NOT NULL,
      PRIMARY KEY (change_id, step),
      UNIQUE (change_id, memory_id)
    ) STRICT"#,
    r#"CREATE INDEX change_steps_by_memory ON change_steps(memory_id)"#,
    r#"CREATE TABLE memory_reads (
      memory_id      TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
      memory_version INTEGER NOT NULL CHECK (memory_version >= 1),
      read_at        TEXT NOT NULL,
      harness        TEXT NOT NULL CHECK (length(harness) BETWEEN 1 AND 64),
      session_id     TEXT CHECK (session_id IS NULL OR length(session_id) BETWEEN 1 AND 128)
    ) STRICT"#,
    r#"CREATE INDEX memory_reads_by_memory ON memory_reads(memory_id, read_at)"#,
    r#"CREATE TABLE sessions (
  harness            TEXT NOT NULL CHECK (harness IN ('claude-code','codex')),
  session_id         TEXT NOT NULL CHECK (length(session_id) BETWEEN 1 AND 128),
  status             TEXT NOT NULL CHECK (status IN ('registered','pending')),
  origin             TEXT NOT NULL CHECK (origin IN ('start','first-seen')),
  project_id         TEXT REFERENCES projects(id),     -- registered: the project or NULL (entry unbound)
  candidate_id       TEXT REFERENCES projects(id),     -- pending: the project to confirm, or NULL
  candidate_root     TEXT,                             -- pending: projects.root seen when the candidate was computed
  entry_cwd          TEXT NOT NULL,
  branch             TEXT,
  transcript_path    TEXT,
  started_at         TEXT NOT NULL,                    -- first time memriver saw the session
  last_active_at     TEXT NOT NULL,
  ended_at           TEXT,                             -- last SessionEnd received
  prompt_count       INTEGER NOT NULL DEFAULT 0 CHECK (prompt_count >= 0),
  last_write_prompt_count INTEGER NOT NULL DEFAULT 0 CHECK (last_write_prompt_count >= 0),
  last_nudge_prompt_count INTEGER NOT NULL DEFAULT 0 CHECK (last_nudge_prompt_count >= 0),
  first_prompt       TEXT,                             -- JSON PromptEntry or NULL
  recent_prompts     TEXT NOT NULL DEFAULT '[]',       -- JSON array of PromptEntry, newest last, at most 5
  summary            TEXT,                             -- the published summary, or NULL
  summary_at         TEXT,                             -- when it was published
  PRIMARY KEY (harness, session_id),
  CHECK (status = 'registered' OR (project_id IS NULL AND origin = 'first-seen')),
  CHECK ((summary IS NULL) = (summary_at IS NULL))
) STRICT"#,
    r#"CREATE INDEX sessions_by_project ON sessions(project_id, last_active_at DESC)"#,
    r#"CREATE TABLE tool_calls (
  harness     TEXT NOT NULL CHECK (harness IN ('claude-code','codex')),
  call_id     TEXT NOT NULL CHECK (length(call_id) BETWEEN 1 AND 256),
  session_id  TEXT NOT NULL CHECK (length(session_id) BETWEEN 1 AND 128),
  recorded_at TEXT NOT NULL,
  cwd         TEXT,                   -- the payload's absolute working directory, or NULL
  PRIMARY KEY (harness, call_id)
) STRICT"#,
    r#"CREATE INDEX tool_calls_by_recorded_at ON tool_calls(recorded_at)"#,
    r#"CREATE TABLE tags (
  project_id  TEXT NOT NULL REFERENCES projects(id),
  name        TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 32),
  description TEXT NOT NULL,          -- one line, with the keywords an agent would look for
  updated     TEXT NOT NULL,
  PRIMARY KEY (project_id, name)
) STRICT"#,
    r#"CREATE TABLE session_moves (
  harness        TEXT NOT NULL CHECK (harness IN ('claude-code','codex')),
  session_id     TEXT NOT NULL,
  seq            INTEGER NOT NULL CHECK (seq >= 1),   -- per session: 1, 2, ...
  moved_at       TEXT NOT NULL,
  from_entry_cwd TEXT NOT NULL,
  from_project_id TEXT REFERENCES projects(id),
  to_entry_cwd   TEXT NOT NULL,
  to_project_id  TEXT REFERENCES projects(id),
  PRIMARY KEY (harness, session_id, seq),
  FOREIGN KEY (harness, session_id) REFERENCES sessions(harness, session_id)
) STRICT"#,
];
