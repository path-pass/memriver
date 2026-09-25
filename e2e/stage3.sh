#!/usr/bin/env bash
# Stage 3: SessionStart source=resume -- proves that RESUMING a session (not
# only starting one fresh) re-runs SessionStart and re-injects the *current*
# memory index, rather than the model just replaying whatever was already in
# session 1's transcript.
#
# It also checks the session registry across the resume: same session id, the
# same row (still registered to the project), its second prompt counted, and no
# Stop nudge yet (that waits for 5 unsaved prompts).
#
# Same caveat as stage2.sh: real Claude Code calls on Azure AI Foundry, billed
# per token. Run only via run-stage3.sh.
#
# Design (why two markers, seeded at two different times):
#   Both markers are GLOBAL memories, hand-seeded as store rows (see
#   common.sh's seed_global_memory); both sessions run in the registered
#   project $E2E_PROJECT_DIR, and --resume is issued from the same directory.
#
#   Marker A (the Quibble axolotl) is seeded by
#   run_shared_setup, before session 1 starts -- identical to stage2. Session
#   1 proves ordinary SessionStart(source=startup) injection, same as stage2.
#
#   Marker B (the Teal Capybara Zephyr codename) is
#   seeded AFTER session 1 has already ended. Session 1's transcript cannot
#   contain marker B by any means -- it did not exist while session 1 ran.
#   If `claude -p --resume` still answers correctly about marker B, the only
#   path for that fact to reach the model is a fresh SessionStart(source=
#   resume) hook firing on the resume and re-injecting the now-updated index
#   -- not stale context carried over from session 1's transcript. That's
#   what makes this discriminating rather than a restated stage 2.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"

use_foundry_for_claude

run_shared_setup

cd "$E2E_PROJECT_DIR"

echo "==> session 1: ask the mascot question (marker A, seeded by run_shared_setup), capture session_id"
run_claude_stream_json -p "According to your memory, what is the project mascot? Answer in one sentence."

FIRST_SESSION_ID="$SESSION_ID"
echo "----- session 1 aggregate -----"
printf '%s\n' "$ASSISTANT_AGGREGATE"
echo "-----------------------------------------------------"
echo "session_id from session 1's result event: $SESSION_ID"

if [ -z "$SESSION_ID" ] || [ "$SESSION_ID" = "None" ]; then
    echo "FAIL: session 1's result event carried no usable session_id" >&2
    exit 1
fi

echo "==> criterion (session 1): SessionStart(source=startup) injection reaches the model"
if printf '%s' "$ASSISTANT_AGGREGATE" | grep -q "Quibble"; then
    pass "session 1: model's answer names 'Quibble' -- the injected index, not training data, is the only place that word comes from"
elif printf '%s' "$ASSISTANT_AGGREGATE" | grep -qi "axolotl"; then
    pass "session 1 (secondary match): model's answer names 'axolotl'"
else
    echo "FAIL: neither 'Quibble' nor 'axolotl' found in session 1's assistant turns" >&2
    exit 1
fi

echo "==> seeding marker B AFTER session 1 ended -- it cannot be in session 1's transcript"
seed_global_memory "the release codename is Teal Capybara Zephyr"

echo "==> resuming session $SESSION_ID headlessly, asking about marker B"
run_claude_stream_json -p --resume "$SESSION_ID" "According to your memory, what is the release codename? Answer in one sentence."

echo "----- resumed session aggregate -----"
printf '%s\n' "$ASSISTANT_AGGREGATE"
echo "-----------------------------------------------------"
echo "num_turns reported by the resumed session's result event: $NUM_TURNS"

echo "==> criterion 1: SessionStart(source=resume) re-injection reaches the model"
if printf '%s' "$ASSISTANT_AGGREGATE" | grep -q "Zephyr"; then
    pass "criterion 1: model's answer names 'Zephyr' -- marker B was seeded after session 1 ended, so this can only come from a fresh SessionStart(source=resume) injection, never from session 1's transcript"
elif printf '%s' "$ASSISTANT_AGGREGATE" | grep -q "Teal Capybara"; then
    pass "criterion 1 (secondary match): model's answer names 'Teal Capybara'"
else
    echo "FAIL: neither 'Zephyr' nor 'Teal Capybara' found in the resumed session's assistant turns" >&2
    exit 1
fi

echo "==> criterion 2: the resume kept the session's registration and counted its prompt"
if [ "$SESSION_ID" != "$FIRST_SESSION_ID" ]; then
    echo "FAIL: --resume changed the session id: $FIRST_SESSION_ID -> $SESSION_ID" >&2
    exit 1
fi
eval "$(read_session_row claude-code "$SESSION_ID")"
if [ "$SESSION_STATUS" != "registered" ] || [ "$SESSION_PROJECT" != "$E2E_PROJECT_ID" ] || [ "$SESSION_PROMPTS" != "2" ]; then
    echo "FAIL: session row ($SESSION_STATUS, $SESSION_PROJECT, $SESSION_PROMPTS prompts); expected (registered, $E2E_PROJECT_ID, 2)" >&2
    exit 1
fi
if [ "$NUM_TURNS" != "1" ]; then
    echo "FAIL: two unsaved prompts must not be nudged; the resumed session reports num_turns=$NUM_TURNS" >&2
    exit 1
fi
pass "criterion 2: same session id after --resume, still registered to e2e-project with 2 prompts, and no Stop nudge (num_turns=1: the nudge waits for 5 unsaved prompts)"

echo
echo "=== STAGE 3: CHECKS COMPLETE ==="
