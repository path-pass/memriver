# memriver e2e verification harness

Automates the PENDING live-harness rows of
`docs/verification/phase2-hooks.md` inside a Docker container, driving the
literal `uvx memriver ...` commands the real installer writes -- against
locally-built wheels, since memriver is not published on PyPI.

Everything here is a verification tool, not a shipped artifact: the scripts
are versioned with the source, the built wheels (`e2e/wheels/`) are not, and no
credential is ever written under `e2e/`.

Status:

- Stage 1 needs no credential and runs on every pull request (the `e2e-smoke`
  job of the `pr-checks` workflow); it covers the session registry.
- Stages 2, 3 and 5 drive real Claude Code / Codex processes on Azure AI
  Foundry (per-token billing), so they never run on a pull request. Run them
  locally (key from the git-ignored `.env`) or through the manually started
  `e2e-foundry` workflow (key from the repository's Actions secrets of the
  same names). All three passed locally on 2026-09-25 against the session
  registry (Claude Code 2.1.282, codex-cli 0.157.0).

## The store under test

The store is one SQLite file, `<root>/memriver.db` (mode `0600`, `PRAGMA
user_version = 2`, foreign keys on). The two tables the stages seed and read:

- `projects(id, name, root, is_global)` -- exactly one row has `is_global = 1`
  (name `global`, `root` NULL); every other row is a registered project.
- `memories(id, project_id, type, source_harness, source_method, trust, sync,
  description, body, created, updated, version, deleted_at)` -- `version` starts
  at 1 and moves on every update and delete; a soft delete sets `deleted_at`
  and makes the row invisible to agents.

Beside them, `sessions` holds one row per (harness, session id) -- the
project the session was registered to at its first SessionStart, its prompt
counters and a few recent prompts -- and `tool_calls` maps a Claude Code tool
call id to its session (written by the PreToolUse hook, kept one hour).

There is no `store.toml`, `projects/`, `memories/` or `registry/` any more; the
install check asserts none of them exists.

None of the stages drives `memory_update` / `memory_delete` (the recall
questions only read), so the MCP tools' new `expected_version` argument needs no
harness change; any future stage that updates or deletes must pass the
`version` its `memory_read` returned.

## Shared setup (`common.sh`, every stage)

Each container starts from a fresh `HOME=/root`; the store is memriver's default
`/root/agent-memory` (no `MEMRIVER_ROOT`). `run_shared_setup` (Claude Code) /
`run_shared_setup_codex` (Codex, which seeds its own marker) do:

1. `uvx memriver --version` -- proves the local wheels resolve.
2. `uvx memriver install --harness <h> --yes` -- on a fresh store the plan
   carries the required store step (`memory store (required): create the global
   project in /root/agent-memory`); `--yes` initializes it before any harness
   file is written and prints `memory store: ready (global project <id>)`.
   Asserted: exit 0, **empty stderr** (install failures go to stderr), that
   line present, `<root>/memriver.db` exists with mode `0600` and
   `user_version = 2`, its one `is_global = 1` row is `(<that id>, "global",
   NULL)`, and no old file-store name exists.
3. The harness config entries. For Claude Code (stage 1): the MCP server is
   `uvx memriver serve --harness claude-code`, the five hooks
   (SessionStart, UserPromptSubmit, Stop, SessionEnd, and PreToolUse with the
   matcher `mcp__memriver__.*`) run `uvx memriver hook <event> --harness
   claude-code`, and `env.CLAUDE_CODE_DISABLE_AUTO_MEMORY = "1"`. For Codex
   (stage 5): `serve --harness codex` and the four hooks (no PreToolUse).
4. `uvx memriver project init /root/e2e-project --yes` -- creates the project
   and binds the directory in one transaction
   (`created project e2e-project [<id>] and bound /root/e2e-project`; id
   exported as `E2E_PROJECT_ID`). The Stop hook only nudges inside a registered
   project (`hooks._stop`), so every stage that exercises Stop runs there. It is
   a sub-directory of HOME because `project init` refuses HOME and its ancestors.
5. Seeds one **global** memory (the mascot fact) -- see below.

### How the global marker memories are seeded

