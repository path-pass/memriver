#!/usr/bin/env bash
# Shared setup steps for stage1/2/3/5.sh. Sourced, never executed directly: it
# defines functions and expects the caller to already have `set -euo pipefail`
# active.
#
# Covers: wheel resolution proof, the real `memriver install` (which now also
# initializes the store's global project as a required step), asserting what it
# wrote, registering one project directory with `memriver project init` (the Stop
# nudge only fires inside a registered project), and seeding one GLOBAL memory so
# SessionStart has something to inject.

pass() { printf 'PASS: %s\n' "$1"; }

# The store root inside the container: no MEMRIVER_ROOT is set, so memriver's
# default (<home>/agent-memory, memriver_core.settings.storage_root) applies.
E2E_STORE="$HOME/agent-memory"
# A registered project directory. It must be *under* HOME, not HOME itself:
# `memriver project init` refuses the home directory and its ancestors.
E2E_PROJECT_DIR="$HOME/e2e-project"

step_verify_uvx_resolution() {
    local version
    version="$(uvx memriver --version)"
    [ -n "$version" ]
    pass "uvx memriver --version resolved from local wheels (UV_FIND_LINKS=$UV_FIND_LINKS): $version"
}

# `memriver install --harness <h> --yes` on a fresh HOME: the store has no
# global project yet, so the plan carries the required store step
# ("memory store (required): create the global project in <root>") and --yes
# initializes it before any harness file is written, printing
# "memory store: ready (global project <id>)" (cli._store_step). Failures go to
# stderr, which must stay empty. The store is one SQLite file,
# <root>/memriver.db (mode 0600, PRAGMA user_version = 2): the printed id must
# be the one row of `projects` with is_global = 1 (name "global", no root), and
# none of the old file-store names (store.toml, projects/, memories/,
# registry/) may exist.
step_install_harness() {
    local harness="$1" out err
    out="$(mktemp)"; err="$(mktemp)"
    if ! uvx memriver install --harness "$harness" --yes >"$out" 2>"$err"; then
        echo "FAIL: memriver install --harness $harness --yes exited nonzero" >&2
        cat "$out"; cat "$err" >&2
        exit 1
    fi
    python3 - "$out" "$err" "$E2E_STORE" <<'PY'
import re, sqlite3, stat, sys
from pathlib import Path
out, err, store = Path(sys.argv[1]).read_text(), Path(sys.argv[2]).read_text(), Path(sys.argv[3])
assert err == "", f"install wrote to stderr: {err!r}"
assert "memory store (required): create the global project in " in out, out
m = re.search(r"^memory store: ready \(global project ([0-9a-hjkmnp-tv-z]{10})\)$", out, re.M)
assert m, f"no 'memory store: ready (global project <id>)' line in install stdout:\n{out}"
gid = m.group(1)
db = store / "memriver.db"
assert db.is_file(), f"{db} missing"
assert stat.S_IMODE(db.stat().st_mode) == 0o600, oct(db.stat().st_mode)
legacy = [n for n in ("store.toml", "projects", "memories", "registry") if (store / n).exists()]
assert not legacy, f"old file-store names present: {legacy}"
conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
rows = conn.execute("SELECT id, name, root FROM projects WHERE is_global = 1").fetchall()
assert rows == [(gid, "global", None)], rows
print(f"global project {gid}")
PY
    rm -f "$out" "$err"
    pass "uvx memriver install --harness $harness --yes: exit 0, empty stderr, store step shown and applied; memriver.db (0600, user_version 2) holds the printed id as its one is_global row; no old store files"
}

step_install() { step_install_harness claude-code; }

