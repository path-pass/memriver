# memriver

Shared memory layer for coding agents across harnesses (Claude Code / Codex /
Cursor / Kiro), exposed via MCP. One SQLite database is the single source of
truth, and it keeps every version of every memory. Local-only mode uses no LLM
and no network, until you set up the optional `memriver dream` maintenance run,
which sends policy-passing memory and session text to a model (see *Dream*).

Monorepo (uv workspace):

- `packages/memriver-core` — the SQLite memory, history and project store, write gate, change log
- `packages/memriver-dream` — the harness-neutral offline maintenance run behind `memriver dream`
- `packages/memriver` — CLI + MCP server, the executors (your `claude -p` / `codex exec`, and jev through Pydantic AI) and the built-in content classifier (the package users install)
- `skills/` — agent skills that ship with the project (see *Migrating existing Claude Code memory*)
- planned: `memriver-vector` / `memriver-sync`
  (installed on demand via extras, e.g. `memriver[vector]`)

## Install into your harnesses

```bash
uvx memriver install            # every supported harness, interactive
uvx memriver install --harness claude-code
uvx memriver install --dry-run  # show the plan, write nothing
uvx memriver install --yes      # accept every change shown
```

`install` plans every change first and shows it before writing anything. Each
harness configuration file it touches gets a sibling backup
(`<file>.memriver-backup-<stamp>`) taken from the exact bytes it is about to
replace; a failure part-way through rolls every touched harness file back.
Symlinked targets are refused. What it writes, per harness:

| Harness | MCP server | Session hooks | Static instructions |
|---|---|---|---|
| Claude Code | `uvx memriver serve --harness claude-code` | `SessionStart` + `UserPromptSubmit` + `Stop` + `SessionEnd` + `PreToolUse` (memriver tools only) | — |
| Codex | `uvx memriver serve --harness codex` | `SessionStart` + `UserPromptSubmit` + `Stop` + `SessionEnd` | — |
| Cursor | `uvx memriver serve --harness cursor` | — | marker block in the project's `AGENTS.md` |
| Kiro | `uvx memriver serve --harness kiro` | — | `.kiro/steering/memriver.md` |

Claude Code and Codex are session-routed: `--harness claude-code|codex`
decides each session's project once, at its `SessionStart`, and every hook
and every MCP call for that session then read the same stored decision, so
the hook and the server always agree on it. A resumed or compacted session
with no row yet gets a proposed project from its `SessionStart` itself --
no prompt needed -- and stays pending until the user agrees and the agent
calls `session_confirm`; only a session `SessionStart` never got to record
(one memriver truly never saw start) waits for its first observed prompt to
get that same proposal instead (see *What your agent sees*). Cursor and
Kiro stay directory mode: `--harness cursor|kiro` resolves `--project-dir`
once, when the server starts, and that never changes for the life of the
process.

Claude Code keeps its MCP server running across `/clear` and an in-app
`/resume`, so the server's own environment keeps naming the session it
started with. memriver follows the current session anyway: its
`PreToolUse` hook, matched to memriver's own tools only
(`mcp__memriver__.*`), records which session is making each memriver tool
call, and the server answers that call for that session; with no record
it falls back to the session the server started with. The cost is one
short hook run before every memriver tool call, never before any other
tool. The hook only records: it never blocks or changes the call.

Turning off the harness's own memory feature (Claude Code, Codex) is a
separately confirmed change, never implied by the rest. Codex only runs hooks
it has been told to trust: after installing, open `/hooks` in the Codex TUI
and review and trust the memriver entries -- `SessionStart`, `UserPromptSubmit`,
`Stop` and `SessionEnd` -- or they are skipped; a hook whose definition
changes needs re-trusting the same way. `--dry-run` prints the exact files
and entries for your machine.

Until memriver is on PyPI, the `uvx memriver` commands above need a local
wheel source — see *Installing before the PyPI release*.

When the memory store has no global project yet, initializing it is required
to continue installation. The full plan shows this prerequisite alongside the
harness changes; its prompt asks whether to initialize and continue, and
declining cancels installation without changing files. Initialization runs
before any harness file is written; if it fails, no harness file is written.
A later harness-file failure rolls back the harness files but leaves an
already-initialized (empty) global project in place -- store initialization
and the harness writes are not one transaction. A reinstall on an initialized
store has nothing to create and asks nothing about it. `--dry-run` writes
nothing, and without `--yes` initialization needs an interactive terminal.
Plans and successful results go to stdout; installation failures and rollback
reports go to stderr.

## What your agent sees

- **Session start** (Claude Code, Codex — including resume and after a
  compaction): the memory index is injected into the agent's context inside
  explicit delimiters, preceded by a notice that the entries are data, not
  instructions. The injection is sized in the harness's own metric (Claude
  Code: 10,000 UTF-16 code units; Codex: about 2,500 tokens) and truncates
  whole lines with an omission notice rather than cutting mid-entry.
- **A session's project is fixed at its start.** A Claude Code or Codex
  session gets its project once -- from its first `SessionStart` -- and keeps
  it for the session's whole life: opening a sub-directory of a monorepo
  later never moves it, and a session started inside a linked git worktree
  registers under the project of the worktree's main working tree, keeping
  the sub-directory it started in. The same session id keeps its row however
  and wherever it is later resumed. A session started in no registered
  project has none, and memriver never looks again on its own: it gets one
  only when the agent calls `session_register` -- because you asked it to,
  or right after it ran `memriver project init` at your request -- which
  resolves the directory the session started in (as stored when it
  registered, not wherever it is now) and registers the project covering
  it. A session that already has a project never changes.
- **A session memriver has never registered** (typically one resumed for the
  first time since before you installed) is not silently bound to whatever
  directory it happens to be resumed in. It is
  marked "awaiting confirmation": only global memory is readable, every write
  is refused, and the agent is told to ask you and call `session_confirm`
  only once you agree to the project it proposes. When the directory it was
  first observed in is in no registered project there is nothing to propose:
  the agent is told that saving needs `memriver project init` there followed
  by `session_register`, and is not asked to confirm.
