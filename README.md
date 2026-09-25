# memriver

Shared memory layer for coding agents across harnesses (Claude Code / Codex /
Cursor / Kiro), exposed via MCP. One SQLite database is the single source of
truth. Local-only mode uses no LLM and no network, until you set up the
optional `memriver dream` maintenance run, which sends policy-passing memory
and session text to a model (*Dream*).

Monorepo (uv workspace):

- `packages/memriver-core` — the SQLite memory and project store, write gate, maintenance facade
- `packages/memriver-dream` — the harness-neutral offline maintenance run behind `memriver dream`
- `packages/memriver` — CLI + MCP server (the package users install)
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
  first time since before you installed, or across a store upgrade) is not
  silently bound to whatever directory it happens to be resumed in. It is
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
  `memory_reads` row (the version read, the time, the server's `--harness`,
  and the calling session when the server is session-routed). Neither is
  returned to the agent; `memriver show`/`export` print `last_read_at`, and
  `memriver dream` counts the reads when it decides how long a memory may go
  unused (*Dream*).
- **Cursor / Kiro** use the static instructions block instead of hooks, and
  have no session routing: they resolve `--project-dir` once, when the
  server starts, and never re-resolve it for the life of that process.

Hooks never fail the harness: an unreadable store is stated inline, in the
injected header itself, as a labelled "unavailable" session -- exit 0, empty
stderr, nothing blocked. Of the five, only `SessionStart` ever writes to
stderr: when it hits something it cannot route around at all (a malformed
payload, some other unhandled failure) it skips the injection entirely and
prints one fixed, path-free stderr line instead. `UserPromptSubmit`, `Stop`,
`SessionEnd` and `PreToolUse` degrade the same failures silently -- empty
stdout, empty stderr, exit 0 -- since none of them owes the agent a header. If memories
seem to be missing, `memriver doctor` shows what the store actually holds.

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
memory is read-only to agents; `memriver dream` writes it (merges, rewrites,
extracts -- see *Dream*), a human deletes an entry from it by id with
`memriver delete`, and hand-editing `memriver.db` remains the fallback (see
*Storage layout*). A binding change reaches Cursor/Kiro at their MCP server's next
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
  server keeps its old rules. This matters more with a schema upgrade: a
  memriver of the previous release refuses the upgraded store outright as an
  unrecognized schema, rather than merely behaving as before. A session that
  was already running when you upgraded is not lost -- it is asked once, the
  next time it resumes, whether to register to the project its directory
  suggests (see *What your agent sees*).
  After an upgrade that changes an MCP tool's parameters, start new harness
  sessions rather than resuming old ones: a resumed Claude Code session keeps
  the schemas of the deferred tools it loaded before -- neither `/mcp`
  reconnect nor a new tool search refreshes them -- so its calls to a changed
  tool fail validation until a new session is opened.
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
| `memory_search(query, limit=None)` | Memories relevant to a task: the project's hits first, then global's; `limit` caps the whole answer, so a query with many project hits can leave no room for global ones |
| `memory_write(content, type, sync=True, description="")` | Save one durable fact to the current project; memriver assigns the id and stamps `source.harness` with the server's own `--harness` value (an installed Cursor/Kiro server records `cursor`/`kiro`; `unknown` only when the server was started with no `--harness` at all); global is read-only to agents; `type` is `user` / `feedback` / `project` / `reference` |
| `memory_update(memory_id, expected_version, content, description=None)` | Rewrite a memory's content in place (id, project and type stay); returns `{id, updated, version}`; refused for global memories or a stale `expected_version` |
| `memory_delete(memory_id, expected_version)` | Remove a memory that is no longer true or wanted; returns `{deleted: memory_id}`; refused for global memories or a stale `expected_version` |
| `session_search(query="", limit=None)` | Claude Code/Codex only: find this project's recorded sessions (newest activity first) by a word in their prompts, summary, branch or entry directory; each result carries a `resume_command` to show the user -- whether to run it is the user's decision |
| `session_confirm()` | Claude Code/Codex only: register the calling session to the project memriver proposed for it; call only after the user agrees; returns the session's new project header |
| `session_register()` | Claude Code/Codex only: register the calling session, when it has no project, to the registered project covering the directory it started in (the one stored when it registered); called when the user asks, or right after the agent ran `memriver project init` at the user's request; never changes a session that already has a project; returns the session's project header, with a note when no project covers that directory |

`expected_version` is the value `memory_read` last returned; if the memory
changed since, the call is refused and nothing is written -- read it again and
redo the edit on the current text. Every write passes the content policy
(secret-shaped content is refused, the value is never echoed back) and the
size limits from *Settings*. An operational failure inside a tool comes back
as an MCP tool error (`isError: true`) carrying the same path-free message,
never as a raw exception through the transport; a call that does not match a
tool's schema — an unknown argument, a missing one, a wrong type — is
rejected by the MCP layer before the tool runs.

