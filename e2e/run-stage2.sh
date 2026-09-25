#!/usr/bin/env bash
# Host driver for stage 2. Run deliberately: it makes real `claude -p` calls
# against an Azure AI Foundry deployment, billed per token.
#
# Credentials come from the repository's git-ignored .env (see foundry-env.sh)
# or the host environment, and are passed to docker by name only:
#   bash e2e/run-stage2.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
# shellcheck source=foundry-env.sh
. "$REPO_ROOT/e2e/foundry-env.sh"
load_foundry_env AZURE_FOUNDRY_BASEURL AZURE_FOUNDRY_API_KEY AZURE_FOUNDRY_CLAUDE_DEPLOYMENT

echo "==> building wheels (memriver, memriver-core) into e2e/wheels"
rm -rf e2e/wheels
uv build --all-packages --out-dir e2e/wheels

echo "==> building memriver-e2e image"
docker build -t memriver-e2e e2e/

echo "==> running stage2.sh inside the container (Claude Code on Azure AI Foundry; credentials passed by name only)"
docker run --rm \
    -v "$REPO_ROOT/e2e/wheels":/wheels:ro \
    -v "$REPO_ROOT/e2e":/e2e:ro \
    -e AZURE_FOUNDRY_BASEURL \
    -e AZURE_FOUNDRY_API_KEY \
    -e AZURE_FOUNDRY_CLAUDE_DEPLOYMENT \
    memriver-e2e bash /e2e/stage2.sh

echo "==> stage 2 finished"