- **Stop**: a reminder to save durable facts, but only for a session's own
  prompts (not a sub-agent's) and only once it has made at least 5 prompts
  since its last save -- a successful `memory_write` or `memory_update`
  counts as a save, `memory_delete` does not -- and then at most once every 5
  prompts after that, so a session that saves as it goes is never interrupted.
- **`memory_read`** records that a memory was read: `last_read_at`, and one
  read record (the version read, the time, the server's `--harness`, and the
  calling session when the server is session-routed). Neither is returned to
  the agent; `memriver show`/`export` print `last_read_at`, and `memriver
  dream` counts the reads when it decides how long a memory may go unused
  (*Dream*). A read never creates a version.
- **Cursor / Kiro** use the static instructions block instead of hooks, and
  have no session routing: they resolve `--project-dir` once, when the
  server starts, and never re-resolve it for the life of that process.

Hooks never fail the harness on a store they can degrade: an unreadable store
is stated inline, in the injected header itself, as a labelled "unavailable"
session -- exit 0, empty stderr, nothing blocked. Of the five, only
`SessionStart` ever writes to stderr for a failure of this kind: when it hits
something it cannot route around at all (a malformed payload, some other
unhandled failure) it skips the injection entirely and prints one fixed,
path-free stderr line instead, still exit 0. `UserPromptSubmit`, `Stop`,
`SessionEnd` and `PreToolUse` degrade the same failures silently -- empty
stdout, empty stderr, exit 0 -- since none of them owes the agent a header.
Two deliberate exceptions: an invalid `settings.toml` or `MEMRIVER_*` value
makes `SessionStart`, `UserPromptSubmit` and `SessionEnd` print their one-line
settings error and exit 1 -- a failing, non-blocking hook the harness shows --
because a broken settings file needs fixing rather than being silently
ignored (see *Settings*); and a store below schema version 4 is refused --
every hook does nothing. If memories seem to
be missing, `memriver doctor` shows what the store actually holds.

Known limits: `session_search`/`memriver sessions` only look at each
session's first prompt and its five most recent, each saved as at most 512
characters; a prompt that is too large, looks like a secret, is invalid, or
fails the scan is stored with no text at all, only a fixed reason. A keyword
that appears only in a middle prompt that got dropped, or in text that was
truncated or omitted, will not be found. The first prompt is never dropped,
so its saved text still matches however early in the session it was -- but
a first prompt that was omitted has no text to match. And memriver never
checks whether a session can still be resumed -- the harness may already
have deleted its transcript (Claude Code prunes transcripts after
`cleanupPeriodDays`, 30 days by default).

## Projects

A project is a directory you registered. Nothing else confers project
identity -- not a `.git` directory, not a marker file -- and the `memriver
project` commands never write anything inside a project directory; each
project's directory binding lives on its row in the store. (Installing Cursor
or Kiro still writes their static instruction file at the git root, as the
install table shows.)

```bash
uvx memriver project init                 # register the current directory
uvx memriver project init ~/99_git/work   # register a parent folder holding several repos
uvx memriver project init --name "Work"   # the project's readable name (default: the directory name)
uvx memriver project adopt <id> <dir>     # bind an unbound project (unbind first if it has a directory)
uvx memriver project unbind <id> <dir>    # drop its binding (one directory per project); memories stay
uvx memriver project explain              # what the current directory resolves to
```

Every directory under a registered root -- including repositories added
later -- shares that project's memories; the nearest registered ancestor
wins, so registering a sub-directory carves it out as its own project. An
unregistered directory has no project: agents can read global memory
but have nowhere to save, and the session-start injection says so. Global
memory is read-only to agents; `memriver dream` writes it, and a person
manages it with the commands of *Browsing and managing memories directly*.
A binding change reaches Cursor/Kiro at their MCP server's next
start. It reaches Claude Code/Codex in a new session, or in a session that
has no project yet once its agent calls `session_register` (on your request,
or right after running `memriver project init` for you): a session's stored
project never changes once it has one, existing and resumed sessions
included, so a session already registered to another project has to be
replaced by a new session to save under the new binding.

Known limits:

- A directory that reuses a registered path inherits that project's memories.
- A root as wide as a whole workspace is, in effect, a writable global.
- Kiro multi-root workspaces start every MCP server in the first root:
  resolving a different project per root is not supported there.
- Mapping a linked git worktree to the project of its main working tree is a
  Claude Code/Codex session-registration feature only; directory mode
  (Cursor, Kiro, a bare `memriver` process) resolves the worktree path
  itself, never its main tree. Two limits apply to that mapping: git is
  queried with the user's global and system git config ignored (its global
  config is pointed at the OS's null device, its system config disabled), so
  a repository whose ownership needs a global `safe.directory` entry -- on an
  external disk or a container mount, say -- cannot be mapped, and a session
  started there is not registered to any project; and a repository created
  with `git init --separate-git-dir <X>/.git <work>` maps its linked
  worktrees into `<X>`, not `<work>`.
- After upgrading memriver, run `memriver install` again: the harness
  configuration an earlier release wrote lacks the newer hooks and the
  `serve --harness` argument, so without it prompts are not counted, `Stop`
  never nudges, and the MCP server stays in directory mode. Then restart
  every harness session and MCP server that shares the store; a running
  server keeps its old rules. This release's store is schema version 4: a
  store written by an earlier release is refused -- hooks do nothing, MCP
  tools answer that the store's schema version is not supported and change
  nothing, and every other command, `doctor`, `memriver dream run` and
  `dream report` included, exits 1 with one line naming the store's schema
  version; an earlier memriver refuses a v4 store. A session that already
  has a row keeps being routed by it regardless of a software update (see
  *What your agent sees*).
- A store written by the pre-SQLite file layout (`global/`, `store.toml`,
  `projects/`, `memories/`, `registry/`) is not read or migrated; `memriver
  doctor` reports it as `legacy-layout`.
- Confirmation prompts protect against mistakes, not against an agent with a
  shell: the store is a database file your user can write.

## Tools

