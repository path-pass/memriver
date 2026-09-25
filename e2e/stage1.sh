#!/usr/bin/env bash
# Stage 1: clean-machine install + the exact written hook command, end to end.
# Runs INSIDE the container (see run-stage1.sh). No credentials needed: this
# only exercises what docs/verification/phase2-hooks.md calls the "CLI-level
# hook smoke", but through the literal `uvx memriver ...` strings the real
# installer wrote, not hand-typed equivalents.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

run_shared_setup

# --- the hand-seeded global memory is valid store data -----------------------
doctor_json="$(uvx memriver doctor --json)"
python3 - "$doctor_json" <<'PY'
import json, sys
report = json.loads(sys.argv[1])
assert report["state"] == "healthy" and report["findings"] == [], report
PY
pass "memriver doctor --json: state=healthy, no findings (memriver.db with the global and e2e-project projects and the seeded memory row)"

# --- the human views: list / show / export ------------------------------------
GLOBAL_ID="$(python3 -c 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("SELECT id FROM projects WHERE is_global = 1").fetchone()[0])' "$E2E_STORE/memriver.db")"

list_out="$(uvx memriver list)"
printf '%s\n' "$list_out"
python3 - "$list_out" "$GLOBAL_ID" "$SEEDED_MEMORY_ID" "$E2E_PROJECT_ID" <<'PY'
import re, sys
out, gid, mid, pid = sys.argv[1:]
lines = out.splitlines()
i = lines.index(f"{gid}  global  (global)")
assert re.fullmatch(rf"  {mid}  \[project\]  \d{{4}}-\d{{2}}-\d{{2}}  project mascot is a purple axolotl named Quibble", lines[i + 1]), lines[i + 1]
assert f"{pid}  e2e-project  (/root/e2e-project)" in lines, lines
PY
pass "uvx memriver list: the global project lists the seeded memory ($SEEDED_MEMORY_ID); e2e-project listed with its root"

show_out="$(uvx memriver show "$SEEDED_MEMORY_ID")"
printf '%s\n' "$show_out"
python3 - "$show_out" "$GLOBAL_ID" "$SEEDED_MEMORY_ID" <<'PY'
import sys
out, gid, mid = sys.argv[1:]
head, body = out.split("\n---\n", 1)
fields = dict(l.split(": ", 1) for l in head.splitlines())
assert fields["id"] == mid and fields["project"] == gid, fields
assert fields["version"] == "1" and fields["type"] == "project" and fields["trust"] == "user", fields
assert fields["source"] == "e2e/manual" and "deleted" not in fields, fields
assert body.strip() == "project mascot is a purple axolotl named Quibble", body
PY
pass "uvx memriver show $SEEDED_MEMORY_ID: project=$GLOBAL_ID, version: 1, trust user, body intact"

export_out="$(uvx memriver export /root/snap)"
printf '%s\n' "$export_out"
python3 - "$export_out" "$GLOBAL_ID" "$SEEDED_MEMORY_ID" "$E2E_PROJECT_ID" <<'PY'
import stat, sys
from pathlib import Path
out, gid, mid, pid = sys.argv[1:]
assert out.strip() == "exported 1 memories to /root/snap", out
snap = Path("/root/snap")
mode = lambda p: stat.S_IMODE(p.stat().st_mode)
assert mode(snap) == 0o700 and mode(snap / gid) == 0o700, (oct(mode(snap)), oct(mode(snap / gid)))
f = snap / gid / f"{mid}.md"
assert f.is_file() and mode(f) == 0o600, f
text = f.read_text()
assert text.startswith("---\n") and f'id: "{mid}"\n' in text and "version: 1\n" in text, text
assert text.endswith("---\nproject mascot is a purple axolotl named Quibble"), text
projects = snap / "projects.md"
assert projects.is_file() and mode(projects) == 0o600
ptext = projects.read_text()
assert f"- {gid} global (global)\n" in ptext and f"- {pid} e2e-project (/root/e2e-project)\n" in ptext, ptext
assert sorted(p.name for p in snap.iterdir()) == sorted([gid, "projects.md"]), list(snap.iterdir())
PY
pass "uvx memriver export /root/snap: /root/snap/$GLOBAL_ID/$SEEDED_MEMORY_ID.md (0600, version 1) and projects.md (0600); no folder for the empty e2e-project"

# --- Session hooks, driven through the exact installed commands --------------
# Every hook payload carries a session_id: a session is registered once, at
# its first SessionStart, to the project its entry directory resolves to (or
# to none), and every later hook reads that registration.
SESSION_START_CMD="$(extract_hook_command SessionStart)"
PROMPT_CMD="$(extract_hook_command UserPromptSubmit)"
STOP_CMD="$(extract_hook_command Stop)"
PRE_TOOL_CMD="$(extract_hook_command PreToolUse)"
SESSION_END_CMD="$(extract_hook_command SessionEnd)"
PROJECT_HEADER="project: e2e-project [$E2E_PROJECT_ID] (root $E2E_PROJECT_DIR)"
NONE_HEADER="project: none — this session was registered with no project, so global is read-only; to save, ask the user to run memriver project init where this session started, then call session_register"
IN_PROJECT=e2e-session-in-project
OUTSIDE=e2e-session-outside

