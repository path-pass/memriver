# memriver

Shared memory layer for coding agents across harnesses (Claude Code / Codex /
Cursor / Kiro), exposed via MCP. Markdown is the single source of truth.
Local-only mode uses no LLM and no network.

Monorepo (uv workspace):

- `packages/memriver-core` — mutable, one-file-per-memory markdown entry store, write gate
- `packages/memriver` — CLI + MCP server (the package users install)
- `skills/` — agent skills that ship with the project (see *Migrating existing Claude Code memory*)
- planned: `memriver-vector` / `memriver-dream` / `memriver-sync`
  (installed on demand via extras, e.g. `memriver[vector]`)

## Install into your harnesses

```bash
uvx memriver install            # every supported harness, interactive
uvx memriver install --harness claude-code
uvx memriver install --dry-run  # show the plan, write nothing
uvx memriver install --yes      # accept every change shown
```

`install` plans every change first and shows it before writing anything. Each
file it touches gets a sibling backup (`<file>.memriver-backup-<stamp>`) taken
from the exact bytes it is about to replace; a failure part-way through rolls
every touched file back. Symlinked targets are refused. What it writes, per
harness:

| Harness | MCP server | Session hooks | Static instructions |
|---|---|---|---|
| Claude Code | `uvx memriver` | `SessionStart` + `Stop` | — |
| Codex | `uvx memriver` | `SessionStart` + `Stop` | — |
| Cursor | `uvx memriver` | — | marker block in the project's `AGENTS.md` |
| Kiro | `uvx memriver` | — | `.kiro/steering/memriver.md` |

Turning off the harness's own memory feature (Claude Code, Codex) is a
separately confirmed change, never implied by the rest. Codex only runs hooks
it has been told to trust: after installing, open `/hooks` in the Codex TUI
and trust the memriver entries, or they are skipped. `--dry-run` prints the
exact files and entries for your machine.

Until memriver is on PyPI, the `uvx memriver` commands above need a local
wheel source — see *Installing before the PyPI release*.

## What your agent sees

- **Session start** (Claude Code, Codex — including resume and after a
  compaction): the memory index is injected into the agent's context inside
  explicit delimiters, preceded by a notice that the entries are data, not
  instructions. The injection is sized in the harness's own metric (Claude
  Code: 10,000 UTF-16 code units; Codex: about 2,500 tokens) and truncates
  whole lines with an omission notice rather than cutting mid-entry.
- **Stop**: one reminder to save anything durable, issued at most once per
  turn so the agent is never trapped in a loop.
- **Cursor / Kiro** use the static instructions block instead of hooks.

Hooks never fail the harness: any error inside memriver degrades to "no
injection" for that event instead of blocking the session. If memories seem
to be missing, `memriver doctor` shows what the store actually holds.

## Projects

A project is a directory you registered. Nothing else confers project
identity -- not a `.git` directory, not a marker file -- and the `memriver
project` commands never write anything inside a project directory; the
registry lives in the store. (Installing Cursor or Kiro still writes their
static instruction file at the git root, as the install table shows.)

```bash
uvx memriver project init                 # register the current directory
uvx memriver project init ~/99_git/work   # register a parent folder holding several repos
uvx memriver project adopt <id> <dir>     # bind an existing project to another (or a moved) directory
uvx memriver project unbind <id> <dir>    # drop one binding; memories stay
uvx memriver project explain              # what the current directory resolves to
```

Every directory under a registered root -- including repositories added
later -- shares that project's memories; the nearest registered ancestor
wins, so registering a sub-directory carves it out as its own project. An
unregistered directory has no project scope: agents can read global memory
but have nowhere to save, and the session-start injection says so. Global
memory is read-only to agents; edit `~/agent-memory/global/entries/` by
hand. Changing a binding takes effect when the affected harness sessions and
their MCP servers restart.

Known limits:

- A directory that reuses a registered path inherits that project's memories.
- A deleted `project.toml` is indistinguishable from "never registered"; if
  the deleted one belonged to a sub-project, its directory falls back to the
  registered parent.
- A root as wide as a whole workspace is, in effect, a writable global.
- The session-start hook resolves the directory the harness reports while the
  MCP server resolves its own working directory, so the two can name different
  projects. Kiro multi-root workspaces start every MCP server in the first
  root: project-scoped memory is not supported there.
- After upgrading memriver, restart every harness session and MCP server that
  shares the store; a running server keeps its old rules.
- Global entries written before this version may hold project facts; agents
  cannot remove them, so review `global/entries/` by hand.
- Confirmation prompts protect against mistakes, not against an agent with a
  shell: the store is a directory your user can write.

## Tools