| Tool | Purpose |
|---|---|
| `memory_index()` | The session's project on the first line, then a compact index: the project's memories, then global's (tagged `global`) |
| `memory_read(memory_id)` | One memory in full, by id: eleven fields, including the `version` that `memory_update`/`memory_delete` must be given back. An entry that exists but cannot be read is reported as such, not as missing |
| `memory_search(query, limit=None)` | Memories relevant to a task, by space-separated keywords: an entry matching any keyword is returned, entries matching more keywords first (see below); the project's hits first, then global's; `limit` caps the whole answer, so a query with many project hits can leave no room for global ones |
| `memory_write(content, type, sync=True, description="")` | Save one durable fact to the current project; memriver assigns the id and stamps `source.harness` with the server's own `--harness` value (an installed Cursor/Kiro server records `cursor`/`kiro`; `unknown` only when the server was started with no `--harness` at all); global is read-only to agents; `type` is `user` / `feedback` / `project` / `reference` |
| `memory_update(memory_id, expected_version, content, description=None)` | Rewrite a memory's content in place (id, project and type stay); returns `{id, updated, version}`; refused for global memories or a stale `expected_version` |
| `memory_delete(memory_id, expected_version)` | Remove a memory that is no longer true or wanted; returns `{deleted: memory_id}`; refused for global memories or a stale `expected_version` |
| `session_search(query="", limit=None)` | Claude Code/Codex only: find this project's recorded sessions by space-separated keywords in their prompts, summary, branch or entry directory -- an entry matching any keyword is returned, entries matching more keywords first, newest activity first among equals; an empty query lists them, newest activity first; each result carries a `resume_command` to show the user -- whether to run it is the user's decision |
| `session_confirm()` | Claude Code/Codex only: register the calling session to the project memriver proposed for it; call only after the user agrees; returns the session's new project header |
| `session_register()` | Claude Code/Codex only: register the calling session, when it has no project, to the registered project covering the directory it started in (the one stored when it registered); called when the user asks, or right after the agent ran `memriver project init` at the user's request; never changes a session that already has a project; returns the session's project header, with a note when no project covers that directory |

`memory_search`, `session_search`, `memriver search` and `memriver sessions`
take space-separated keywords: an entry matching any keyword is returned,
entries matching more keywords first. The query is split on whitespace and on
`,`, `;`, the ideographic comma (U+3001) and the full-width comma and
semicolon (U+FF0C, U+FF1B); repeated keywords count once, and only the first
16 are used. Matching ignores case and full-width/half-width differences
(NFKC) and finds a keyword anywhere in a word, so `uv` also matches `uvx`.
Hits are ordered by, in turn: the whole query (the keywords joined by one
space) appearing as written (after the same folding), how many keywords match, for memories how many
match in the description, then newest first. There is no phrase, boolean or
fuzzy syntax. A query with no keyword matches nothing, except the empty
session query: `session_search()` lists this project's sessions and `memriver
sessions` without `QUERY` lists every session, newest activity first (both up
to their limit).

`expected_version` is the value `memory_read` last returned; if the memory
changed since, the call is refused and nothing is written -- read it again and
redo the edit on the current text. Every write passes the content policy
(secret-shaped content is refused, the value is never echoed back) and the
size limits from *Settings*. An operational failure inside a tool comes back
as an MCP tool error (`isError: true`) carrying the same path-free message,
never as a raw exception through the transport; a call that does not match a
tool's schema — an unknown argument, a missing one, a wrong type — is
rejected by the MCP layer before the tool runs.

When the content classifier is configured (*Content classifier*), a
`memory_write` or `memory_update` it blocks is refused with `content rejected by the
content classifier (<category>); no change was made`, and one it could not check with
`the content classifier could not check this text (<reason>); no change was made; see
memriver doctor`. `memory_delete` carries no new text and is never checked.

Every successful `memory_write`, `memory_update` or `memory_delete` is one
change in the store's change log, recorded as made by `mcp` through the
server's `--harness`; an update that changes nothing records nothing. Agents
never see history or the change log; a person does, with `memriver history`
and `memriver undo` (*Browsing and managing memories directly*).

The memory model — its fields, the four types, id rules, the strict boolean
and timestamp formats — is specified in
[`docs/memory-model.md`](docs/memory-model.md).

## Content classifier

Built in, and off until you configure it. Every write already passes the content
policy (*Tools*); the content classifier adds a model's judgment before memriver stores
new memory text: would storing it plant instructions in a future agent's context -- a
command addressed to an agent beyond recording a fact or preference, text that tries to
steer the model that reads it, a request to send data, credentials or files anywhere?
Such text is refused.

**Without a `[classifier]` table in `settings.toml`, memriver calls no classifier at
all, so no model outside your harness ever sees a memory.** With one, the table names
the executor that answers: memriver never picks one for you. Nothing extra is
installed, and `memriver install` registers the MCP server the same way either way.

What is checked: the new description and body of every agent write (`memory_write`,
`memory_update`) and of every dream change that carries new text (a merge, a rewrite,
a new or supplemented global entry) -- after the content policy, before the write, and
nothing else: no other memory, no project, no id. Deletes, restores and undos carry no
new text and are never checked, and neither is anything you do with memriver's own
commands.

Executors:

- `claude` / `codex`: a clean headless run of your harness -- `claude -p` or `codex
  exec` with the isolation dream's executors use (no tools, no MCP servers, none of
  your settings, an empty temporary directory, memriver's own hooks inert) -- that
  answers allow, or block with a category (`instruction`, `injection`,
  `exfiltration`). It uses your harness's login. `model` picks the model (a small one,
  such as `model = "haiku"`, answers faster); `claude_settings` names a settings file
  that is reloaded whole into that call -- its hooks, MCP servers and environment run
  there too, so it must hold authentication only (`apiKeyHelper`, a Bedrock/Vertex
  environment block) and never a hook, which would see the memory text being
  classified (put only auth there, as for dream: *Dream*, Executors).
- `jev`: TypeSafe's hosted classifier (`https://api.typesafe.ai`, early access, a key
  from their waitlist), asked through Pydantic AI for one number: the probability that
  storing the note plants instructions, with the criteria in that number's
  description. The text is blocked (category `unsafe`) when the probability is at
  least `block_threshold`. `model` picks the jev model (`jev-latest` when unset). The
  key is read at each call from the environment variable `api_key_env` names --
  `settings.toml` holds only the variable's name -- and memriver never logs or prints
  the key or the request; a request is never retried and never follows a redirect.
  **With `jev`, every checked memory text is sent to TypeSafe's servers**; their
  retention terms are not published in their documentation, and language coverage
  (for example Chinese) is not documented.

Fail closed: when the classifier cannot decide, the write is refused, with the reason,
and nothing is written: `timeout`, `login` (a harness that is not logged in, or no
key, or a key jev refuses), `quota`, `too-large` (input too large for the model),
`start` (the harness could not be started), `unparsable` (an answer that does not fit)
or `exit` (any other failure, an HTTP error among them). Dream lists each change the
classifier blocks under *Needs you* -- a classifier that cannot check at all once per
run, with its reason -- and tries again next run. `enabled = false` turns the
classifier off and keeps the table; `agent_writes = false` or `dream_writes = false`
stops checking one source. `memriver doctor` states whether the classifier is
configured, off, or which executor it uses (it never calls a model).

