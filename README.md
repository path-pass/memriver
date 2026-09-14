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

## Tools

| Tool | Purpose |
|---|---|
| `memory_index()` | Compact index of every active memory (global + current project) |
| `memory_read(entry_id)` | One memory in full, by name |
| `memory_search(query, limit=None)` | Memories relevant to a task |
| `memory_write(content, type, name="", scope="project", sync=True, harness="unknown", description="")` | Save one durable fact; `type` is `user` / `feedback` / `project` / `reference`; `name` becomes the permanent id; `scope` is `project` or `global` |
| `memory_update(entry_id, content, description=None)` | Rewrite a memory in place (name and type stay) |
| `memory_delete(entry_id)` | Remove a memory that is no longer true or wanted |
| `memory_dream(limit=3)` | Maintenance queue: the entries least recently confirmed true — for dedicated memory-hygiene sessions only |

Every write passes the content policy (secret-shaped content is refused, the
value is never echoed back) and the size limits from *Configuration*. Tools
never raise through the transport: an operational failure comes back as a
path-free error message.

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
into memriver through the MCP tools: one file becomes one memory under its
original name, bodies and descriptions are copied verbatim, and the source
directory is never modified. Copy the directory to `~/.claude/skills/` and
ask Claude Code to migrate your memory. Install memriver first — the skill
writes through `memory_write`, so the tools have to be present.

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
runs memriver from this checkout while keeping that directory, so `scope="project"`
memories land under the project you are actually working in (use `--directory`
and every project would share memriver's own slug). Pass `--project-dir` to the
`memriver` command to pin a project explicitly.

## Storage layout

```
~/agent-memory/
  global/entries/<name>.md
  projects/<slug>/entries/<name>.md
  config.toml            # optional, see Configuration
```

`<name>` is the kebab-case name the agent proposed (or a server-generated
ULID when no usable name was given); `<slug>` derives from the project's git
root. Directories memriver creates are private to your user (`0700`).
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