step_assert_config() {
    python3 - <<'PY'
import json
from pathlib import Path

home = Path.home()

claude_json = json.loads((home / ".claude.json").read_text())
mcp = claude_json.get("mcpServers", {}).get("memriver")
expected_mcp = {"command": "uvx", "args": ["memriver", "serve", "--harness", "claude-code"]}
assert mcp == expected_mcp, f"unexpected mcpServers.memriver: {mcp!r}"

settings = json.loads((home / ".claude" / "settings.json").read_text())
hooks = settings["hooks"]
for event, name in [("SessionStart", "session-start"), ("UserPromptSubmit", "user-prompt-submit"),
                    ("Stop", "stop"), ("SessionEnd", "session-end"), ("PreToolUse", "pre-tool-use")]:
    command = hooks[event][0]["hooks"][0]["command"]
    assert f"uvx memriver hook {name} --harness claude-code" in command, (event, command)
assert hooks["PreToolUse"][0]["matcher"] == "mcp__memriver__.*", hooks["PreToolUse"][0]
assert settings["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1", settings.get("env")
print("config assertions ok")
PY
    pass "~/.claude.json (mcpServers.memriver: serve --harness claude-code) and ~/.claude/settings.json (five hooks, PreToolUse scoped to memriver tools, env) hold the expected entries"
}

# Registers E2E_PROJECT_DIR as a project, the way a user does
# (`memriver project init <dir> --yes`), and exports its id as E2E_PROJECT_ID.
# The Stop hook only nudges inside a registered project (hooks._stop), and the
# SessionStart header names it, so every stage that exercises Stop runs there.
step_register_project() {
    local out
    mkdir -p "$E2E_PROJECT_DIR"
    if ! out="$(uvx memriver project init "$E2E_PROJECT_DIR" --yes)"; then
        echo "FAIL: memriver project init exited nonzero:" >&2
        printf '%s\n' "$out" >&2
        exit 1
    fi
    E2E_PROJECT_ID="$(python3 - "$out" "$E2E_PROJECT_DIR" <<'PY'
import re, sys
out, root = sys.argv[1], sys.argv[2]
m = re.search(r"^created project e2e-project \[([0-9a-hjkmnp-tv-z]{10})\] and bound (.+)$", out, re.M)
assert m, f"unexpected project init output:\n{out}"
assert m.group(2) == root, (m.group(2), root)
print(m.group(1))
PY
)"
    export E2E_PROJECT_ID
    pass "uvx memriver project init $E2E_PROJECT_DIR --yes: created project e2e-project [$E2E_PROJECT_ID]"
}

# Seeds one GLOBAL memory whose description (the index cue) and body are
# $1. Global is read-only to agents through the service (GlobalReadOnly;
# service.record only writes the session's own project), so this seeds it the
# way an operator would: one INSERT into <root>/memriver.db's `memories` table,
# with project_id = the id of the one is_global project and foreign keys on.
# The id and timestamps come from core's own model (Memory.new), so the row is
# what memriver itself would store: version 1, deleted_at NULL. trust="user":
# a human wrote it. The content is passed through argv, not interpolated into
# the heredoc.
seed_global_memory() {
    local content="$1" memory_id
    memory_id="$(uv run --no-project --with memriver python - "$E2E_STORE" "$content" <<'PY'
import sqlite3, sys
from pathlib import Path
from memriver_core.models import Memory

db, content = Path(sys.argv[1]) / "memriver.db", sys.argv[2]
conn = sqlite3.connect(f"{db.as_uri()}?mode=rw", uri=True)
conn.execute("PRAGMA foreign_keys = ON")
with conn:
    (gid,) = conn.execute("SELECT id FROM projects WHERE is_global = 1").fetchone()
    m = Memory.new(body=content, type="project", project_id=gid,
                   source={"harness": "e2e", "method": "manual"}, trust="user",
                   description=content)
    conn.execute(
        "INSERT INTO memories (id, project_id, type, source_harness, source_method, trust,"
        " sync, description, body, created, updated, version, deleted_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)",
        (m.id, m.project_id, m.type, m.source["harness"], m.source["method"], m.trust,
         int(m.sync), m.description, m.body, m.created, m.updated))
conn.close()
print(m.id)
PY
)"
    SEEDED_MEMORY_ID="$memory_id"
    pass "seeded global memory $memory_id ('$content') as a memriver.db row, project_id = the is_global project"
}

# A distinctive, checkable fact (not a generic "e2e marker memory" label) is
# the point: "project mascot is a purple axolotl named Quibble" is 49 chars,
# fits the 60-char cue budget uncut, and "Quibble"/"axolotl" are not words a
# model would produce unprompted.
step_seed_memory() {
    seed_global_memory "project mascot is a purple axolotl named Quibble"
}