Latency: each checked write waits for the answer. A headless harness run takes
seconds (a smaller `model` helps); TypeSafe documents 70-500 ms per `jev` request.

**Scheduled dream runs.** `memriver dream run`'s LaunchAgent (*Dream*, Schedule) starts
with only `HOME`, a `PATH` and `MEMRIVER_ROOT` -- no other environment variable reaches
it. A `jev` executor's `api_key_env` variable, and a `codex` executor's own
`codex_overrides` `env_key`, are invisible there too, so the 04:00 run finds no key,
refuses every text-carrying dream change as classifier-unavailable, and tries again the
next night for nothing (the executor still ran and was paid for). If you schedule
`dream run` with a `jev` or `codex` classifier, make that variable visible to your
login session yourself, the same way as for `[dream.codex_overrides]` (`launchctl
setenv NAME value` after each login). A `claude` classifier using your harness's
subscription login needs nothing added.

```toml
# ~/agent-memory/settings.toml -- [classifier], every key shown with its default
# except executor and executor_path, which have none
[classifier]
executor = "claude"                   # "claude", "codex" or "jev"
executor_path = "/absolute/path/to/claude"   # required for claude and codex
enabled = true                        # false: off, the table kept
agent_writes = true                   # check memory_write and memory_update
dream_writes = true                   # check dream's changes
# model = "haiku"                     # the model to ask; jev: "jev-latest" when unset
# claude_settings = "/absolute/path/to/auth-settings.json"
# timeout_s = 60                      # per call; the default is 60 (claude/codex), 10 (jev)
api_key_env = "TYPESAFE_API_KEY"      # jev: the variable that holds the key, never the key
block_threshold = 0.7                 # jev: block at or above this probability

# codex only: the same keys and rules as [dream.codex_overrides] (see Dream)
# [classifier.codex_overrides]
```

## Browsing and managing memories directly

```bash
uvx memriver list [--project ID]                      # every project, or one; id, type, date, cue
uvx memriver show ID [--deleted]                       # full memory: header fields + body
uvx memriver search QUERY [--project ID] [--limit N]
uvx memriver export DIR                                # DIR must not exist; a markdown snapshot, never read back
uvx memriver history ID [--show N]                     # every version of one memory; --show N prints version N's body
uvx memriver restore ID --to N [--yes]                 # make version N's state current again
uvx memriver undo CHANGE_ID [--yes]                    # reverse one change
uvx memriver delete ID --version N [--yes]             # soft delete
uvx memriver delete ID --hard [--dry-run | --confirm CODE]   # delete a memory with its whole history
```

These are commands for a person, not the MCP surface agents use; MCP has no
path to history, restore, undo or a hard delete. `search` ranks its hits as one
list across the projects it reads (see *Tools*). `list`/`search`/`export` see
every project, including global, but never a soft-deleted memory; `show
--deleted` is the one view that can, and it prints the memory's `deleted_at`.
`show` and `export` also expose `last_read_at`, set by a successful
`memory_read`: `show` prints it as `never` until the first one, `export`
writes the same field as JSON, `null` until then -- neither is part of what an
agent can read.

**History.** Every change of a memory's description, body, sources or deleted
state creates a new version, and old versions are kept for good; a write that
changes nothing creates none. Every one of those writes -- an agent's
`memory_write`, `memory_update` or `memory_delete`, a `memriver dream`
change, `restore`, `undo`, a soft `delete` -- is one *change* with a change
id, recording who made it (`mcp`, `dream` or `human`, and through which
harness) and which versions of which memories it produced. A hard delete
makes no change of its own (*Hard delete*). `history ID` lists every version
of one memory, global included: its number, time, who made it, its change id, whether it is
deleted, its description and the ids of the memories it cites; `--show N`
prints that version's body. A version with no change of its own shows as
`imported`.

**Restore.** `restore ID --to N` shows the current version and version N --
what changes in content, sources and deleted state -- asks, and makes version
N's full recorded state -- trust and sync included -- the new current version
(a new version; nothing is rewritten). Restoring a version that was not
deleted undeletes a memory. If the memory changed while you looked, it shows
the new state and asks again. The content policy applies: a version that
fails today's policy cannot be restored, and the rule is named.

**Undo.** `undo CHANGE_ID` shows the change -- each memory it touched and its
project, global flagged -- and what undoing it does, asks, and applies the
reverse as one new change: a created memory is soft-deleted, anything else
goes back to the version it had before. It is refused, with nothing changed,
when any memory the change touched has changed since (`changed`, naming them;
a read does not count, text changed back to the same words does), when a hard
delete removed part of it (`hard-deleted`), when the reverse fails the
content policy (the rule is named), or when there is no such change
(`not-found`). An undo is itself a change and can be undone the same way;
undoing an older change after a later one touched the same memories is
refused -- `restore` handles that case. An imported version belongs to no
change and cannot be undone.

**Delete.** `delete ID --version N` soft-deletes, and needs the `version` that
`memriver show` printed. A global memory is deleted by id, from anywhere; any
other memory only from its own project's directory: `delete` resolves the
command's current directory the way directory mode does, which is not always
the project a session-routed agent would use (a session registered in A but
resumed from B can delete only in A; running `delete` from B at that same
moment acts on B). A soft-deleted memory keeps its history and comes back
with `restore`. It passes the content policy like any other write: if the
memory's stored text now fails a rule, the soft delete is refused and names
the rule, pointing at `delete ID --hard` (`--dry-run` first, then
`--confirm`) instead.

