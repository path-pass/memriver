#!/usr/bin/env bash
# Prints changed=true when a commit after BASE touches what the Rust CI jobs cover,
# changed=false otherwise. Without a usable BASE it says true: run rather than skip.
#   rust-paths-changed.sh BASE
paths='^(crates/|compat/|Cargo\.|rust-toolchain\.toml$|scripts/|\.github/workflows/pr-checks\.yml$|packages/memriver-core/src/memriver_core/content_policy/rules/(memriver|gitleaks)\.toml$)'
if ! files="$(git diff --name-only "$1" HEAD 2>/dev/null)"; then
  echo "changed=true"
elif grep -qE "$paths" <<<"$files"; then
  echo "changed=true"
else
  echo "changed=false"
fi
