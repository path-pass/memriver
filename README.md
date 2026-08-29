# memriver

Shared memory layer for coding agents across harnesses (Claude Code / Codex /
Cursor / Kiro), exposed via MCP. Markdown is the single source of truth.
Local-only mode uses no LLM and no network.

Monorepo (uv workspace):

- `packages/memriver-core` — mutable, one-file-per-memory markdown entry store, write gate
- `packages/memriver` — CLI + MCP server (the package users install)
- planned: `memriver-vector` / `memriver-dream` / `memriver-sync`
  (installed on demand via extras, e.g. `memriver[vector]`)

## Quick start (local dev)

```bash
uv run memriver          # stdio MCP server, storage at ~/agent-memory
```

## Hook up a harness (manual, until `memriver install` ships)

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

Storage layout: `~/agent-memory/{global,projects/<slug>}/entries/<name>.md`
(a kebab-case name proposed by the agent, or a server-generated ULID when no
usable name is given; override root with `MEMRIVER_ROOT`).

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

The root itself is set with `--root` or `MEMRIVER_ROOT`, not in this file: it is
what locates the file.