**Hard delete.** `delete ID --hard` is the only thing that removes versions,
and it makes no change of its own: with no content-policy check, it deletes
the plan's rows outright -- the memory with its whole history, together with
every memory that cites any version of it, repeated until nothing outside the
set cites a member (it never follows a member's own sources). It prints that
plan -- each memory, its project, its current version, whether it is deleted
and the citations that pulled it in -- asks, and deletes exactly the set
shown; if the set changed meanwhile, it prints the new plan and asks again.
`--dry-run` prints the plan and a command carrying `--confirm CODE`; that
command deletes without asking only while the plan still matches the code,
and otherwise exits 2 and deletes nothing. `--version` is not accepted with
`--hard`, and `--dry-run` and `--confirm` only go with `--hard`. Existing
change log entries a hard delete touched stay, without the deleted memories'
steps, and can no longer be undone -- but the hard delete itself adds no new
one. This is how a secret already in the store is removed even once its text
fails today's policy: a soft delete would be refused (above), but a hard
delete runs no such check. It zeroes the text inside the store file itself.
It cannot reach a copy made outside the store -- a backup, or the harness's
own transcript the secret came from -- or free disk blocks that still hold
the text, such as the ones the temporary journal SQLite writes during the
delete leaves behind once it is removed.

## Sessions

```bash
uvx memriver sessions [QUERY] [--project ID] [--limit N] [--json]
```

Lists every recorded Claude Code/Codex session (or one project's), newest
activity first: harness, project, branch, when it was first recorded and last
active, its last `SessionEnd`, its first and latest prompt, a resume command
(`claude --resume <id>` / `codex resume <id>`), and the session's summary once
`memriver dream` has written one. `QUERY` is space-separated keywords (see
*Tools*), matched against a session's saved prompt text (the same
first-plus-five, 512-characters-each scope `session_search` has -- see its
Known limits above), its summary, branch or entry directory, and ranked the
same way `session_search` ranks the calling session's own project; unlike
`session_search`, this sees every project. `--json` emits the same item shape
`session_search` returns.

## Dream: offline maintenance

```bash
uv tool install memriver   # the schedule needs a persistent memriver, not uvx's cache
memriver dream init [--executor claude|codex] [--ttl-days N] [--at HH:MM] [--yes]
memriver dream run [--phase summarize|consolidate|extract|retire]
memriver dream report [RUN_ID] [--list [N]]
memriver dream uninstall
```

`memriver dream` is an offline batch run, started every day by a schedule or
by hand with `dream run`. One run at a time: a run that finds another one
still going is recorded as skipped. Each run, in order:

1. **Checks every stored version against the content policy** -- every
   project and global, deleted memories and old versions included -- and lists
   each hit under *Needs you* in its report by id, version, rule and whether
   it is the current version, never the matched text. Nothing is deleted:
   removing a secret for good is a hard delete you run (`memriver delete ID
   --hard`). A memory whose current version hits is left out of every model
   step of the run. No model is involved, so this also runs with no executor
   configured: `memriver dream run` before `init` is a policy scan.
2. **Summarizes sessions**: each Claude Code/Codex session bound to a project
   that has new activity since its last summary gets a summary of its
   transcript -- goal, what was done, results, open items, identifiers kept
   verbatim -- which `session_search` and `memriver sessions` search and show.
   A long session is summarized part by part and merged, and one too long for
   a run keeps its progress for the next. A session whose transcript cannot be
   read waits for new activity; a failed call, or a summary the content policy
   refuses, is tried again by the next run. Transcript records that fail the
   content policy are never sent.
3. **Consolidates each project**: it merges entries that state the same fact
   into one new entry citing them and soft-deletes the originals, rewrites an
   entry that another entry of the project shows to be outdated (citing that
   evidence), soft-deletes an entry a newer one explicitly replaces, and lists
   contradictions and entries that are instructions addressed to an agent
   under *Needs you* without changing them. Only the project's own memories
   count as evidence, and no change is preferred when in doubt.
4. **Consolidates global** the way step 3 consolidates a project, before global
   memories are sent anywhere else: an entry this step flags as an instruction
   addressed to an agent is excluded from every later step of the run, and the
   step is not counted as finished, so the next run judges global again first.
5. **Extracts shared principles into global**: a principle backed by memories
   of at least two projects becomes a global entry citing them, or supplements
   an existing one; what only one project says stays there. Principles, not
   commands: "Python projects prefer pytest for tests", never "pytest -q". A
   global entry whose cited sources changed since is re-checked against their
   new versions: kept, pointed at the new versions, revised, or listed under
   *Needs you* as overturned.
6. **Retires memories unused past their TTL**, after asking the model whether
   there is reason enough to retire each one: a memory's last use is the
   latest of its creation, its last update and its last `memory_read`, and its
   TTL is `ttl_days` times one plus its recorded reads, capped at
   `ttl_read_multiplier_max` times. `keep` leaves it alone for another
   `ttl_days`; `delete` soft-deletes it; `uncertain_limit` uncertain answers in
   a row about the same version soft-delete it too. A read that lands while
   the model decides keeps the memory.
7. Writes its report, and deletes runs and reports older than
   `report_retention_days`.

`--phase` runs one step after the policy scan: `summarize` (2), `consolidate`
(3 and 4), `extract` (5) or `retire` (6). A consolidation or extraction pass
is skipped while the memories it would read are unchanged since its last
finished pass.

**Changes and undo.** Every change dream makes goes through the same change
log as any other write, recorded as `dream` with its executor's harness, and
takes effect at once, without a review step. Dream never hard-deletes. Its
report lists each change with its memories, their versions and a
one-sentence reason, followed by the command that reverses it, `memriver undo
<change_id>` -- refused once any memory the change touched has changed since,
where `memriver restore` takes over (*Browsing and managing memories
directly*). A session summary is not a memory change and has no undo. Every
change to global -- a global consolidation, an extraction, a re-check, a global
memory's retirement -- is also listed under *Needs you* with its undo command,
so a change every project reads is never missed. A change the content
classifier blocks (*Content classifier*) is not made; it is listed under
*Needs you* (a classifier that cannot check at all is listed once per run, with
its reason) and the step runs again next run.

**Reports.** `memriver dream report` prints the latest run's report (or
`RUN_ID`'s) as it was written: the run, its trigger and executor; each change,
by project; the TTL decisions; skips and failures with their reasons; *Needs
you* -- content-policy hits with their `memriver delete ID --hard` command,
contradictions, instruction-like entries, overturned global entries and
refused extractions, every change to global with its undo command, and
changes the content classifier blocked (a classifier that could not check at
all: one line per run, with its reason), inputs too large for
`context_budget_tokens` (each by
scope or id, with its estimate and the room -- raise the setting when our own
estimate rejected the input before any call, lower it when the executor
itself refused an input that fit our estimate) and inputs above 70% of it (a
call that itself ended too large never also gets this line); the first login
failure and the first quota failure of the run, with what to check; and
how the run finished. `--list [N]` lists the last N
runs (default 10) with time, trigger and status. A run that stopped without
finishing is shown as interrupted, and the next run marks each of its changes
whose outcome is unknown with the `memriver history` command that settles it.
A report never holds a memory body, a model's full input or output, or a
secret; a description or model reason that fails the content policy appears
as `(withheld)`. `memriver dream run` prints its report too (a scheduled
run prints one line instead). It exits 0 when
the run completed (failures of single items included), was skipped, or only
scanned because no `[dream]` table exists; and 1 on a store failure, an
invalid `[dream]` table, `--phase` with no executor configured, a store below
schema version 4, or a missing Codex provider variable (below).

**Executors.** `init` picks Claude Code or Codex (`--executor`; default the
configured one, else whichever of `claude` and `codex` is on `PATH`) and
stores its absolute path. Each model call is one headless run -- `claude -p`
with memriver's own system prompt, no tools, no MCP servers and none of your
settings files, or `codex exec` ephemeral, in a read-only sandbox, ignoring
your Codex config, with hooks and the built-in tools switched off, but reading
your global `AGENTS.md` -- in an empty temporary directory, with your existing
login, and with `MEMRIVER_ROOT` pointed at a path that does not exist so
memriver's own hooks inside it do nothing. The prompt goes to the harness on
its standard input. A failed call (timeout, not logged in, over quota, input
too large, unreadable answer, a harness that cannot be started) is retried by
the next run; dream never switches executor. The executor's provider receives
memory text and session transcripts; memories and transcript records that
fail the content policy are left out.

A login or quota failure is listed once under *Needs you* (the run goes on and still
completes). For API-key, Bedrock or Vertex authentication, Claude Code's own settings
files are ignored in an executor run, so give dream a settings file of its own with
`claude_settings = "/absolute/path/to/auth-settings.json"`: it is passed as `--settings`,
which `--restricted` still honours. Put only authentication in that file (an
`apiKeyHelper`, the Bedrock or Vertex `env` block): hooks or MCP servers in it would run
inside the executor. For Codex, give the provider in `[dream.codex_overrides]` (below).

What an executor run can and cannot do:

- Claude Code runs with `--tools ""`, `--strict-mcp-config` and
  `--restricted`: no built-in tool, no MCP server, and your user, project and
  local settings files -- hooks included -- are ignored. Managed (enterprise)
  settings, and their hooks, still apply.
- Codex runs with `--ignore-user-config` (your `config.toml`, its MCP servers
  included, is not loaded), `--disable hooks` (ignoring the config does not
  stop hooks on its own) and switches that turn off the built-in tools (shell,
  exec, image, multi-agent, goals, plugins, web search), in a read-only
  sandbox. One harmless tool stays registered, `request_user_input`, and your
  global `AGENTS.md` is read. The switches are Codex feature names; if a Codex
  release renames one, the call fails and the next run reports it. Because
  `config.toml` is not loaded, a model provider defined only there is not
  used unless you give it in `[dream.codex_overrides]` (below).
- Both use your login, and memriver's own hooks do nothing there.

**Schedule.** On macOS, `init` installs a per-user LaunchAgent,
`~/Library/LaunchAgents/io.github.path-pass.memriver.dream.plist`, that runs
`memriver dream run` daily at `--at` (default 04:00) with `HOME`, a `PATH`
holding the memriver and executor directories (and node's, found on `init`'s
own `PATH`, when the executor is a node script as npm installs it; `init` warns
when it finds no node), and `MEMRIVER_ROOT` set to the store's absolute path;
its output goes to `<root>/dream/dream.log`, one line per run naming the run
and its status (read the report with `memriver dream
report`), so the log never keeps report text past the retention. It runs
only while you are logged in, and needs no sudo. The same bare environment
reaches the content classifier's own executor when one is configured
(see *Content classifier*, Scheduled dream runs) -- a `jev` or `codex` executor
needs the same `launchctl setenv` treatment as `[dream.codex_overrides]` below. The schedule needs a
persistent memriver: `uvx` runs memriver from uv's cache, which uv may delete
at any time, so `init` refuses there -- install it with `uv tool install
memriver` and run `memriver dream init` from that installation (run it again
after moving the installation). Until memriver is on PyPI, `uv tool install
memriver` finds the local wheels through `UV_FIND_LINKS` (see *Installing
before the PyPI release*). Elsewhere, `init` writes the settings and prints
the command line to add to your own scheduler. `init` is idempotent (running
it again replaces the schedule); it repairs an invalid key it owns rather than
refusing, and refuses only when the `[dream]` table holds an invalid key it
does not own, naming the key; a failed replacement puts the previous schedule
back, and says so if it cannot. `uninstall` removes the schedule and keeps the
settings and data, and says so, keeping the plist, when launchd would not let
go of it or could not say.

```toml
# ~/agent-memory/settings.toml -- [dream], every key shown with its default
# except executor and executor_path, which have none;
# init itself writes only executor, executor_path, ttl_days and schedule_at
[dream]
executor = "claude"                   # or "codex" (never "jev": dream needs generated text)
executor_path = "/absolute/path/to/claude"
ttl_days = 30                         # days unused before a memory is reviewed
ttl_read_multiplier_max = 3           # cap on 1 + reads as a TTL multiplier
uncertain_limit = 2                   # uncertain answers in a row that retire a memory
report_retention_days = 30            # how long runs and their report files are kept
schedule_at = "04:00"
max_sessions_per_run = 20             # sessions one run summarizes
max_groups_per_run = 20               # changes one run may make
max_candidates_per_run = 30           # TTL reviews one run may ask for
context_budget_tokens = 200000        # tokens one executor call may use, input and output
# claude_settings = "/absolute/path/to/auth-settings.json"   # passed to claude as --settings
# model = "your-model"                # passed to claude as --model, to codex as -c model=
# api_key_env = "TYPESAFE_API_KEY"   # validated as a variable name; unused (dream never runs jev)
```

**A Codex provider from `config.toml`.** Dream's Codex runs skip your
`config.toml`, so a model provider defined there (an Azure deployment, a
gateway) is given to dream separately, in a table you write yourself; `init`
keeps it and checks it:

```toml
[dream.codex_overrides]               # keys are quoted, dots included
"model_provider" = "azure-foundry"
"model" = "your-deployment"
"model_providers.azure-foundry.name" = "Azure AI Foundry"
"model_providers.azure-foundry.base_url" = "https://your-resource.example/openai/v1"
"model_providers.azure-foundry.env_key" = "AZURE_FOUNDRY_API_KEY"   # a variable's name
"model_providers.azure-foundry.wire_api" = "responses"
```

Only these keys (and `model_providers.<id>.requires_openai_auth`, a boolean)
are accepted, for the one provider `model_provider` names; anything else --
features, tools, MCP servers, hooks, whole tables, token or header fields, a
URL with credentials, a query or a fragment -- is refused, and the error names
only the field `dream.codex_overrides`, never the offending key or its value.
Never put a key itself in this table: `env_key` names the environment variable
that holds it. `init` and every run refuse when that variable is not set,
rather than falling back to Codex's default provider. The scheduled job does
not see your shell's environment and memriver never copies the variable into
the LaunchAgent: make it visible to your login session yourself (for example
`launchctl setenv NAME value` after each login), or scheduled runs refuse and
say so in `dream.log`.

Known limits: token counts are a rough estimate (no tokenizer), kept inside a
margin; one call's input may use `context_budget_tokens` less 20,000 tokens
kept for the answer and the margin -- lower it for a model with a smaller
context, and follow the direction *Needs you* names when it reports an input
too large (raise the setting when our own estimate rejected the input before
any call, lower it when the executor itself refused an input that fit our
estimate); a transcript file is read whole; the kind of an executor failure is
recognized from the wording of the harness's own error messages, and an
unrecognized one is reported as `exit`; a pass whose own changes moved
versions runs once more on the next run before it is skipped.

## Doctor

```bash
uvx memriver doctor                   # human-readable report
uvx memriver doctor --json            # stable machine-readable report
uvx memriver doctor --stale-days 30   # flag memories not updated in 30 days (default 90)
uvx memriver doctor --root /path      # check a non-default store
```

A store below schema version 4 is refused before any of the checks below run:
`doctor` prints the one line naming the store's schema version to stderr and
exits 1, with `--json` still emitting `{"error": ...}` on stdout -- no report
either way. On a store at schema version 4, `doctor` diagnoses problems in
more detail than a tool call reports: a schema version this build does not
recognize as current (`unknown-schema`; when the check cannot complete at
all -- a garbage file, a missing table -- doctor takes the inaccessible
branch below instead, exit 2), a failed SQLite integrity check (`integrity`),
`memriver.db` as a symlink or anything but a regular file
(`unsafe-database`), a memory whose project row no longer exists (`orphan`), a
session whose project or proposed project no longer exists (`session-orphan`),
a memory, project, or session row holding a value memriver could not have
written (`invalid-row`), a bound directory that is no longer canonical or
could not be checked, two projects
bound to the same directory under different spellings (`root-conflict`), the
pre-SQLite file layout (`legacy-layout`), invalid `updated` timestamps, stale
memories and near-duplicates. It also checks the history -- a memory row that
differs from its latest version, a gap in a memory's versions, a version
without its change, a source reference that does not resolve, a source cycle
-- and lists the changes a hard delete left incomplete (they can no longer be
undone) and every stored version, current, older or deleted, that fails the
content policy, by id, version and rule, never the text. It also lists every project -- id, name, its
directory (or `global`), its root's state (a directory that has gone missing
is only a state here, not a finding), and its active and deleted memory
counts. Reports never contain memory bodies, and no finding carries a path --
findings use store-relative location hints such as `projects/<id>` or
`memories/<id>`. The projects section is where an absolute directory appears:
every bound project's `root`, printed for a person and, with `--json`,
returned as a plain field. An inaccessible store exits with status 2 (with
`--json`, a `{"error": ...}` object is still emitted on stdout).