The memory model — its fields, the four types, id rules, the strict boolean
and timestamp formats — is specified in
[`docs/memory-model.md`](docs/memory-model.md).

## Browsing and deleting memories directly

```bash
uvx memriver list [--project ID]                      # every project, or one; id, type, date, cue
uvx memriver show ID [--deleted]                       # full memory: header fields + body
uvx memriver search QUERY [--project ID] [--limit N]
uvx memriver export DIR                                # DIR must not exist; a markdown snapshot, never read back
uvx memriver delete ID --version N [--hard] [--yes]
```

These are read-only views for a person, not the MCP surface agents use:
`list`/`search`/`export` see every project, including global, but never a
soft-deleted memory; `show --deleted` is the one view that can, and it prints
the memory's `deleted_at`. `show` and `export` also expose `last_read_at`,
set by a successful `memory_read`: `show` prints it as `never` and `export`
writes `null` for a memory that was never read since it was created -- except
one that already existed when its store upgraded to schema v3, which had
`last_read_at` backfilled once to the upgrade time instead (not a recorded
read, and no `memory_reads` row for it), so it doesn't look overdue for
`memriver dream`'s TTL review the moment the upgrade lands; neither field is
part of what an agent can read.
`delete` needs the `version` that `memriver show` printed. A global memory is
deleted by id, from anywhere -- a management delete of the human CLI; MCP
still never writes global. Any other memory is deleted only from its own
project's directory: `delete` resolves the command's current directory the
way directory mode does, which is not always the project a session-routed
agent would use (a session registered in A but resumed from B can delete only
in A; running `delete` from B at that same moment acts on B). It soft-deletes
by default (the row's `deleted_at` is set); `--hard` removes the row itself,
including one already soft-deleted. A hard delete of a memory that a derived
entry (a merge or a global extract made by `memriver dream`) cites as a
source is refused, and memriver lists those entries: hard-delete them first,
with the same command (`memriver delete ID --version N --hard`) -- a soft
delete of a derived entry keeps its reference.

## Sessions

```bash
uvx memriver sessions [QUERY] [--project ID] [--limit N] [--json]
```

Lists every recorded Claude Code/Codex session (or one project's), newest
activity first: harness, project, branch, when it was first recorded and last
active, its last `SessionEnd`, its first and latest prompt, and a resume
command (`claude --resume <id>` / `codex resume <id>`), and the session's
summary once `memriver dream` has written one. `QUERY` matches a word
in a session's saved prompt text (the same first-plus-five, 512-characters-each
scope `session_search` has -- see its Known limits above), its summary, branch
or entry directory, the same way `session_search` does for the calling session's own
project; unlike `session_search`, this sees every project. `--json` emits
the same item shape `session_search`
returns.

## Dream: offline maintenance

```bash
uv tool install memriver   # dream's schedule needs a persistent memriver, not uvx's cache
memriver dream init [--executor claude|codex] [--ttl-days N] [--at HH:MM] [--yes]
memriver dream run [--phase summarize|consolidate|retire]
memriver dream report [RUN_ID] [--list [N]]
memriver dream undo CHANGE_ID [--yes]
memriver dream uninstall
```

`memriver dream` is an offline batch run, started every day by a schedule or
by hand with `dream run`. One run at a time: a run that finds another one
still going records itself as skipped and exits. Each run, in order:

1. **Re-scans every active memory, global included, for secrets** with the
   current secret rules and soft-deletes each match as a `secret` change
   whose reason names only the rule. No model is involved, so this runs even
   with no executor configured: a plain `memriver dream run` is a secret
   sweep. A soft-deleted secret still sits in `memriver.db` (mode 0600) and
   in the change's before-image, which `undo` needs; nothing erases it yet.
2. **Summarizes sessions**: each Claude Code/Codex session idle for
   `idle_minutes` gets a summary of its transcript -- goal, what was done,
   results, open items, identifiers kept verbatim -- that `session_search`
   and `memriver sessions` search and show. A long session is summarized
   part by part and merged; nothing is stored unless every part was covered,
   and a session too long for one run keeps its progress and is finished by
   the next runs. A session whose transcript was missing, or ended in an
   unfinished line, is tried again on later runs; the ones tried least
   recently go first.
3. **Consolidates each project, then global**: merges memories that state
   the same fact (the originals stay), rewrites one that others contradict,
   extracts what holds beyond the project into global (recording which
   memories, at which version, it came from), and soft-deletes entries that
   are instructions addressed to an agent rather than facts or preferences
   (`unsafe`). A rewrite names the memories that show the change, and an
   entry never loses the sources it was built from. A project is planned
   again after its memories change, and also after a pass in which any
   change could not be applied.
4. **Retires memories unused past their TTL**, after asking the model
   whether there is reason enough to retire each one (not whether it was
   used): `keep` leaves it alone until the TTL passes again, `delete`
   soft-deletes it, and `uncertain_limit` uncertain answers in a row about
   the same content soft-delete it too (an edit starts the count over). A
   memory's last use is the latest of its creation,
   its last update and its last `memory_read`; its TTL is `ttl_days` times
   one plus its recorded reads, capped at `ttl_read_multiplier_max` times.
   A read that lands while the model decides keeps the memory.

Every change to a memory (a secret quarantine, a merge/rewrite/extract, or a
retirement -- phases 1, 3 and 4) takes effect at once, without a review step,
and each one is recorded as one change group; a session summary (phase 2) is
not. `memriver dream report` shows what the latest
run (or `RUN_ID`) found and changed, phase by phase -- secrets by id, project,
cue and rule; each change group with its memories, the model's reason and
its `memriver dream undo <change_id>` command; the TTL decisions; the
summary outcomes as counts -- and `--list` shows the last runs. A run that
was interrupted still shows every change group it committed, with its undo
command. It never
prints a memory body, a summary or a secret; a cue whose own text looks like
a secret is shown as `(cue withheld)`. `memriver dream undo` restores every
memory of a group while none of them changed since; otherwise it names the
ones that did and changes nothing. There is no dry run: `undo` reverses one
change group at a time, not a whole run, and it never touches a session
summary.

**Executors.** `init` picks Claude Code or Codex (`--executor`; default the
configured one, else whichever of `claude` and `codex` is on `PATH`) and
stores its absolute path. Each model call is one headless run --
`claude -p` with memriver's own system prompt, no tools, no MCP servers and
none of your settings files, or `codex exec` ephemeral, in a read-only
sandbox, ignoring your Codex config, with hooks and the built-in tools
switched off, but reading your global `AGENTS.md` -- in an empty
temporary directory, with your existing login, and with `MEMRIVER_ROOT`
pointed at a path that does not exist so memriver's own hooks inside it do
nothing. The prompt goes to the harness on its standard input. A failed call
(timeout, not logged in, over quota, input too large, unreadable answer, a
harness that cannot be started) is retried by the next run; dream never
switches executor. The executor's provider receives memory text and session
transcripts; records and memories that match a secret rule are left out (a
transcript record becomes `[omitted]`).

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
holding the memriver and executor directories, and `MEMRIVER_ROOT` set to
the store's absolute path; its output goes to `<root>/dream/dream.log`. It
runs only while you are logged in, and needs no sudo. The schedule needs a
persistent memriver: `uvx` runs memriver from uv's cache, which uv may delete
at any time, so `init` refuses there -- install it with `uv tool install
memriver` and run `memriver dream init` from that installation (run it again
after moving the installation). Elsewhere, `init` writes the settings and
prints the command line to add to your own scheduler. `init` is idempotent
(running it again replaces the schedule), and it refuses when the `[dream]`
table already holds a key it cannot use, naming the key; a failed
replacement puts the previous schedule back, and says so if it cannot;
`uninstall` removes the schedule and keeps the settings and data, and says
so, keeping the plist, when launchd would not let go of it or could not say.

```toml
# ~/agent-memory/settings.toml -- [dream], every key shown with its default;
# init itself writes only executor, executor_path, ttl_days and schedule_at
[dream]
executor = "claude"                   # or "codex"
executor_path = "/absolute/path/to/claude"
ttl_days = 90                         # days unused before a memory is reviewed
ttl_read_multiplier_max = 5           # cap on 1 + reads as a TTL multiplier
uncertain_limit = 2                   # uncertain answers in a row that retire a memory
idle_minutes = 60                     # quiet time before a session is summarized
schedule_at = "04:00"
max_sessions_per_run = 20
max_groups_per_run = 20
max_candidates_per_run = 30
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
features, tools, MCP servers, hooks, whole tables, token or header fields,
a URL with credentials, a query or a fragment -- is refused by name, and the
value is never printed. Never put a key itself in this table: `env_key` names
the environment variable that holds it. `init` and every run refuse when that
variable is not set, rather than falling back to Codex's default provider.
The scheduled job does not see your shell's environment and memriver never
copies the variable into the LaunchAgent: make it visible to your login
session yourself (for example `launchctl setenv NAME value` after each
login), or scheduled runs refuse and say so in `dream.log`.

Known limits: token counts are a rough estimate (no tokenizer), kept inside a
margin; when a harness still reports its input too large, a session is split
finer and a TTL review compares against fewer memories, while a project
whose memories do not fit one call is reported as `too-large` and tried
again by every run (no memory is cut to make it fit; a larger budget or
another executor then takes it); a transcript file is read whole; the kind
of an executor failure is recognized from the wording of the harness's own
error messages, and an unrecognized one is reported as `exit`.

## Doctor

```bash
uvx memriver doctor                   # human-readable report
uvx memriver doctor --json            # stable machine-readable report
uvx memriver doctor --stale-days 30   # flag memories not updated in 30 days (default 90)
uvx memriver doctor --root /path      # check a non-default store
```

`doctor` diagnoses store problems in more detail than a tool call reports: an
unrecognized schema version (`unknown-schema`; when the check cannot complete
at all -- a garbage file, a missing table -- doctor takes the inaccessible
branch below instead, exit 2), a failed SQLite integrity check (`integrity`),
`memriver.db` as a symlink or anything but a regular file
(`unsafe-database`), a memory whose project row no longer exists (`orphan`), a
session whose project or proposed project no longer exists (`session-orphan`),
a memory, project, session, provenance (`memory_sources`), read (`memory_reads`) or dream
(`dream_*`) row holding a value memriver could not have
written (`invalid-row`), a bound directory that is no longer canonical or
could not be checked, two projects
bound to the same directory under different spellings (`root-conflict`), the
pre-SQLite file layout (`legacy-layout`), invalid `updated` timestamps, stale
memories and near-duplicates. It also lists every project -- id, name, its
directory (or `global`), its root's state (a directory that has gone missing
is only a state here, not a finding), and its active and deleted memory
counts. Reports never contain memory bodies, and no finding carries a path --
findings use store-relative location hints such as `projects/<id>` or
`memories/<id>`. The projects section is where an absolute directory appears:
every bound project's `root`, printed for a person and, with `--json`,
returned as a plain field. An inaccessible store exits with status 2 (with
`--json`, a `{"error": ...}` object is still emitted on stdout).

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
is; the completion report says so and names any file left empty.

`--purge-data` is the only thing that deletes the whole store rather than one
memory (`memriver delete` removes a single memory; see *Browsing and deleting
memories directly*), and only with the explicit flag (`--yes` merely skips the
prompts). The resolved, canonical path is shown before deletion; the
filesystem root, your home, the current directory and anything that resolves
onto them through a symlink are refused.
`--clean-uv-cache` runs `uv cache clean` for both packages afterwards and is
non-fatal if `uv` is missing or fails.

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
  memriver.db          # 0600; the only data file -- every project and memory, bodies included
  memriver.db-journal  # transient, SQLite's own; also left by a crashed writer until the next connection rolls it back
  settings.toml        # optional, see Settings
  dream/               # 0700; created by memriver dream: .lock (one run at a time) and dream.log (the scheduled runs' output)
```

Every id is 10 random lowercase characters memriver generates (Crockford base32:
digits and letters without i, l, o, u). A memory belongs to exactly one project
through its `project_id`; global is the one project row flagged global (it
never has a directory -- an unbound project has none either; the flag is
what tells them apart), shown as `global` by `doctor` and `project explain`.
Global memories are written by `memriver dream` (merges, rewrites, extracts) and deleted by
id with `memriver delete`; MCP never writes them. To edit one by hand, edit `memriver.db`'s
`memories` table where `project_id` is that project's id. The root directory memriver creates is private
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
`MEMRIVER_*` variable) stops the server, the hooks and every command with one
stderr line naming the file (or variable) and the field, never the value -- for
example `memriver: settings.toml is invalid: field search_limit_default`:

```toml
# ~/agent-memory/settings.toml
max_body_chars = 8000       # largest body memory_write accepts
search_limit_default = 5    # memory_search limit when the caller omits it
search_limit_max = 50       # ceiling applied to any caller-supplied limit
index_budget_lines = 100    # entries memory_index lists before truncating
```

One more top-level key, unset by default: `memory_reads_retention_days = N`
drops `memory_reads` rows older than N days whenever a new one is written
(unset keeps every row; pruned reads no longer lengthen a memory's TTL).
The `[dream]` table is `memriver dream init`'s (see *Dream*); an invalid
`[dream]` table is ignored on its own, with a warning, and never resets the
other settings.

`search_limit_default` may not exceed `search_limit_max`. The root itself is
set with `--root` or `MEMRIVER_ROOT`, not in this file: it is what locates the
file.

## Development

```bash
uv run pytest             # both packages plus tools/
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
