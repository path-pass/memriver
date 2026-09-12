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
     `[[links]]`. Never paraphrase, translate, merge, split, or "improve" it.
   - `name`: the frontmatter `name` (fallback: the filename stem). Keeping
     the original name keeps `[[name]]` cross-references resolvable.
   - `description`: the frontmatter `description`, verbatim.
   - `type`: `metadata.type` when it is one of user/feedback/project/reference;
     anything else falls back to `project` — note the fallback in the report.
   - `scope`: `project`, unless the type is `user` or `feedback` AND the body
     itself says it applies across projects — then `global`.
   - `harness`: `"claude-code"`. Leave `sync` at its default.
3. `memory_index()` again; confirm every migrated name appears.
4. Report a table: migrated / skipped / rejected, each with its reason.

## Collisions and rejections

- Name already taken: `memory_read` it. Same body → already migrated, skip.
  Different body → report both versions to the user and touch nothing; never
  `memory_update` over an existing entry during migration.
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

## Common mistakes

| Mistake | Correct behavior |
|---|---|
| Rewriting or translating bodies "for clarity" | Copy the body exactly as stored |
| Splitting one file into several memories, or renaming ids | One file = one memory under its original name |
| Deleting source files or trimming MEMORY.md after success | The source stays untouched |
| `memory_update` on a name collision | Read, compare, then skip or escalate — never overwrite |
