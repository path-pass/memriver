"""Which changed paths make CI run the Rust jobs and the cross-implementation job."""

from __future__ import annotations

import pytest

from .harness import REPO, run

SCRIPT = ["bash", str(REPO / "scripts" / "rust-paths-changed.sh"), "-"]
RULES = "packages/memriver-core/src/memriver_core/content_policy/rules/"


@pytest.mark.parametrize(("path", "rust", "compat"), [
    ("crates/memriver/src/main.rs", True, True),
    ("compat/harness.py", True, True),
    ("Cargo.lock", True, True),
    ("Cargo.toml", True, True),
    ("rust-toolchain.toml", True, True),
    ("scripts/cargo-env.sh", True, True),
    (".github/workflows/pr-checks.yml", True, True),
    (RULES + "gitleaks.toml", True, True),
    (RULES + "memriver.toml", True, True),
    # the Python side is what the suite compares against and what its ledger lists
    ("packages/memriver/src/memriver/cli.py", False, True),
    ("packages/memriver-core/tests/test_models.py", False, True),
    ("tools/test_source_text.py", False, True),
    ("pyproject.toml", False, True),
    ("uv.lock", False, True),
    ("README.md", False, False),
    ("docs/Cargo.md", False, False),
    ("e2e/stage1.sh", False, False),
    (".github/workflows/e2e-foundry.yml", False, False),
])
def test_a_changed_path_starts_the_jobs_that_depend_on_it(tmp_path, path, rust, compat):
    done = run(SCRIPT, [], home=tmp_path / "home", root=tmp_path / "store", cwd=tmp_path,
               stdin=f"README.md\n{path}\n")
    assert (done.stderr, done.code) == (b"", 0)
    assert done.stdout.decode() == f"rust={str(rust).lower()}\ncompat={str(compat).lower()}\n"


def test_without_a_usable_base_both_run(tmp_path):
    where = {"home": tmp_path / "home", "root": tmp_path / "store", "cwd": tmp_path}
    done = run(SCRIPT[:2], ["0000000000000000000000000000000000000000"], **where)
    assert done.stdout == b"rust=true\ncompat=true\n"