# The exact SessionStart / Stop command strings the installer wrote, read back
# from ~/.claude/settings.json -- used to drive "the exact written command",
# not a hand-typed approximation of it.
extract_hook_command() {
    python3 - "$1" <<'PY'
import json, sys
from pathlib import Path
settings = json.loads((Path.home() / ".claude" / "settings.json").read_text())
print(settings["hooks"][sys.argv[1]][0]["hooks"][0]["command"])
PY
}

extract_session_start_command() { extract_hook_command SessionStart; }
extract_stop_command() { extract_hook_command Stop; }

# Parses a `claude ... --output-format stream-json --verbose` transcript
# (path in $1) into eval-able shell assignments: ASSISTANT_AGGREGATE (every
# assistant turn's text, joined with "\n---\n" -- a Stop-nudge continuation
# is a *second* turn, so aggregating all of them, not just the final "result"
# field, matters), NUM_TURNS, and SESSION_ID (both read from the stream's
# "result" event).
#
# NOTE: parses from a temp FILE, not a pipe into `python3 - <<'PY' ... PY`. A
# pipe would be wrong: the heredoc is what supplies python3's own stdin (the
# *script* itself, since "-" means "read the program from stdin"), so by the
# time the script runs, stdin is already at EOF and a `for line in
# sys.stdin` loop silently sees nothing. Verified this failure mode offline
# before ever spending a real `claude -p` call on it.
parse_stream_json_file() {
    python3 - "$1" <<'PY'
import json
import shlex
import sys

assistant_texts = []
num_turns = None
session_id = None

with open(sys.argv[1]) as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "assistant":
            for block in obj.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text"):
                    assistant_texts.append(block["text"])
        elif obj.get("type") == "result":
            num_turns = obj.get("num_turns")
            session_id = obj.get("session_id")

aggregate = "\n---\n".join(assistant_texts)
print(f"ASSISTANT_AGGREGATE={shlex.quote(aggregate)}")
print(f"NUM_TURNS={shlex.quote(str(num_turns))}")
print(f"SESSION_ID={shlex.quote(str(session_id))}")
PY
}

# Runs `claude "$@" --output-format stream-json --verbose`, captures the
# transcript to a temp file, parses it with parse_stream_json_file, and
# eval's the result -- setting ASSISTANT_AGGREGATE / NUM_TURNS / SESSION_ID
# in the CALLER's shell (this is a function, not a subshell, so the eval's
# variables stick around after it returns). Cleans up its own temp file.
run_claude_stream_json() {
    local tmp_stream
    tmp_stream="$(mktemp)"
    claude "$@" --output-format stream-json --verbose > "$tmp_stream"
    eval "$(parse_stream_json_file "$tmp_stream")"
    rm -f "$tmp_stream"
}

run_shared_setup() {
    step_verify_uvx_resolution
    step_install
    step_assert_config
    step_register_project
    step_seed_memory
}

# --- Azure AI Foundry as the model provider (stages 2, 3, 5) ------------------
# The runners pass AZURE_FOUNDRY_* into the container by name; these map them
# onto what each harness reads. Billing is per token on the Foundry resource,
# not a Claude or ChatGPT subscription.

# Claude Code: its documented Microsoft Foundry variables, with every model
# alias pinned to the one deployment (unpinned aliases resolve to built-in
# defaults the resource may not have deployed).
use_foundry_for_claude() {
    export CLAUDE_CODE_USE_FOUNDRY=1
    export ANTHROPIC_FOUNDRY_BASE_URL="${AZURE_FOUNDRY_BASEURL%/}/anthropic"
    export ANTHROPIC_FOUNDRY_API_KEY="$AZURE_FOUNDRY_API_KEY"
    export ANTHROPIC_DEFAULT_OPUS_MODEL="$AZURE_FOUNDRY_CLAUDE_DEPLOYMENT"
    export ANTHROPIC_DEFAULT_SONNET_MODEL="$AZURE_FOUNDRY_CLAUDE_DEPLOYMENT"
    export ANTHROPIC_DEFAULT_HAIKU_MODEL="$AZURE_FOUNDRY_CLAUDE_DEPLOYMENT"
    pass "Claude Code uses Microsoft Foundry (deployment $AZURE_FOUNDRY_CLAUDE_DEPLOYMENT)"
}