One more line states the content classifier: `classifier: not configured`,
`off (enabled = false)`, or the executor and its executable or model; with `--json`
the same text is the `classifier` field.

## Uninstall

```bash
uvx memriver uninstall                          # remove the harness configuration
uvx memriver uninstall --harness codex --dry-run
uvx memriver uninstall --purge-data             # ...and delete the memory store
uvx memriver uninstall --purge-data --root /path
uvx memriver uninstall --clean-uv-cache         # ...and drop uv's cached memriver environment
```

`uninstall` is the inverse of `install` and runs through the same plan /
confirm / backup / rollback pipeline. It removes only the entries install
manages — hook entries, MCP registrations, the marker block — byte for byte
around them; a container the removal empties is left as an empty container
rather than guessed at, Kiro's steering file (memriver's own) is deleted, and
shared files are never deleted. The harness's own memory setting is left as it
is; the completion report says so and names any file left empty. Once the
configuration is removed, `uninstall` also removes the dream schedule (see
*Dream: offline maintenance*) when every harness is uninstalled (or with
`--purge-data`), after its own confirmation (or with `--yes`), so a scheduled
dream does not keep sending memories to the model service after memriver
itself is gone; uninstalling a single harness leaves memriver, and the
schedule, running for the others.

`--purge-data` is the only thing that deletes the whole store rather than one
memory (`memriver delete` removes a single memory; see *Browsing and managing
memories directly*), and only with the explicit flag (`--yes` merely skips the
prompts). The resolved, canonical path is shown before deletion; the
filesystem root, your home, the current directory, anything that resolves onto
them through a symlink, and a directory that is neither empty nor a memriver
store (it holds no `memriver.db`) are refused.
`--clean-uv-cache` runs `uv cache clean` for memriver's packages afterwards and
is non-fatal if `uv` is missing or fails.

