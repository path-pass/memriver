#!/usr/bin/env bash
# Stage 3: SessionStart source=resume -- proves that RESUMING a session (not
# only starting one fresh) re-runs SessionStart and re-injects the *current*
# memory index, rather than the model just replaying whatever was already in
# session 1's transcript.
#
# Same caveat as stage2.sh: this needs CLAUDE_CODE_OAUTH_TOKEN and spends
# real quota. Run only via run-stage3.sh.
#
# Design (why two markers, seeded at two different times):
#   Both markers are GLOBAL memories, hand-seeded as files (see
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

run_shared_setup

cd "$E2E_PROJECT_DIR"

echo "==> session 1: ask the mascot question (marker A, seeded by run_shared_setup), capture session_id"
run_claude_stream_json -p "According to your memory, what is the project mascot? Answer in one sentence."

SESSION_ID="$SESSION_ID"
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

echo "==> criterion 2: num_turns on resume (Stop nudge fires on resume end too; recorded, asserted <= 2)"
if [ "$NUM_TURNS" -le 2 ] 2>/dev/null; then
    pass "criterion 2: resumed session reports num_turns=$NUM_TURNS (<=2)"
else
    echo "FAIL: expected num_turns <= 2 on the resumed session, got num_turns=$NUM_TURNS" >&2
    exit 1
fi

echo
echo "=== STAGE 3: CHECKS COMPLETE ==="
