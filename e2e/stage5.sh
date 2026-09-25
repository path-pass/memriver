#!/usr/bin/env bash
# Stage 5: the Codex CLI counterpart of stage2.sh -- a real `codex exec`
# non-interactive turn against a live Codex install on Azure AI Foundry:
# SessionStart injection reaches the model, the session is registered to the
# project it entered from (one prompt counted, no Stop nudge -- that waits for
# 5 unsaved prompts), and a real memriver tool call is answered by the MCP
# server (`serve --harness codex`) with the session's project.
#
# Spends Foundry tokens. Run it only via run-stage5.sh.
#
# --- Hook trust (read this before changing the --dangerously-bypass-hook-trust
# calls below) ---------------------------------------------------------------
# Codex requires every non-managed hook to be reviewed and trusted before it
# runs -- trust is recorded per hook, keyed by the hook's current content hash,
# in `[hooks.state."<path>:<event>:<idx>:<idx>"]` tables inside
# ~/.codex/config.toml. `/hooks` in the interactive TUI is the normal way to
# grant it; there is no separate non-interactive "trust" subcommand, and the
# stored hash isn't a documented, stable format worth hand-computing for a
# throwaway container.
#
# Instead, Codex documents exactly this case: "For one-off automation that
# already vets hook sources outside Codex, pass --dangerously-bypass-hook-trust
# to run enabled hooks without requiring persisted hook trust for that
# invocation." (developers.openai.com/codex/hooks#review-and-trust-hooks,
# fetched 2026-09-12). This container's ~/.codex is fresh, its only hooks are
# the four memriver wrote moments ago, and nothing else runs in it -- exactly
# the "already vets hook sources" case the flag exists for. That's the
# mechanism this script uses.
#
# The PROBE run below deliberately omits the flag first, to confirm (not just
# assume) that an untrusted hook is silently skipped rather than run -- see
# "criterion 0".
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

echo "==> codex version: $(codex --version)"

run_shared_setup_codex
use_foundry_for_codex
SESSION_START_CMD="$(extract_codex_session_start_command)"

echo "==> seeding the global marker memory used by the recall question"
seed_global_memory "the support contact is Ombudsman Krakenfeld"

# Every codex exec below runs from the registered project, so its session is
# registered to it at its first SessionStart.
cd "$E2E_PROJECT_DIR"
PROJECT_HEADER="project: e2e-project [$E2E_PROJECT_ID] (root $E2E_PROJECT_DIR)"

session_start_output="$(printf '{"session_id":"e2e-codex-hook-probe","cwd":"%s","source":"startup"}' "$E2E_PROJECT_DIR" | sh -c "$SESSION_START_CMD")"
assert_session_start "$session_start_output" "$PROJECT_HEADER" "$SEEDED_MEMORY_ID" "Krakenfeld"
pass "SessionStart command '$SESSION_START_CMD' exits 0, emits valid JSON; additionalContext has the header '$PROJECT_HEADER' and the seeded global memory between both index delimiters"

RECALL_PROMPT="According to your memory, who is the support contact? Answer in one sentence."
COMMON_EXEC_FLAGS=(--skip-git-repo-check --ephemeral --sandbox read-only)

show_run() {
    echo "----- $1: codex exit=$CODEX_EXIT_CODE num_turns=$NUM_TURNS thread=$THREAD_ID had_error=$HAD_ERROR -----"
    printf '%s\n' "$ASSISTANT_AGGREGATE"
    echo "----- stderr -----"
    printf '%s\n' "$CODEX_STDERR" | tail -20
    echo "------------------"
}

echo "==> criterion 0 (probe, no bypass flag): confirm an untrusted hook does not run"
run_codex_exec_json "${COMMON_EXEC_FLAGS[@]}" "$RECALL_PROMPT"
show_run probe
if printf '%s' "$ASSISTANT_AGGREGATE" | grep -qi "Krakenfeld\|Ombudsman"; then
    echo "NOTE: the untrusted-hook probe answered correctly anyway -- this Codex"
    echo "      version may auto-trust user-level ~/.codex/hooks.json. Recorded,"
    echo "      not a failure; see e2e/README.md."
else
    pass "criterion 0: untrusted hook did not inject the marker (the documented trust gate)"
fi

echo "==> criterion 1/2 (--dangerously-bypass-hook-trust): injection and the session registry"
run_codex_exec_json --dangerously-bypass-hook-trust "${COMMON_EXEC_FLAGS[@]}" "$RECALL_PROMPT"
show_run recall
[ "$CODEX_EXIT_CODE" -eq 0 ] || { echo "FAIL: codex exec exited $CODEX_EXIT_CODE" >&2; exit 1; }
if printf '%s' "$ASSISTANT_AGGREGATE" | grep -q "Krakenfeld"; then
    pass "criterion 1: the answer names 'Krakenfeld' -- only the injected index line holds that word"
elif printf '%s' "$ASSISTANT_AGGREGATE" | grep -qi "Ombudsman"; then
    pass "criterion 1 (secondary match): the answer names 'Ombudsman'"
else
    echo "FAIL: neither 'Krakenfeld' nor 'Ombudsman' in any agent_message" >&2
    exit 1
fi
eval "$(read_session_row codex "$THREAD_ID")"
if [ "$SESSION_STATUS" != "registered" ] || [ "$SESSION_PROJECT" != "$E2E_PROJECT_ID" ] || [ "$SESSION_PROMPTS" != "1" ]; then
    echo "FAIL: session row ($SESSION_STATUS, $SESSION_PROJECT, $SESSION_PROMPTS prompts); expected (registered, $E2E_PROJECT_ID, 1)" >&2
    exit 1
fi
[ "$NUM_TURNS" = "1" ] || { echo "FAIL: one unsaved prompt must not be nudged; num_turns=$NUM_TURNS" >&2; exit 1; }
pass "criterion 2: session $THREAD_ID registered to e2e-project, 1 prompt counted, no Stop nudge (num_turns=1)"

echo "==> criterion 3: a real memory_index call, routed by the calling session"
# codex exec cannot answer an approval prompt, so this one read-only tool is
# pre-approved; every other memriver tool keeps Codex's default.
printf '\n[mcp_servers.memriver.tools.memory_index]\napproval_mode = "approve"\n' >> "$HOME/.codex/config.toml"
python3 -c 'import sys, tomllib; tomllib.load(open(sys.argv[1], "rb"))' "$HOME/.codex/config.toml"
run_codex_exec_json --dangerously-bypass-hook-trust "${COMMON_EXEC_FLAGS[@]}" \
    "Call the memriver memory_index tool now, then reply with only the first line of its output, verbatim."
show_run memory_index
[ "$CODEX_EXIT_CODE" -eq 0 ] || { echo "FAIL: codex exec exited $CODEX_EXIT_CODE" >&2; exit 1; }
if ! printf '%s' "$ASSISTANT_AGGREGATE" | grep -qF "project: e2e-project [$E2E_PROJECT_ID]"; then
    echo "FAIL: the answer does not quote the session's project header '$PROJECT_HEADER'" >&2
    exit 1
fi
pass "criterion 3: memory_index, called by the model, answered with the session's project header"

echo
echo "=== STAGE 5: CHECKS COMPLETE ==="