Global is read-only to agents through the service (`GlobalReadOnly`), and
`MemoryService.record` only writes the session's own project, so a global
memory cannot be seeded through the service. `seed_global_memory "<text>"`
seeds it the way an operator would: one `INSERT` into `<root>/memriver.db`'s
`memories` table, over a connection with `PRAGMA foreign_keys = ON`. The id and
timestamps come from core's own model -- `Memory.new(body=text,
description=text, type="project", project_id=<global id>, trust="user",
source={"harness": "e2e", "method": "manual"})`, where the global id is the one
`projects` row with `is_global = 1` -- and the row is written column by column
(`source` split into `source_harness` / `source_method`, `sync` as 0/1,
`version = 1`, `deleted_at = NULL`). The text doubles as the description, i.e.
the index cue (60-character budget), so the injected index line reads `-
[project, global] <id>: <text> (<date>)`. Stage 1 runs `memriver doctor --json`
afterwards and requires `state: healthy` with no findings, which confirms the
hand-inserted row is valid store data.

## Stage 1 -- clean-machine install + exact hook command (no credentials)

What it proves: on a machine with nothing memriver-related installed,
`uvx memriver install --harness claude-code --yes` initializes the store's
global project and writes the documented `~/.claude.json` /
`~/.claude/settings.json` entries, and the *exact* command strings it wrote
actually run end to end:

Every hook payload carries a `session_id`; two sessions are driven, one
entering from the registered project and one from unregistered `/root`:

- SessionStart from the registered project: the first line inside the index
  delimiters is `project: e2e-project [<id>] (root /root/e2e-project)`,
  followed by the seeded global memory's `- [project, global] <id>: ...` line.
- SessionStart from `/root`: the no-project header (`project: none — this
  session was registered with no project, ...`), and the global memory is still
  injected.
- Five prompts through UserPromptSubmit in each session; then Stop in the
  registered session returns `decision: block` with the `[memriver] Before
  finishing ...` nudge, and stays silent on the continuation
  (`stop_hook_active: true`), on a second Stop with no new prompts, and in the
  session with no project.
- PreToolUse and SessionEnd exit 0 with empty stdout.
- The `sessions` table holds both sessions (the registered one bound to
  e2e-project with 5 prompts, nudged at 5 and ended; the other with no
  project), `tool_calls` maps the PreToolUse call to its session, and `uvx
  memriver sessions` lists the session with its `claude --resume` command.
- `memriver doctor --json` reports `healthy` with no findings.
- The human views read the store: `uvx memriver list` shows `<global id>
  global  (global)` followed by the seeded memory's line (`<id>  [project]
  <date>  <cue>`) and `<id>  e2e-project  (/root/e2e-project)`; `uvx memriver
  show <id>` prints the header (`project: <global id>`, `trust: user`, `source:
  e2e/manual`, `version: 1`, no `deleted:`) and the body after `---`; `uvx
  memriver export /root/snap` prints `exported 1 memories to /root/snap` and
  writes `/root/snap/<global id>/<id>.md` (mode `0600`, front matter with
  `version: 1`, the body after it) and `projects.md` (mode `0600`, one line per
  project) -- nothing else, since the empty e2e-project gets no folder.

```bash
bash e2e/run-stage1.sh
```

This rebuilds the `memriver`/`memriver-core` wheels into `e2e/wheels/` (`rm -rf
e2e/wheels && uv build --all-packages --out-dir e2e/wheels` -- every
`run-stage*.sh` does this first, so the container can never resolve a stale
wheel; the container's uv cache is fresh on every `--rm` run), builds the
`memriver-e2e` image, and runs `stage1.sh` in a throwaway `--rm` container
with the wheels mounted read-only at `/wheels`. Nonzero exit = a check failed.

Covers §5 and the CLI-observable half of §6's rows (config source, exact
command, SessionStart/Stop shape, stdout/exit status) without a live Claude
Code session.

## Credentials for stages 2, 3 and 5: Azure AI Foundry

Stages 2, 3 and 5 drive real Claude Code and Codex processes against Azure AI
Foundry deployments, billed per token on the Foundry resource (not a Claude or
ChatGPT subscription). The runners read the repository's git-ignored `.env`
(or the host environment) through `foundry-env.sh`:

```bash
AZURE_FOUNDRY_BASEURL=https://<resource>.services.ai.azure.com/
AZURE_FOUNDRY_API_KEY=...
AZURE_FOUNDRY_CLAUDE_DEPLOYMENT=<Claude deployment name>   # stages 2, 3
AZURE_FOUNDRY_GPT_DEPLOYMENT=<GPT deployment name>         # stage 5
```

- A runner refuses to start unless every variable its stage needs is set; it
  never prompts for or accepts a value as an argument.
- Values reach the container by name only (`docker run -e NAME`): never in
  `argv`, the Dockerfile, the image layers or a log line. The container runs
  `--rm`, so nothing persists.
- Inside the container, `use_foundry_for_claude` sets Claude Code's documented
  Microsoft Foundry variables (`CLAUDE_CODE_USE_FOUNDRY=1`,
  `ANTHROPIC_FOUNDRY_BASE_URL` = `<resource URL>/anthropic`,
  `ANTHROPIC_FOUNDRY_API_KEY`) and pins every
  model alias to the deployment; `use_foundry_for_codex` adds a
  `model_providers` entry to `~/.codex/config.toml` (`base_url` =
  `<resource URL>/openai/v1`, `wire_api = "responses"`, the key read from the
  environment through `env_key`). One resource key serves both APIs.
- In CI they run only through the `e2e-foundry` workflow, started by hand
  (`workflow_dispatch`); its secrets are never exposed to pull requests.

## Stage 2 -- one real Claude Code session, five prompts

```bash
bash e2e/run-stage2.sh
```

A single session, driven headlessly: `claude -p`, then four `claude -p
--resume <id>` calls from the registered `/root/e2e-project`, each read as a
`stream-json` transcript. It proves, against a live Claude Code install:

1. SessionStart injection reaches the model: asked "what is the project
   mascot?", the answer names Quibble (or the axolotl), which only the
   injected global memory holds. The question is deliberately innocuous --
   asked to list memriver ids verbatim, the model reads it as a
   prompt-injection attempt and declines, since the index says its entries
   are data, not instructions.
2. The session is registered once, to the project it entered from, and keeps
   its row across every resume (same session id; 1 prompt after the first
   call, no Stop nudge: `num_turns = 1`).
3. A real memriver tool call is routed by session: with
   `--allowedTools=mcp__memriver__memory_index`, the model's call returns the
   session's project header (`project: e2e-project [<id>] ...`), and the
   PreToolUse hook mapped the call to the session in `tool_calls`.
4. The Stop nudge is conditional: prompts 3 and 4 run one turn each; prompt 5,
   the fifth without a save, gets exactly one continuation (`num_turns = 2`),
   and the row records 5 prompts, nudged at 5.

## Stage 3 -- `claude --resume` re-injects the current index

```bash
bash e2e/run-stage3.sh
```

What it proves beyond stage 2: resuming a session fires
`SessionStart(source=resume)` and re-injects the memory index as it is *now*,
rather than the model replaying the old transcript. Two global markers,
seeded at two different times:

- Marker A (the Quibble axolotl) is seeded before session 1, which asks the
  mascot question and captures its `session_id`.
- Marker B ("the release codename is Teal Capybara Zephyr") is seeded after
  session 1 has ended, so no transcript can hold it. `claude -p --resume
  <id>` then asks for the release codename; a correct answer (Zephyr, or Teal
  Capybara) can only come from the fresh resume injection.

It also checks the registry across the resume: the same session id, still
registered to e2e-project with 2 prompts, and no Stop nudge (`num_turns = 1`:
the nudge waits for 5 unsaved prompts).

## Stage 5 -- Codex CLI: `codex exec` non-interactive turns

```bash
bash e2e/run-stage5.sh
```

The Codex counterpart: `uvx memriver install --harness codex --yes` writes
the documented `~/.codex/config.toml` (`serve --harness codex`) and
`~/.codex/hooks.json` (SessionStart, UserPromptSubmit, Stop, SessionEnd)
entries; the installed SessionStart command produces the right JSON offline;
then three `codex exec --ephemeral` runs from `/root/e2e-project`:

- **criterion 0 (probe)**: the recall question without
  `--dangerously-bypass-hook-trust`. The hooks were just written and have no
  persisted trust, so Codex skips them and the answer does not name the
  marker. If a future Codex auto-trusts user-level hooks, this prints a NOTE
  instead of failing.
- **criteria 1 and 2**: the same question with the bypass flag: the answer
  names Krakenfeld (injection works), and the session (Codex's thread id) is
  registered to e2e-project with 1 prompt and no Stop nudge.
- **criterion 3**: the model is asked to call `memory_index`; the answer
  quotes the session's project header. `codex exec` cannot answer an approval
  prompt, so that one read-only tool is pre-approved
  (`[mcp_servers.memriver.tools.memory_index] approval_mode = "approve"`).

Why the bypass flag: Codex records hook trust per hook, keyed by a content
hash, in `[hooks.state."<path>:<event>:<idx>:<idx>"]` tables of
`~/.codex/config.toml`, granted through the interactive `/hooks` TUI; the hash
is not a documented value worth hand-computing. Codex documents the flag for
exactly this case: "For one-off automation that already vets hook sources
outside Codex, pass `--dangerously-bypass-hook-trust` to run enabled hooks
without requiring persisted hook trust for that invocation"
(<https://developers.openai.com/codex/hooks#review-and-trust-hooks>). The
container's `~/.codex` is fresh and holds only the hooks memriver just wrote.
It is not a substitute for a real user trusting hooks through `/hooks`.

Why the image also writes `~/.config/uv/uv.toml`: Codex starts MCP servers
with a filtered environment that drops `UV_FIND_LINKS`, so `uvx memriver
serve` would look for memriver on PyPI and fail; a uv config file (the way a
real machine points uv at local wheels before the PyPI release) applies
whatever the environment.

## `/clear` and `/compact` -- headless feasibility (probe, no API cost)

**Verdict: not headlessly feasible with Claude Code 2.1.268.** Piping
`/clear` or `/compact` through `claude -p --input-format stream-json` (or any
other headless invocation) does not drive those commands. Use the manual TTY
fallback below instead.

Evidence this verdict is based on (no API tokens spent):

- `docker run --rm memriver-e2e claude --help` lists `--input-format
  stream-json` as realtime *streaming input* for `-p`, with no mention of
  slash-command dispatch from that stream.
- The official headless-mode docs
  (<https://code.claude.com/docs/en/headless>) state plainly: "Built-in
  commands that only run in the terminal interface, such as `/login`, aren't
  available in `-p` mode." They then name the specific exceptions that *are*
  available in `-p` mode -- `/model`, `/effort`, `/fast`, `/color`,
  `/rename` (as `command value`), and `/mcp`/`/config` -- and neither
  `/clear` nor `/compact` is on that list.
- The commands reference (<https://code.claude.com/docs/en/commands>)
  documents `/clear` and `/compact` with no "-p mode" availability note at
  all, unlike `/config`/`/model`, which explicitly call it out.
- The `claude-code` binary itself (`strings`-equivalent scan of
  `bin/claude.exe` inside the image) only surfaces `/compact` in
  interactive-UI-flavored strings (context-limit warnings, autocompact
  config) plus a *server-side* SDK compaction knob (`edits: [{type:
  "compact_20260112"}]` passed to `toolRunner()`) -- an Agent-SDK/API-level
  feature, not a CLI headless slash command.

Net: there is no stream-json input message shape that triggers `/clear` or
`/compact` in `-p` mode, so **no stage4.sh headless skeleton was written** --
per the task, only the interactive fallback is documented (below), precisely
enough for a human to paste.

### Manual fallback: `/clear` / `/compact` re-injection, interactive TTY

Same discrimination logic as stage 3's two-marker design, run by hand instead
of headlessly. Marker C is seeded *between* startup and `/compact` (or
`/clear`), so a correct post-compact answer can only come from a fresh
`SessionStart` injection, never from pre-compact transcript content.

```bash
# 1. build wheels + image once, same as the other stages
uv build --all-packages --out-dir e2e/wheels
docker build -t memriver-e2e e2e/