## Migrating existing Claude Code memory

`skills/migrate-claude-memory/SKILL.md` is an agent skill that moves an
existing Claude Code auto-memory store (`~/.claude/projects/<slug>/memory/`)
into memriver through the MCP tools: every file becomes one memory in the
current project, bodies and descriptions are copied verbatim (the store
strips their leading and trailing whitespace; memriver assigns new ids, so
`[[name]]` cross-references are not preserved), and the source directory is
never modified. Copy the directory to
`~/.claude/skills/` and ask Claude Code to migrate your memory. Install
memriver first — the skill writes through `memory_write`, so the tools have to
be present, and the directory you run it in has to be a registered project.

## Hook up a harness by hand

A hand registration with no `--harness` is directory mode, the same as a
plain `memriver serve`: the project is resolved once, from `--project-dir`,
when the server starts, and `source.harness` is `unknown`.

Claude Code: `claude mcp add memriver -- uv run --project /path/to/repo memriver serve --harness claude-code`

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.memriver]
command = "uv"
args = ["run", "--project", "/path/to/repo", "memriver", "serve", "--harness", "codex"]
```

Cursor (`~/.cursor/mcp.json`) / Kiro: the same `command`/`args` shape under
`mcpServers.memriver`, ending in `"serve", "--harness", "cursor"` or
`"kiro"` -- directory mode either way, since Cursor and Kiro have no session
hooks.

`--harness claude-code|codex` alone only makes the MCP server session-routed;
session registration also needs the hooks run by hand, one per event,
e.g. `uv run --project /path/to/repo memriver hook session-start --harness
claude-code` (and `user-prompt-submit`, `stop`, `session-end`; Claude Code
also `pre-tool-use`, in a `hooks.PreToolUse` group with `"matcher":
"mcp__memriver__.*"`), wired into `~/.claude/settings.json`'s
`hooks.SessionStart` etc. (or Codex's `~/.codex/hooks.json`) the way
`memriver install` does. Without them the
server still answers per session -- reading global memory is still allowed --
but no session is ever registered, so no session ever gets a project of its
own to write to, and `session_search` sees none of them; a session that
already has a row from before keeps being routed by it regardless.

`--project-dir` on the `memriver` command is where directory-mode project
discovery starts (the nearest directory bound to a project decides the id).
In directory mode, the MCP client's working directory determines project
attribution through it: `--project` runs memriver from this checkout while
keeping that directory, so memories land under the project you are actually
working in (use `--directory` and every session would resolve against
memriver's own checkout).

## Storage layout

```
~/agent-memory/
  memriver.db            # 0600; the only memory data file -- every project, memory and version, bodies included
  memriver.db-journal    # transient, SQLite's own; also left by a crashed writer until the next connection rolls it back
  settings.toml          # optional, see Settings
  dream/                 # 0700; memriver dream's own (see Dream)
    dream.db             # runs, TTL reviews, finished passes, source re-checks, summary progress
    reports/             # one report file per run, kept report_retention_days
    .lock                # one run at a time
    dream.log            # one line per scheduled run: its id and status
