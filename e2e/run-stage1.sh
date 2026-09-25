#!/usr/bin/env bash
# Host driver for stage 1: build wheels from the working tree, build the
# image, run stage1.sh inside a throwaway container. Run from the repo root:
#   bash e2e/run-stage1.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> building wheels (memriver, memriver-core) into e2e/wheels"
rm -rf e2e/wheels
uv build --all-packages --out-dir e2e/wheels

echo "==> building memriver-e2e image"
docker build -t memriver-e2e e2e/

echo "==> running stage1.sh inside the container"
docker run --rm \
    -v "$REPO_ROOT/e2e/wheels":/wheels:ro \
    -v "$REPO_ROOT/e2e":/e2e:ro \
    memriver-e2e bash /e2e/stage1.sh

echo "==> stage 1 passed"
