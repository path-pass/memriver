# memriver

Shared memory layer for coding agents across harnesses (Claude Code / Codex /
Cursor / Kiro), exposed via MCP. Markdown is the single source of truth;
the SQLite FTS5 index is a rebuildable derivative. Local-only mode uses no
LLM and no network.

Monorepo (uv workspace):

- `packages/memriver-core` — immutable markdown entry store, write gate,
  rebuildable FTS5 index
- `packages/memriver` — CLI + MCP server (the package users install)
- planned: `memriver-vector` / `memriver-dream` / `memriver-sync`
  (installed on demand via extras, e.g. `memriver[vector]`)

## Quick start (local dev)

```bash
uv run memriver          # stdio MCP server, storage at ~/agent-memory
```

## Hook up a harness (manual, until `memriver install` ships)

Claude Code: `claude mcp add memriver -- uv run --directory /path/to/repo memriver`

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.memriver]
command = "uv"
args = ["run", "--directory", "/path/to/repo", "memriver"]
```

Cursor (`~/.cursor/mcp.json`) / Kiro: same `command`/`args` shape under
`mcpServers.memriver`.

Storage layout: `~/agent-memory/{global,projects/<slug>}/entries/<ulid>.md`
(override root with `MEMRIVER_ROOT`).
