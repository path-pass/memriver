---
name: migrate-claude-memory
description: Use when asked to move, import, or copy an existing Claude Code auto-memory store (a memory/ directory with MEMORY.md and per-fact markdown files) into memriver, or when switching a project from native Claude Code memory to memriver.
---

# Migrate Claude Code Memory into memriver

## Overview

Copy every fact file from the native Claude Code auto-memory directory into
memriver through its MCP tools. The migration is faithful and read-only:
it writes to memriver only, and never edits, renames, or deletes anything
in the source directory.

## Source

The native store is `~/.claude/projects/<project-slug>/memory/`. When native
memory is still enabled, your own system prompt names this directory; when it
is already disabled, derive `<project-slug>` from the project's absolute path
with every `/`, `.`, and `_` replaced by `-` (verify by listing
`~/.claude/projects/`). The directory holds `MEMORY.md` (an index) plus one
markdown file per fact with YAML frontmatter (`name`, `description`,
`metadata.type`).

## Procedure

1. `memory_index()` — record what already exists before writing anything.
2. For every `*.md` file except `MEMORY.md`, split the YAML frontmatter from
   the body. One file = one `memory_write` call:
   - `content`: the body exactly as stored — same language, formatting, and
     `[[links]]`. Never paraphrase, translate, merge, split, or "improve" it
     (the store strips only leading and trailing whitespace from the saved
     body; it is not otherwise rewritten).
   - `description`: the frontmatter `description`, verbatim (the store
     strips its leading and trailing whitespace too).
   - `type`: `metadata.type` when it is one of user/feedback/project/reference;
     anything else falls back to `project` — note the fallback in the report.
   - `harness`: `"claude-code"`. Leave `sync` at its default.

   memriver assigns every memory a new id, so the source file's `name` is not
   kept and `[[name]]` cross-references between source files will not resolve
   inside memriver; say so in the report. Every entry is written to the
   current project. Keep the `{id, project_id}` a successful `memory_write`
   returns for every file you write — step 3 needs it.
3. For every file written in step 2, `memory_read(id)` and check that
   `project_id` is the current project and the returned `body`/`description`
   match the source content (compare after stripping leading/trailing
   whitespace from the source, the same normalization the store already
   applies). This read is the acceptance check for that file: a call that
   errors or a mismatch means the file is not confirmed migrated — report it,
   never assume success from `memory_write` alone. Do not use `memory_index()`
   for this: its description is truncated to 60 characters and the whole
   listing is capped by a line budget, so a genuinely written entry can still
   be missing from it. `memory_index()` is only useful as the step 1 overview.
4. Report a table: migrated / skipped / rejected, each with its reason.

## Duplicates and rejections

- Already present: before writing a file, use the index (same description)
  or `memory_search` (a distinctive phrase of the body) only to find
  candidates, then `memory_read` each candidate and compare its full body with
  the file -- never judge equality from an index cue or a search snippet, and
  compare after stripping leading/trailing whitespace from the file's body
  (the store already strips it, so comparing the raw file byte-for-byte can
  flag a real match as different). Same body → already migrated, skip.
  Different body → report both versions to the user and touch nothing; never
  `memory_update` an existing entry during migration.
- `memory_write` rejects the content (secret-shaped text): report the file
  and move on. Do not rephrase content to get past the policy.
- File without parseable frontmatter: skip and report. `MEMORY.md` itself is
  never migrated — memriver generates its own index.

## Hard limits

- Never modify the source directory: no deleting fact files, no trimming
  `MEMORY.md`. Turning native memory off is `memriver install`'s job, not
  the migration's.
- Original `modified` timestamps are not carried over (memriver stamps its
  own); say so in the report instead of encoding dates into bodies.
- If memriver reports `no writable project`, stop and tell the user what the
  message asks for (`memriver project init`, `memriver project explain` or
  `memriver doctor`). Never run any of them yourself.

## Common mistakes

| Mistake | Correct behavior |
|---|---|
| Rewriting or translating bodies "for clarity" | Copy the body exactly as stored |
| Splitting one file into several memories | One file = one memory |
| Deleting source files or trimming MEMORY.md after success | The source stays untouched |
| `memory_update` over an entry that already exists | Read, compare, then skip or escalate — never overwrite |
| Trusting `memory_index()` as proof a file was migrated | `memory_read(id)` each written file; the index is an overview only, truncated per entry and capped in total |
| Routing "cross-project" facts to global | Every migrated file becomes a memory in the current project; global is read-only to agents |
