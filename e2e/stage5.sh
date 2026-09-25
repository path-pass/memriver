#!/usr/bin/env bash
# Stage 5: the Codex CLI counterpart of stage2.sh -- a real `codex exec`
# non-interactive turn against a live Codex install, exercising SessionStart
# injection and the Stop nudge for real, the same way stage2 does for
# `claude -p`.
#
# Needs a real ~/.codex/auth.json and spends real ChatGPT-plan quota. Run it
# only via run-stage5.sh.
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
# the two memriver wrote moments ago, and nothing else runs in it -- exactly
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

echo "==> staging host auth into this container's own (fresh) HOME"
mkdir -p ~/.codex
install -m 600 /host-auth.json ~/.codex/auth.json
pass "copied /host-auth.json (mounted read-only from the host) to ~/.codex/auth.json, mode 600"

run_shared_setup_codex

# --- offline hook-content check, no codex call, mirrors stage1.sh -----------
# Cheap and quota-free: proves the exact commands the installer wrote produce
# the right JSON shape, before spending a single real `codex exec` call on it.
SESSION_START_CMD="$(extract_codex_session_start_command)"
STOP_CMD="$(extract_codex_stop_command)"

echo "==> seeding the global marker memory used by the recall question"
seed_global_memory "the support contact is Ombudsman Krakenfeld"

# every codex call below runs inside the registered project: the Stop nudge
# only fires there, and the SessionStart header names it
cd "$E2E_PROJECT_DIR"
PROJECT_HEADER="project: e2e-project [$E2E_PROJECT_ID] (root $E2E_PROJECT_DIR)"

session_start_output="$(printf '{"cwd":"%s","source":"startup"}' "$E2E_PROJECT_DIR" | sh -c "$SESSION_START_CMD")"
assert_session_start "$session_start_output" "$PROJECT_HEADER" "$SEEDED_MEMORY_ID" "Krakenfeld"
pass "SessionStart command '$SESSION_START_CMD' exits 0, emits valid JSON; additionalContext has the header '$PROJECT_HEADER' and the seeded global memory between both index delimiters"

stop_first_output="$(printf '{"cwd":"%s","stop_hook_active":false}' "$E2E_PROJECT_DIR" | sh -c "$STOP_CMD")"
python3 - "$stop_first_output" <<'PY'
import json, sys
obj = json.loads(sys.argv[1])
assert obj.get("decision") == "block", f"expected decision=block, got {obj!r}"
PY
pass "Stop command '$STOP_CMD' with stop_hook_active=false exits 0 and returns decision=block"

stop_second_output="$(printf '{"cwd":"%s","stop_hook_active":true}' "$E2E_PROJECT_DIR" | sh -c "$STOP_CMD")"
if [ -n "$stop_second_output" ]; then
    echo "FAIL: expected empty stdout for stop_hook_active=true, got: $stop_second_output" >&2
    exit 1
fi
pass "Stop command '$STOP_CMD' with stop_hook_active=true exits 0 with empty stdout"

RECALL_PROMPT="According to your memory, who is the support contact? Answer in one sentence."
# NOTE: no -a/--ask-for-approval here -- that flag exists on the top-level
# interactive `codex` command only. `codex exec --help` (codex-cli 0.154.0)
# has no -a/--ask-for-approval entry at all: exec is non-interactive by
# design, so --sandbox read-only alone is what bounds what the model can do,
# with no approval prompt possible to configure or need bypassing. Verified
# offline (2026-09-12) by grepping every flag below against a real
# `docker run --rm memriver-e2e codex exec --help`, not assumed from the
# top-level `codex --help` text.
COMMON_EXEC_FLAGS=(--skip-git-repo-check --ephemeral --sandbox read-only)