# Codex: a model provider in ~/.codex/config.toml, written after memriver
# install. Top-level keys must precede every table, so they are prepended; the
# provider table is appended. The key stays in the environment (env_key).
use_foundry_for_codex() {
    local config="$HOME/.codex/config.toml" tmp
    tmp="$(mktemp)"
    {
        printf 'model_provider = "azure-foundry"\n'
        printf 'model = "%s"\n\n' "$AZURE_FOUNDRY_GPT_DEPLOYMENT"
        cat "$config"
        printf '\n[model_providers.azure-foundry]\n'
        printf 'name = "Azure AI Foundry"\n'
        printf 'base_url = "%s/openai/v1"\n' "${AZURE_FOUNDRY_BASEURL%/}"
        printf 'env_key = "AZURE_FOUNDRY_API_KEY"\n'
        printf 'wire_api = "responses"\n'
    } > "$tmp"
    mv "$tmp" "$config"
    python3 -c 'import sys, tomllib; tomllib.load(open(sys.argv[1], "rb"))' "$config"
    pass "Codex uses Azure AI Foundry (deployment $AZURE_FOUNDRY_GPT_DEPLOYMENT) through ~/.codex/config.toml"
}

# One row of the sessions table, as eval-able shell assignments:
# SESSION_STATUS, SESSION_PROJECT, SESSION_PROMPTS, SESSION_NUDGED_AT.
read_session_row() {
    python3 - "$E2E_STORE/memriver.db" "$1" "$2" <<'PY'
import shlex, sqlite3, sys
db, harness, session_id = sys.argv[1:]
row = sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(
    "SELECT status, project_id, prompt_count, last_nudge_prompt_count FROM sessions"
    " WHERE harness = ? AND session_id = ?", (harness, session_id)).fetchone()
assert row is not None, f"no sessions row for ({harness}, {session_id})"
for name, value in zip(("SESSION_STATUS", "SESSION_PROJECT", "SESSION_PROMPTS", "SESSION_NUDGED_AT"), row):
    print(f"{name}={shlex.quote(str(value))}")
PY
}

# --- Codex-specific counterparts (stage5.sh) ---------------------------------
# Codex's install targets differ from Claude Code's (~/.codex/config.toml +
# ~/.codex/hooks.json instead of ~/.claude.json + ~/.claude/settings.json), so
# these are separate functions rather than a harness argument threaded through
# the Claude-Code ones above -- same reasoning as memriver's own per-harness
# hook encoders (hooks.py): the schemas are owned by two vendors and have
# already diverged (see extract_codex_*_command below).

step_install_codex() { step_install_harness codex; }

step_assert_config_codex() {
    python3 - <<'PY'
import json
import tomllib
from pathlib import Path

home = Path.home()

config = tomllib.loads((home / ".codex" / "config.toml").read_text())
mcp = config.get("mcp_servers", {}).get("memriver")
assert mcp == {"command": "uvx", "args": ["memriver", "serve", "--harness", "codex"]}, f"unexpected mcp_servers.memriver: {mcp!r}"

hooks = json.loads((home / ".codex" / "hooks.json").read_text())["hooks"]
for event, name in [("SessionStart", "session-start"), ("UserPromptSubmit", "user-prompt-submit"),
                    ("Stop", "stop"), ("SessionEnd", "session-end")]:
    command = hooks[event][0]["hooks"][0]["command"]
    assert command == f"uvx memriver hook {name} --harness codex", (event, command)
assert "PreToolUse" not in hooks, hooks.keys()
print("config assertions ok")
PY
    pass "~/.codex/config.toml (mcp_servers.memriver: serve --harness codex) and ~/.codex/hooks.json (SessionStart, UserPromptSubmit, Stop, SessionEnd) hold the expected entries"
}