# 2. start an interactive container (needs a real TTY: -it, not --rm-only);
#    /e2e is mounted so common.sh's setup and seeding helpers are available
docker run -it --rm -v "$PWD/e2e/wheels:/wheels:ro" -v "$PWD/e2e:/e2e:ro" memriver-e2e bash

# --- inside the container ---
source /e2e/common.sh
step_install                # memriver install --harness claude-code --yes (creates global)
step_register_project       # memriver project init /root/e2e-project --yes
step_seed_memory            # marker A: global "project mascot is a purple axolotl named Quibble"
cd "$E2E_PROJECT_DIR"

claude --debug   # interactive session; --debug shows hook firings live

# 3. inside the interactive claude session, ask the mascot question first to
#    confirm startup injection worked (should name Quibble/axolotl)

# 4. in a SECOND terminal, exec into the same container and seed marker C
#    (a global memory row, same way as the stages) AFTER the session already
#    started -- so it postdates whatever context claude already has:
docker exec -it <container-id-or-name> bash -c \
  'source /e2e/common.sh && seed_global_memory "the launch signal phrase is Amber Falcon Ninety"'

# 5. back in the interactive claude session, run:
/compact
#    (or /clear, tested separately -- /clear starts a brand-new conversation
#    rather than summarizing, so it is the stronger of the two tests)