# --- criterion 0: an UNTRUSTED hook does not run -----------------------------
# No --dangerously-bypass-hook-trust here on purpose: this is the fresh
# container's first-ever `codex exec` call, so the two memriver hooks have no
# persisted trust yet. Expect the recall to fail (no Krakenfeld) and/or a
# hook-review warning on stderr -- that is a PASS for this criterion, not a
# script failure, since it's exactly the documented untrusted-hook behavior.
echo "==> criterion 0 (probe, no bypass flag): confirm an untrusted hook does not run"
run_codex_exec_json "${COMMON_EXEC_FLAGS[@]}" "$RECALL_PROMPT"
echo "----- probe: codex exit=$CODEX_EXIT_CODE num_turns=$NUM_TURNS had_error=$HAD_ERROR -----"
echo "----- probe: aggregate assistant text -----"
printf '%s\n' "$ASSISTANT_AGGREGATE"
echo "----- probe: stderr -----"
printf '%s\n' "$CODEX_STDERR"
echo "--------------------------------------------"
if printf '%s' "$ASSISTANT_AGGREGATE" | grep -qi "Krakenfeld\|Ombudsman"; then
    echo "NOTE: the untrusted-hook probe answered correctly anyway -- this Codex"
    echo "      version may auto-trust user-level ~/.codex/hooks.json, or trust"
    echo "      state carried over unexpectedly. Not a failure; recorded as a"
    echo "      finding, see e2e/README.md."
else
    pass "criterion 0: untrusted hook did not inject the marker (expected -- confirms the documented trust gate)"
fi

# --- criterion 1 & 2: the real run, trust bypassed for this one-off ----------
echo "==> criterion 1/2 (real run, --dangerously-bypass-hook-trust): SessionStart injection + Stop nudge"
run_codex_exec_json --dangerously-bypass-hook-trust "${COMMON_EXEC_FLAGS[@]}" "$RECALL_PROMPT"

echo "----- real run: aggregate of every agent_message across all turns -----"
printf '%s\n' "$ASSISTANT_AGGREGATE"
echo "-------------------------------------------------------------------------"
echo "codex exit code: $CODEX_EXIT_CODE"
echo "num_turns (turn.completed/turn.failed events): $NUM_TURNS"
echo "had_error: $HAD_ERROR"
echo "----- stderr -----"
printf '%s\n' "$CODEX_STDERR"
echo "-------------------"
echo "----- -o/--output-last-message file contents -----"
printf '%s\n' "$CODEX_LAST_MESSAGE"
echo "----------------------------------------------------"

if [ "$CODEX_EXIT_CODE" -ne 0 ]; then
    echo "FAIL: codex exec exited $CODEX_EXIT_CODE (see stderr above)" >&2
    exit 1
fi

echo "==> criterion 1: SessionStart injection reaches the model"
if printf '%s' "$ASSISTANT_AGGREGATE" | grep -q "Krakenfeld"; then
    pass "criterion 1: model's answer names 'Krakenfeld' -- the injected index line, not training data, is the only place that word comes from"
elif printf '%s' "$ASSISTANT_AGGREGATE" | grep -qi "Ombudsman"; then
    pass "criterion 1 (secondary match): model's answer names 'Ombudsman'"
else
    echo "FAIL: neither 'Krakenfeld' nor 'Ombudsman' found in any turn's agent_message" >&2
    exit 1
fi

echo "==> criterion 2: Stop nudge observability (recorded, not hard-asserted to a fixed count)"
# Codex's Stop event carries stop_hook_active the same way Claude Code's does
# (developers.openai.com/codex/hooks, "Stop" section), and memriver's encoder
# is the same decision/reason shape for both harnesses (see hooks.py), so the
# same one-continuation nudge is expected: num_turns=2 (answer, then exactly
# one Stop-nudge continuation). Recorded rather than hard-failed on a mismatch
# -- this is the first live run of this exact path, and Codex's turn-boundary
# event counting has no direct precedent in stages 1-3 to cross-check against.
if [ "$NUM_TURNS" = "2" ]; then
    pass "criterion 2: exactly 2 turns observed (the answer, then one Stop-nudge continuation) -- matches stage2's claude -p result"
else
    echo "NOTE: observed num_turns=$NUM_TURNS (expected 2 by analogy with stage2). Recorded as a finding, not a failure -- see e2e/README.md for what to check if this recurs."
fi

echo
echo "=== STAGE 5: CHECKS COMPLETE ==="