out="$(printf '{"session_id":"%s","cwd":"%s","source":"startup"}' "$IN_PROJECT" "$E2E_PROJECT_DIR" | sh -c "$SESSION_START_CMD")"
assert_session_start "$out" "$PROJECT_HEADER" "$SEEDED_MEMORY_ID" "Quibble"
pass "SessionStart command '$SESSION_START_CMD' (cwd = registered project) exits 0, emits valid JSON; additionalContext has the header '$PROJECT_HEADER' and the seeded global memory between both index delimiters"

out="$(printf '{"session_id":"%s","cwd":"/root","source":"startup"}' "$OUTSIDE" | sh -c "$SESSION_START_CMD")"
assert_session_start "$out" "$NONE_HEADER" "$SEEDED_MEMORY_ID" "Quibble"
pass "SessionStart (cwd = unregistered /root) still injects the global memory, under the no-project header"

# Stop nudges only after 5 main-session prompts without a save, so five
# prompts go through the installed UserPromptSubmit command first.
for session in "$IN_PROJECT" "$OUTSIDE"; do
    cwd="$E2E_PROJECT_DIR"; [ "$session" = "$OUTSIDE" ] && cwd=/root
    for n in 1 2 3 4 5; do
        out="$(printf '{"session_id":"%s","cwd":"%s","prompt":"e2e prompt %s"}' "$session" "$cwd" "$n" | sh -c "$PROMPT_CMD")"
        if [ -n "$out" ] && ! python3 -c 'import json,sys; json.loads(sys.argv[1])' "$out"; then
            echo "FAIL: UserPromptSubmit printed non-JSON output: $out" >&2
            exit 1
        fi
    done
done
pass "UserPromptSubmit command '$PROMPT_CMD' ran 5 prompts in each session, exit 0"

stop_first_output="$(printf '{"session_id":"%s","cwd":"%s","stop_hook_active":false}' "$IN_PROJECT" "$E2E_PROJECT_DIR" | sh -c "$STOP_CMD")"
python3 - "$stop_first_output" <<'PY'
import json, sys
obj = json.loads(sys.argv[1])
assert obj.get("decision") == "block" and obj.get("reason", "").startswith("[memriver] Before finishing"), f"expected the block nudge, got {obj!r}"
PY
pass "Stop command '$STOP_CMD' (registered session, 5 unsaved prompts) exits 0 and returns decision=block with the save nudge"

for payload in \
    "{\"session_id\":\"$IN_PROJECT\",\"cwd\":\"$E2E_PROJECT_DIR\",\"stop_hook_active\":true}" \
    "{\"session_id\":\"$IN_PROJECT\",\"cwd\":\"$E2E_PROJECT_DIR\",\"stop_hook_active\":false}" \
    "{\"session_id\":\"$OUTSIDE\",\"cwd\":\"/root\",\"stop_hook_active\":false}"; do
    out="$(printf '%s' "$payload" | sh -c "$STOP_CMD")"
    if [ -n "$out" ]; then
        echo "FAIL: expected no nudge for $payload, got: $out" >&2
        exit 1
    fi
done
pass "Stop stays silent on the continuation (stop_hook_active=true), right after a nudge with no new prompts, and in a session with no project"

out="$(printf '{"session_id":"%s","cwd":"%s","tool_name":"mcp__memriver__memory_index","tool_use_id":"toolu_01E2EPROBE000000000000000"}' "$IN_PROJECT" "$E2E_PROJECT_DIR" | sh -c "$PRE_TOOL_CMD")"
[ -z "$out" ] || { echo "FAIL: PreToolUse printed output: $out" >&2; exit 1; }
out="$(printf '{"session_id":"%s","cwd":"%s","reason":"exit"}' "$IN_PROJECT" "$E2E_PROJECT_DIR" | sh -c "$SESSION_END_CMD")"
[ -z "$out" ] || { echo "FAIL: SessionEnd printed output: $out" >&2; exit 1; }
pass "PreToolUse command '$PRE_TOOL_CMD' and SessionEnd command '$SESSION_END_CMD' exit 0 with empty stdout"

python3 - "$E2E_STORE/memriver.db" "$IN_PROJECT" "$OUTSIDE" "$E2E_PROJECT_ID" <<'PY'
import sqlite3, sys
db, in_project, outside, pid = sys.argv[1:]
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
rows = {r[0]: r[1:] for r in conn.execute(
    "SELECT session_id, harness, status, project_id, prompt_count, last_nudge_prompt_count,"
    " ended_at IS NOT NULL FROM sessions")}
assert rows[in_project] == ("claude-code", "registered", pid, 5, 5, 1), rows[in_project]
assert rows[outside] == ("claude-code", "registered", None, 5, 0, 0), rows[outside]
calls = conn.execute("SELECT harness, session_id FROM tool_calls").fetchall()
assert calls == [("claude-code", in_project)], calls
PY
sessions_out="$(uvx memriver sessions)"
printf '%s\n' "$sessions_out"
case "$sessions_out" in
    *"claude-code $IN_PROJECT"*"resume: claude --resume $IN_PROJECT"*) ;;
    *) echo "FAIL: memriver sessions does not list $IN_PROJECT with its resume command" >&2; exit 1 ;;
esac
pass "sessions table: $IN_PROJECT registered to e2e-project (5 prompts, nudged at 5, ended), $OUTSIDE registered with no project; the PreToolUse call is mapped to $IN_PROJECT; memriver sessions lists it with its resume command"

# --- Claude Code binary present ----------------------------------------------
claude_version="$(claude --version)"
pass "claude binary present: $claude_version"

echo
echo "=== STAGE 1: ALL CHECKS PASSED ==="
