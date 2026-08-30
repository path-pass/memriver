# Memory Model

## What memriver is — and is not

memriver does **not** invent a new auto-memory mechanism. File-based agent
memory — one markdown file per memory, a compact index injected at session
start, the agent reading individual files on demand — already works well
inside a single harness. Claude Code's auto memory is the reference
implementation of that model, and memriver adopts it as-is.

What no harness solves today is what happens **outside** its own walls:

1. **Cross-harness sharing.** Each tool keeps its own silo (Claude Code auto
   memory, Codex/Cursor rule files, Kiro steering). The same user working in
   two tools has two disjoint, drifting memories.
2. **Team sharing (later).** There is no path from "what I learned" to "what
   my team knows" that is reviewable and safe.

memriver's job is to take the proven single-harness model, put it behind MCP
so every harness reads and writes the *same* store, and add the discipline a
shared store needs: server-side naming, a secrets gate, and a sync boundary.
Everything else stays deliberately boring.

## The entry

One memory = one mutable markdown file with YAML frontmatter:

```markdown
---
id: mise-runtime-management
type: user
scope: global
sync: true
created: 2026-08-29T10:00:00Z
updated: 2026-08-29T10:00:00Z
source: {harness: claude-code, method: agent}
trust: user
description: mise manages every runtime; check before suggesting installs
---

All language runtimes on this machine are managed by mise, not nvm/pyenv.
```

- **type** — `user` | `feedback` | `project` | `reference`, Claude Code's
  taxonomy verbatim: who the user is; guidance on how to work; ongoing work
  and constraints; pointers to external resources. Adopted unchanged so that
  agents already trained on this taxonomy need no re-learning.
- **description** — a one-line recall cue, written for the reader deciding
  whether to open the entry; rendered in the index.
- **scope** — `global` (follows the user everywhere) or a project slug.
- **sync** — per-entry privacy boundary: `false` means this entry never
  leaves the machine, regardless of mode.
- **trust** — provenance of the *source material*: `user` (stated
  explicitly), `agent` (judged worth keeping while working), or
  `untrusted-derived` (distilled from external content — web pages,
  third-party code, tool output). Trust gates future promotion into shared
  storage.
- Freshness is judged by `updated`, not by type.

## Naming

The agent proposes a short kebab-case name; the server disposes:

- The server sanitizes the proposal against a strict slug whitelist. Once
  accepted, **the name is the permanent id**: the filename, the update
  handle, and the future sync key. It is never renamed, even if the content
  drifts (delete and rewrite if the name becomes truly wrong). A global name
  is unique across the entire store; a project name is unique within its
  project and may not be claimed by a later global write. Different projects
  may reuse the same name.
- **Name collisions are refused, not resolved.** A write against an existing
  name returns the existing entry's summary instead of writing. The agent —
  which has the semantic context — then decides: same fact → update the
  existing entry; different fact → propose a more precise name. The server
  never silently forks a topic with a `-2` suffix. This refusal doubles as
  the cheapest possible duplicate-topic detector.
- A missing or unsalvageable proposal falls back to a server-generated ULID,
  keeping the write tool's never-raise contract.

Layout, sanitization rules, collision policy, and sync semantics all live in
the server. The agent only ever contributes a human-readable hint.

## Recall

Recall follows the index-and-read pattern, unchanged from single-harness
practice:

- `memory_index` renders one line per live entry (name + description,
  falling back to the body's first line for entries without one) under a
  line budget, with an explicit truncation notice. The harness injects it at
  session start; the LLM does the semantic matching.
- `memory_read` fetches one entry by name.
- `memory_search` exists as a tool contract, but the local engine is a plain
  in-memory scan over the files. At local scale (hundreds of entries) an LLM
  scanning the index outperforms any keyword engine, so the local layer
  ships no search infrastructure. When hybrid mode adds semantic retrieval,
  the engine upgrades behind the same contract — agents never notice.

## Updates, deletion, and history

- Update = rewrite the same file in place (atomic replace under a file
  lock), bump `updated`.
- Delete = delete the file.
- The local store keeps **no version history**. History and conflict-free
  replication are the sync layer's job, where object-store native versioning
  provides them without any local machinery. The file count therefore equals
  the number of live memories — naturally bounded, no compaction mechanism
  required.

## Maintenance

Everything above is use-time: an entry is checked, if at all, only when
retrieval happens to surface it. That leaves a blind spot — a memory nobody
ever searches for again just sits there, correct or not, indefinitely
unexamined.

`memory_dream` closes it with amortized full-coverage review instead of
retrieval-triggered spot checks: each call hands back the batch of entries
whose `updated` is oldest, for a dedicated maintenance session (started by
the user, or by a user-scheduled headless run) to check against reality.
It is a tool for that session alone — a working task session must never
call it, since a maintenance sweep has nothing to do with the task at hand
and would just crowd its context.

This works because `updated` doubles as "last confirmed true", not merely
"last edited". An entry the reviewer finds still correct is confirmed by
calling `memory_update` with its own unchanged body — same content, but the
timestamp bump rotates it to the back of the review queue; a changed fact
gets `memory_update` with the corrected body, and a dead one gets
`memory_delete`. Read this way, an entry's `updated` date is less "last
touched" and more "current as of" — the date someone last stood behind it.

## The write gate

Every write passes a deterministic, LLM-free gate before touching disk:
size limits, then a vendored secrets ruleset (gitleaks rules plus a small
floor of provider rules with known upstream gaps). Rejections name the rule,
never echo the secret. The gate is a pure function of the content, so
local-only mode needs no network and no model.

## How harnesses learn the protocol

External harnesses know nothing about this taxonomy up front, and never need
to. The protocol reaches their agents through three layers:

1. **MCP tool schemas (zero-install, universal).** `memory_write` declares
   `type` as a schema enum, and the tool descriptions carry one-line
   semantics for each type plus the naming rules. Every MCP client feeds
   tool descriptions to its model, so any harness that can connect the
   server has already been taught — this is why MCP is the integration
   surface in the first place.
2. **Refusal as correction (backstop).** An unknown type or a name collision
   comes back as a structured refusal that names the valid values. The agent
   self-corrects within the same turn. The server never guesses; it refuses
   and teaches.
3. **Installed protocol instructions (richer guidance, planned).** Tool
   descriptions cannot carry behavioral guidance — when to write a memory,
   what not to store, check the index before writing. A planned `memriver
   install` will inject a single server-maintained protocol block into each
   harness's native instruction file (Codex `AGENTS.md`, Claude Code
   `CLAUDE.md`, Cursor rules, Kiro steering). The injection points differ
   per harness; the text would be one source, maintained in the server.

Layers 1–2 alone make an unconfigured harness work correctly; layer 3
upgrades "works" to "works well". The taxonomy's four words fitting in a
tool description is itself part of why it was adopted.

## Modes and sync (forward-looking)

- **Local-only** — everything above; pure markdown, no LLM, no network.
- **Hybrid** — entries with `sync: true` replicate to user-owned object
  storage; versioning and multi-device semantics live there.
- **Team** — shared knowledge is produced by a distillation pipeline with
  human review, never by raw entry replication; `trust` and `sync` are the
  gates on that path.

Only local-only exists today; the fields above are the extent of the
provisioning for the later modes.

## Non-goals

- A new memory taxonomy, file format, or recall strategy.
- Local search infrastructure (databases, tokenizers, embeddings).
- Local version history, immutable entry chains, or supersede protocols.
- Agent-controlled naming or layout.