```

Every id is 10 random lowercase characters memriver generates (Crockford base32:
digits and letters without i, l, o, u). A memory belongs to exactly one project
through its `project_id`; global is the one project row flagged global (it
never has a directory -- an unbound project has none either; the flag is
what tells them apart), shown as `global` by `doctor` and `project explain`.
Global memories are written by `memriver dream` and managed with `memriver
history`, `restore`, `undo` and `delete`; MCP never writes them. Editing
`memriver.db` by hand bypasses the history and the change log, and `memriver
doctor` reports what that breaks. `dream.db` holds no memory body; the only
model text in it is the policy-checked partial summaries of a long session
still being summarized. The root directory memriver creates is private
to your user (`0700`);
`memriver.db` is `0600` and must be a real file (memriver never follows a link
there).
Override the root with `--root` or `MEMRIVER_ROOT`.

## Installing before the PyPI release

`memriver install` writes hook and MCP entries that invoke `uvx memriver`, and
each harness resolves that command itself every time it starts. Until memriver
is on PyPI, point `uvx` at locally built wheels:

```bash
uv build --all-packages                      # wheels land in ./dist
export UV_FIND_LINKS="/absolute/path/to/memriver/dist"   # your checkout's path
```

`UV_FIND_LINKS` has to be in the profile, not just the current shell: the
harness starts the hooks and the MCP server in its own environment, and a
variable exported once is gone by then. Spell the path out in full rather than
as `$PWD/dist` — a profile expands `$PWD` afresh in every shell, so the value
would follow whatever directory that shell happened to start in. A harness
launched from a GUI may not read an interactive shell profile at all; the same
absolute value then has to reach the environment that harness actually
inherits (its own env settings, a launch agent, or the desktop session).
After rebuilding the wheels, run
`uvx --refresh memriver --help` once so `uvx` picks up the new build instead of
its cached one. `install` prints a note when `uvx` itself is not on `PATH`.

## Settings

Settings are read from `--root` / `MEMRIVER_*` environment variables and an
optional `<root>/settings.toml`, in that order of precedence. All four file
settings are positive integers. A key or table memriver does not use is ignored.
An unreadable file, bad TOML or an invalid value (in the file or in a
`MEMRIVER_*` variable) stops every entry point that reads settings with one
stderr line naming the file (or variable) and the field, never the value --
for example `memriver: settings.toml is invalid: field search_limit_default`;
`memriver doctor` prints that same one line and exits 2 instead of a report.
Under Codex, the hook and MCP surfaces don't carry that line: a failed hook
shows only `hook: SessionStart Failed` (or `UserPromptSubmit Failed`), and a
failed MCP server start prints nothing by default. Run `memriver doctor` (or
any other memriver command that reads settings) to see the one-line reason.
Four entry points never read `settings.toml` at all, so none of them are
affected: the `Stop` and `PreToolUse` hooks, `memriver uninstall` and
`memriver dream uninstall`.
Everything else -- the server, the other three hooks, every `memriver project`
and browsing command, and `memriver install` -- reads settings and stops on
this error. This is new in this release: an invalid settings.toml used to be
silently ignored by the entry points that read it; now they stop until it is
fixed.

```toml
# ~/agent-memory/settings.toml
max_body_chars = 8000       # largest body memory_write accepts
search_limit_default = 5    # memory_search limit when the caller omits it
search_limit_max = 50       # ceiling applied to any caller-supplied limit
index_budget_lines = 100    # entries memory_index lists before truncating
```

One more top-level key, unset by default: `memory_reads_retention_days = N`
keeps read records for N days (unset keeps every one; a dropped read no longer
lengthens a memory's TTL). The `[dream]` table belongs to `memriver dream` (see
*Dream*) and is read by the `memriver dream` commands only: an invalid key there
stops `memriver dream init`, `run` and `report` with the same one-line error
(`field dream.<key>`), never the server or the other commands, and `memriver
dream init` repairs an invalid key it owns instead of refusing.

The `[classifier]` table (see *Content classifier*) is read by `memriver serve`,
`memriver dream run` and `memriver doctor`: an invalid key there stops them with the
same one-line error (`field classifier.<key>`).

`search_limit_default` may not exceed `search_limit_max`. The root itself is
set with `--root` or `MEMRIVER_ROOT`, not in this file: it is what locates the
file.

## Development

```bash
uv run pytest             # every package plus tools/
uv run ruff check .       # repo-wide lint gate
```

To run the suite on Linux from a macOS checkout, stream the tracked files
into a throwaway container — with macOS metadata stripped, or bsdtar adds
AppleDouble `._*.py` sidecars that the architecture tests then try to parse:

```bash
git ls-files -co --exclude-standard -z \
  | tar --null --no-xattrs --no-mac-metadata -T - -cf - \
  | docker run --rm -i ghcr.io/astral-sh/uv:python3.12-bookworm-slim \
      sh -c 'mkdir /work && cd /work && tar xf - && uv run --frozen pytest -q'
```

Tests that need non-root permission semantics skip when the container runs
as root.