extract_codex_session_start_command() {
    python3 - <<'PY'
import json
from pathlib import Path
hooks = json.loads((Path.home() / ".codex" / "hooks.json").read_text())
print(hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"])
PY
}

extract_codex_stop_command() {
    python3 - <<'PY'
import json
from pathlib import Path
hooks = json.loads((Path.home() / ".codex" / "hooks.json").read_text())
print(hooks["hooks"]["Stop"][0]["hooks"][0]["command"])
PY
}

run_shared_setup_codex() {
    step_verify_uvx_resolution
    step_install_codex
    step_assert_config_codex
    step_register_project
}

# Parses a `codex exec --json` JSONL stream (path in $1) into eval-able shell
# assignments. Codex's event names (documented at
# developers.openai.com/codex/non-interactive-mode): "thread.started" carries
# thread_id; "item.completed" items of type "agent_message" carry the model's
# text (one per turn -- same reasoning as parse_stream_json_file above: a Stop
# nudge continuation is a second turn, so every item.completed is collected,
# not just the last one); "turn.completed"/"turn.failed" mark turn boundaries,
# counted as NUM_TURNS (Claude Code's stream-json exposes this as a single
# "result" event's num_turns field; Codex has no equivalent single summary
# event, so it's counted here instead); "error" sets HAD_ERROR.
parse_codex_json_file() {
    python3 - "$1" <<'PY'
import json
import shlex
import sys

assistant_texts = []
num_turns = 0
thread_id = None
had_error = False

with open(sys.argv[1]) as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = obj.get("type")
        if event == "thread.started":
            thread_id = obj.get("thread_id")
        elif event == "item.completed":
            item = obj.get("item", {})
            if item.get("type") == "agent_message" and item.get("text"):
                assistant_texts.append(item["text"])
        elif event in ("turn.completed", "turn.failed"):
            num_turns += 1
        elif event == "error":
            had_error = True

aggregate = "\n---\n".join(assistant_texts)
print(f"ASSISTANT_AGGREGATE={shlex.quote(aggregate)}")
print(f"NUM_TURNS={shlex.quote(str(num_turns))}")
print(f"THREAD_ID={shlex.quote(str(thread_id))}")
print(f"HAD_ERROR={shlex.quote(str(had_error))}")
PY
}

# Runs `codex exec "$@" --json -o <tmp>`, captures stdout (the JSONL stream),
# stderr, and the exit code -- WITHOUT `set -e` aborting the script, since
# stage5 needs to report a nonzero exit / stderr as evidence, not die on it.
# Sets (in the CALLER's shell, same eval trick as run_claude_stream_json):
# ASSISTANT_AGGREGATE, NUM_TURNS, THREAD_ID, HAD_ERROR, CODEX_EXIT_CODE,
# CODEX_STDERR, CODEX_LAST_MESSAGE.
run_codex_exec_json() {
    local tmp_stream tmp_err tmp_last rc
    tmp_stream="$(mktemp)"
    tmp_err="$(mktemp)"
    tmp_last="$(mktemp)"
    set +e
    codex exec "$@" --json -o "$tmp_last" >"$tmp_stream" 2>"$tmp_err"
    rc=$?
    set -e
    eval "$(parse_codex_json_file "$tmp_stream")"
    CODEX_EXIT_CODE="$rc"
    CODEX_STDERR="$(cat "$tmp_err")"
    CODEX_LAST_MESSAGE="$(cat "$tmp_last" 2>/dev/null || true)"
    rm -f "$tmp_stream" "$tmp_err" "$tmp_last"
}

# Asserts one SessionStart hook stdout ($1): valid JSON, the additionalContext
# holds the header line $2 right after the begin delimiter (session.py /
# hooks._compose: prefix, begin delimiter, header, index body, end delimiter),
# and a global index line for the seeded memory -- "- [project, global] <id>:
# <cue> (<date>)" (service._index_line) -- carrying marker $4, between both
# delimiters.
assert_session_start() {
    python3 - "$1" "$2" "$3" "$4" <<'PY'
import json, sys
out, header, memory_id, marker = sys.argv[1:]
obj = json.loads(out)
assert obj["hookSpecificOutput"]["hookEventName"] == "SessionStart", obj
ctx = obj["hookSpecificOutput"]["additionalContext"]
begin, end = "--- memriver index begin ---", "--- memriver index end ---"
assert begin in ctx and end in ctx, ctx
inside = ctx.split(begin, 1)[1].split(end, 1)[0].strip("\n").split("\n")
assert inside[0] == header, f"header line {inside[0]!r} != {header!r}"
lines = [l for l in inside[1:] if l.startswith(f"- [project, global] {memory_id}: ")]
assert lines and marker in lines[0], f"no global index line for {memory_id} with {marker!r}: {inside!r}"
PY
}