# 6. after compaction/clear finishes, ask:
"According to your memory, what is the launch signal phrase?"

# PASS: the answer names "Amber Falcon Ninety" (or "Amber Falcon") -- proof
# that /compact's or /clear's follow-up turn re-ran SessionStart and
# re-injected the (by-then-updated) index, since marker C postdates
# everything /compact could have summarized from the prior transcript.
```

Watch `claude --debug` (category `hooks`, e.g. `claude --debug hooks`) in the
first terminal to see `SessionStart` actually fire again around the
`/compact`/`/clear` boundary, rather than relying on the recall answer alone.

## What each stage proves vs. what stays manual

| | Stage 1 | Stage 2 | Stage 3 | Stage 5 | Stays manual |
| --- | --- | --- | --- | --- | --- |
| Wheel resolution (`uvx memriver` works with no PyPI package) | yes | yes | yes | yes | |
| `memriver install --harness <harness> --yes` initializes the global project (required store step) and writes the documented config | yes (claude-code) | yes | yes | yes (codex) | |
| A hand-inserted global memory row (`Memory.new`, `project_id` = the `is_global` project) is valid store data and is injected | yes (`doctor` healthy + injected line) | yes | yes | yes | |
| `memriver list` / `show` / `export` render the store for a person (`version: 1`, 0600 snapshot files) | yes | | | | |
| SessionStart header names the registered project / says none is registered | yes (both) | | | yes (registered) | |
| Exact installed SessionStart/Stop command strings run, correct shape | yes | yes | yes | yes | |
| A real harness process injects the seeded memory into model context | | yes (`claude -p` answers a recall question with the seeded fact) | yes (session 1, same as stage 2) | yes (`codex exec --dangerously-bypass-hook-trust` answers correctly) | |
| Stop nudges only inside a registered project (silent in an unregistered directory) | yes | | | | |
| A session is registered once, to the project it entered from; its row survives resumes | yes (hook level: two sessions, `sessions` rows) | yes (5 prompts, same id across 4 resumes) | yes (same id, 2 prompts) | yes (thread id, 1 prompt) | |
| A real memriver tool call is routed by session (the MCP server answers with the session's project) | | yes (`memory_index` header + PreToolUse `tool_calls` mapping) | | yes (`memory_index` header) | |
| Stop nudge is conditional in a real session (none before the fifth unsaved prompt, one continuation at it) | yes (hook level) | yes (`num_turns` 1, 1, 1, 1, then 2) | yes (none at 2 prompts) | yes (none at 1 prompt) | |
| `claude --resume` re-injects the *current* index (`SessionStart(source=resume)`) | | | yes -- two-marker design proves it's a fresh injection, not stale transcript context | | |
| Untrusted Codex hooks are skipped, not run | | | | yes -- criterion 0 probe (no bypass flag) confirmed against the documented behavior | |
| `/clear`, `/compact` re-injection | | | | | yes -- confirmed not headlessly driveable (see the feasibility probe above); needs an interactive TTY session, exact commands documented above |
| Codex `/hooks` review + trust as a real user would do it | | | | | yes -- stage 5 uses `--dangerously-bypass-hook-trust` for its own throwaway hooks only, which is not a substitute for a real interactive `/hooks` review; see §7/§8 of `docs/verification/phase2-hooks.md` |

After stage 2, stage 3, and stage 5, paste their output into §6 of
`docs/verification/phase2-hooks.md` (the rows they cover) and record the
remaining manual rows (`/clear`/`/compact`, a real interactive `/hooks`
review) separately, exactly as §8 of that document describes.
