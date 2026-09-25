#!/usr/bin/env bash
# Host driver for stage 5 (Codex). Run deliberately -- it
# needs a real ~/.codex/auth.json and makes real
# `codex exec` calls (spends the ChatGPT-plan quota tied to that login, not a
# separate API key -- see e2e/README.md).
#
# Credential flow (the auth file is never baked into the image and never
# printed by this script or stage5.sh):
#   codex login                                    # on the host, once, if not already
#   bash e2e/run-stage5.sh
#
# ~/.codex/auth.json is bind-mounted read-only into the container at
# /host-auth.json; stage5.sh copies it (mode 600) into the container's own
# throwaway ~/.codex so token refresh during the run has somewhere writable to
# land, without ever touching the host file.
set -euo pipefail

HOST_AUTH="$HOME/.codex/auth.json"
if [ ! -f "$HOST_AUTH" ]; then
    echo "refusing to start: $HOST_AUTH not found." >&2
    echo "run: codex login   # on the host, once" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> building wheels (memriver, memriver-core) into e2e/wheels"
rm -rf e2e/wheels
uv build --all-packages --out-dir e2e/wheels

echo "==> building memriver-e2e image"
docker build -t memriver-e2e e2e/

echo "==> running stage5.sh inside the container"
echo "    (host ~/.codex/auth.json mounted read-only at /host-auth.json -- never echoed)"
docker run --rm \
    -v "$REPO_ROOT/e2e/wheels":/wheels:ro \
    -v "$REPO_ROOT/e2e":/e2e:ro \
    -v "$HOST_AUTH":/host-auth.json:ro \
    memriver-e2e bash /e2e/stage5.sh

echo "==> stage 5 finished"
