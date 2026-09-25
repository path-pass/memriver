#!/usr/bin/env bash
# Host driver for stage 2. Run deliberately --
# it needs a real CLAUDE_CODE_OAUTH_TOKEN and makes real `claude -p`
# calls (uses quota/spend).
#
# Credential flow (the token is never written to disk, this script, the
# image, or a shell history entry with its value):
#   claude setup-token                        # on the host, once
#   read -s CLAUDE_CODE_OAUTH_TOKEN           # paste it; not echoed to the terminal
#   export CLAUDE_CODE_OAUTH_TOKEN
#   bash e2e/run-stage2.sh
#   unset CLAUDE_CODE_OAUTH_TOKEN             # when done
set -euo pipefail

if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    echo "refusing to start: CLAUDE_CODE_OAUTH_TOKEN is not set in the host environment." >&2
    echo "run: claude setup-token   # then: read -s CLAUDE_CODE_OAUTH_TOKEN && export CLAUDE_CODE_OAUTH_TOKEN" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> building wheels (memriver, memriver-core) into e2e/wheels"
rm -rf e2e/wheels
uv build --all-packages --out-dir e2e/wheels

echo "==> building memriver-e2e image"
docker build -t memriver-e2e e2e/

echo "==> running stage2.sh inside the container"
echo "    (CLAUDE_CODE_OAUTH_TOKEN passed to docker by name only -- never in argv or logs)"
docker run --rm \
    -v "$REPO_ROOT/e2e/wheels":/wheels:ro \
    -v "$REPO_ROOT/e2e":/e2e:ro \
    -e CLAUDE_CODE_OAUTH_TOKEN \
    memriver-e2e bash /e2e/stage2.sh

echo "==> stage 2 finished"
