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
rust_paths='^(crates/|compat/|Cargo\.|rust-toolchain\.toml$|scripts/|\.github/workflows/pr-checks\.yml$|packages/memriver-core/src/memriver_core/content_policy/rules/(memriver|gitleaks)\.toml$)'
python_paths='^(packages/|tools/|pyproject\.toml$|uv\.lock$)'

# The paths, each ended by a NUL, as git stores them: never quoted, and a path is one
# path whatever bytes its name holds. A renamed file counts under its old name and its
# new one (--no-renames: a deletion and an addition).
list="$(mktemp)"
trap 'rm -f "$list"' EXIT
if [ "$1" = "-" ]; then
  tr '\n' '\0' >"$list"
elif ! git diff --no-renames --name-only -z "$1" HEAD >"$list" 2>/dev/null; then
  printf 'rust=true\ncompat=true\n'
  exit 0
fi
rust=false
compat=false
while IFS= read -r -d '' path; do
  # a whole path is matched: ^ and $ are its two ends, wherever a line break stands
  if [[ $path =~ $rust_paths ]]; then
    rust=true
    compat=true
  elif [[ $path =~ $python_paths ]]; then
    compat=true
  fi
done <"$list"
printf 'rust=%s\ncompat=%s\n' "$rust" "$compat"