| Tool | Purpose |
|---|---|
| `memory_index()` | The session's project on the first line, then a compact index (global + project) |
| `memory_read(entry_id)` | One memory in full, by name |
| `memory_search(query, limit=None)` | Memories relevant to a task |
| `memory_write(content, type, name="", sync=True, harness="unknown", description="")` | Save one durable fact to the current project; global is read-only to agents; `type` is `user` / `feedback` / `project` / `reference`; `name` becomes the permanent id |
| `memory_update(entry_id, content, description=None)` | Rewrite a memory in place (name and type stay); refused for global entries |
| `memory_delete(entry_id)` | Remove a memory that is no longer true or wanted; refused for global entries |
| `memory_dream(limit=3)` | Maintenance queue: the current project's entries least recently confirmed true — for dedicated memory-hygiene sessions only |

Every write passes the content policy (secret-shaped content is refused, the
value is never echoed back) and the size limits from *Configuration*. An
operational failure inside a tool comes back as a path-free error message
rather than an exception through the transport; a call that does not match a
tool's schema — an unknown argument, a missing one, a wrong type — is rejected
by the MCP layer before the tool runs.

The storage model — frontmatter fields, the four types, naming rules, the
strict boolean and timestamp formats — is specified in
[`docs/memory-model.md`](docs/memory-model.md).

## Doctor

```bash
uvx memriver doctor                   # human-readable report
uvx memriver doctor --json            # stable machine-readable report
uvx memriver doctor --stale-days 30   # flag memories not updated in 30 days (default 90)
uvx memriver doctor --root /path      # check a non-default store
```

`doctor` reads the store and reports what the MCP tools would silently hide:
unreadable or unparsable entries, entries whose directory and frontmatter
disagree, ids that cannot be addressed, names shadowed across scopes,
invalid timestamps, stale memories. Reports never contain memory bodies or
absolute paths. An inaccessible store exits with status 2 (with `--json`, a
`{"error": ...}` object is still emitted on stdout).

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

`--purge-data` is the only thing that deletes memories, and only with the
explicit flag (`--yes` merely skips the prompts). The resolved, canonical path
is shown before deletion; the filesystem root, your home, the current
directory and anything that resolves onto them through a symlink are refused.
`--clean-uv-cache` runs `uv cache clean` for both packages afterwards and is
non-fatal if `uv` is missing or fails.

## Migrating existing Claude Code memory

`skills/migrate-claude-memory/SKILL.md` is an agent skill that moves an
existing Claude Code auto-memory store (`~/.claude/projects/<slug>/memory/`)
into memriver through the MCP tools: every file becomes a memory in the
current project under its original name, bodies and descriptions are copied
verbatim, and the source directory is never modified. Copy the directory to
`~/.claude/skills/` and ask Claude Code to migrate your memory. Install
memriver first — the skill writes through `memory_write`, so the tools have to
be present, and the directory you run it in has to be a registered project.

## Hook up a harness by hand

Claude Code: `claude mcp add memriver -- uv run --project /path/to/repo memriver`

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.memriver]
command = "uv"
args = ["run", "--project", "/path/to/repo", "memriver"]
```

Cursor (`~/.cursor/mcp.json`) / Kiro: same `command`/`args` shape under
`mcpServers.memriver`.

The MCP client's working directory determines project attribution: `--project`
runs memriver from this checkout while keeping that directory, so memories land
under the project you are actually working in (use `--directory` and every
session would resolve against memriver's own checkout). `--project-dir` on the
`memriver` command is where project discovery starts (the registry decides the
id); pass it to pin a directory.

## Storage layout

```
~/agent-memory/
  global/entries/<name>.md
  projects/<id>/entries/<name>.md
  projects/<id>/project.toml   # the project's registered roots
  config.toml            # optional, see Configuration
```

`<name>` is the kebab-case name the agent proposed (or a server-generated
ULID when no usable name was given); `<id>` is the project id `memriver
project init` generated. Directories memriver creates are private to your
user (`0700`).
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

## Configuration

Settings are read from `--root` / `MEMRIVER_*` environment variables and an
optional `<root>/config.toml`, in that order of precedence. All four file
settings are positive integers; an unknown key, an unparsable file or an invalid
value is warned about and the file is ignored, so a typo can never stop the
server from starting (a bad `MEMRIVER_*` variable does fail, with a readable
message):

```toml
# ~/agent-memory/config.toml
max_body_chars = 8000       # largest body memory_write accepts
search_limit_default = 5    # memory_search limit when the caller omits it
search_limit_max = 50       # ceiling applied to any caller-supplied limit
index_budget_lines = 100    # entries memory_index lists before truncating
```

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
