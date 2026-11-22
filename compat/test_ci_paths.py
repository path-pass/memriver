"""Which changed paths make CI run the Rust jobs and the cross-implementation job."""

from __future__ import annotations

import pytest

from .harness import GIT, REPO, run

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


def changed(tmp_path, change):
    """What the script says for one commit made by `change(repository)` in a new repository."""
    repository = tmp_path / "repository"
    where = {"home": tmp_path / "home", "root": tmp_path / "store", "cwd": tmp_path}

    def git(*args):
        done = run(GIT, ["-C", str(repository), *args], **where)
        assert done.code == 0, done.stderr
        return done.stdout.decode().strip()

    for directory in (repository / "compat" / "seeds", repository / "docs", tmp_path / "home"):
        directory.mkdir(parents=True)
    (repository / "compat" / "base.json").write_text("{}\n")
    (repository / "README.md").write_text("read me\n")
    run(GIT, ["init", "-q", "-b", "main", str(repository)], **where)
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    base = git("rev-parse", "HEAD")
    change(repository, git)
    git("add", "-A")
    git("commit", "-q", "-m", "change")
    done = run(SCRIPT[:2], [base], home=where["home"], root=where["root"], cwd=repository)
    assert (done.stderr, done.code) == (b"", 0)
    return done.stdout.decode()


def test_a_file_moved_out_of_a_watched_directory_counts(tmp_path):
    out = changed(tmp_path, lambda repository, git: git("mv", "compat/base.json", "docs/base.json"))
    assert out == "rust=true\ncompat=true\n"


def test_a_path_git_would_quote_counts(tmp_path):
    def add(repository, git):
        (repository / "compat" / "seeds" / "中文.json").write_text("{}\n")

    assert changed(tmp_path, add) == "rust=true\ncompat=true\n"


def test_a_real_commit_outside_the_watched_paths_starts_nothing(tmp_path):
    def edit(repository, git):
        (repository / "README.md").write_text("read me again\n")
        (repository / "docs" / "中文.md").write_text("notes\n")

    assert changed(tmp_path, edit) == "rust=false\ncompat=false\n"
