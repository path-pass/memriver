#!/usr/bin/env bash
# Says which CI jobs the changed files call for, as two lines: rust=true|false and
# compat=true|false.
#   rust-paths-changed.sh BASE    the files changed between BASE and HEAD
#   rust-paths-changed.sh -       file names on standard input, one per line
# Without a usable BASE both are true: run rather than skip.
#
# rust: the workspace, the cross-implementation suite, CI itself, and the rule files
# both implementations embed. compat: those, and the Python side -- the suite compares
# against it and its ledger lists every Python test function.
rust='^(crates/|compat/|Cargo\.|rust-toolchain\.toml$|scripts/|\.github/workflows/pr-checks\.yml$|packages/memriver-core/src/memriver_core/content_policy/rules/(memriver|gitleaks)\.toml$)'
python='^(packages/|tools/|pyproject\.toml$|uv\.lock$)'

# Paths as git stores them (-z: never quoted), one per line; a renamed file counts under
# its old name and its new one (--no-renames: a deletion and an addition).
set -o pipefail
if [ "$1" = "-" ]; then
  files="$(cat)"
elif ! files="$(git diff --no-renames --name-only -z "$1" HEAD 2>/dev/null | tr '\0' '\n')"; then
  printf 'rust=true\ncompat=true\n'
  exit 0
fi
if grep -qE "$rust" <<<"$files"; then
  printf 'rust=true\ncompat=true\n'
elif grep -qE "$python" <<<"$files"; then
  printf 'rust=false\ncompat=true\n'
else
  printf 'rust=false\ncompat=false\n'
fi
