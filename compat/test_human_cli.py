"""The commands that only read: both implementations print the same over one store."""

from __future__ import annotations

import sqlite3
import stat
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from .differences import MASKS
from .harness import PY_API, Case, _checked, run, seed_store, snapshot

# sessions on top of the seed, written by the Python implementation: registered with
# prompts, ended and summarised; pending with a candidate (a linked worktree); without a
# project; moved by /cd; one whose id needs quoting in a shell
SESSIONS = """
from memriver_core.models import SessionKey
proj, repo, other = str(WORK / "proj"), str(WORK / "repo-linked"), str(WORK / "other")
s = services.session
a = SessionKey("codex", "sess-a")
s.start_session(a, source="startup", entry_dir=proj, transcript_path=None)
s.observe_prompt(a, prompt="first task: リリース 🚀 release train", entry_dir=proj, transcript_path=None)
s.observe_prompt(a, prompt="second\\ttask\\nwith a line break", entry_dir=proj, transcript_path=None)
s.observe_prompt(a, prompt="password = hunter2hunter2hunter2", entry_dir=proj, transcript_path=None)
stored = next(x for x in s.list_sessions() if x.key == a)
s.publish_summary(a, "Fixed the build.\\nThen the docs: é.", expected_last_active_at=stored.last_active_at)
s.end_session(a)
b = SessionKey("claude-code", "it's-b")
s.start_session(b, source="resume", entry_dir=repo, transcript_path=None)
c = SessionKey("claude-code", "sess-c")
s.start_session(c, source="startup", entry_dir=other, transcript_path=None)
s.observe_prompt(c, prompt="release notes", entry_dir=other, transcript_path=None)
d = SessionKey("claude-code", "moved-1")
s.start_session(d, source="startup", entry_dir=proj, transcript_path="/t/a/moved-1.jsonl")
s.observe_prompt(d, prompt="go", entry_dir=proj, transcript_path="/t/b/moved-1.jsonl",
                 current_dir=other)
"""


def _earlier(stamp: str) -> str:
    form = "%Y-%m-%dT%H:%M:%S.%fZ"
    then = datetime.strptime(stamp, form).replace(tzinfo=UTC)
    return (then - timedelta(minutes=90)).strftime(form)


def seeded(tmp_path: Path, *, sessions: bool = False) -> Case:
    seed = seed_store(tmp_path)
    if sessions:
        _checked(run(PY_API, [SESSIONS], home=tmp_path / "home", root=seed, cwd=tmp_path / "work"))
        # ninety minutes old: "1 hour ago" on both sides however long the case takes
        with closing(sqlite3.connect(seed / "memriver.db")) as conn, conn:
            for table, columns in (("sessions", ("started_at", "last_active_at", "ended_at",
                                                 "summary_at")), ("session_moves", ("moved_at",))):
                for column in columns:
                    stamps = conn.execute(f"SELECT DISTINCT {column} FROM {table} "
                                          f"WHERE {column} IS NOT NULL").fetchall()
                    for (stamp,) in stamps:
                        conn.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                                     (_earlier(stamp), stamp))
    return Case(tmp_path, seed=seed)


def same(case: Case, args: list[str], kind: str = "plain", *, cwd: Path | None = None,
         code: int = 0, stdin: str = "") -> str:
    """Runs `args` on both sides, compares what they print and returns Python's stdout."""
    py, rs = case.step(lambda side: side.run(args, cwd=cwd, stdin=stdin))
    case.assert_same_output(py, rs, kind)
    assert (py.code, rs.code) == (code, code), (args, py, rs)
    assert py.stderr == b"", (args, py.stderr)
    return py.stdout.decode()


def memory(case: Case, description: str) -> dict:
    return next(row for row in snapshot(case.py.root).tables["memories"]
                if row["description"].startswith(description))


def project(case: Case, name: str) -> str:
    return next(row["id"] for row in snapshot(case.py.root).tables["projects"]
                if row["name"] == name)


def test_list_shows_every_project_and_one(tmp_path):
    case = seeded(tmp_path)
    out = same(case, ["list"], "list")
    assert out.count("\n") == 3 + 5 + 31 + 1 and "  (global)\n" in out
    demo = project(case, "demo proj")
    assert same(case, ["list", "--project", demo], "list").count("\n") == 32
    assert "(no memories)" in same(case, ["list", "--project", project(case, "repo with history")],
                                   "list")
    for unknown in ("zzzzzzzzzz", "no\x1b[31mne", "x" * 300, ""):
        out = same(case, ["list", "--project", unknown], "list", code=2)
        assert out.startswith("no such project: ") and len(out) <= len("no such project: \n") + 255
    case.assert_same_store()


def test_show_prints_a_memory_and_a_deleted_one_only_when_asked(tmp_path):
    case = seeded(tmp_path)
    for description in ("deploy script location", "日本語のメモ", "emoji", "always answer",
                        "derived from"):
        out = same(case, ["show", memory(case, description)["id"]], "show")
        assert "\n---\n" in out and "last_read_at: never\n" in out
    plain = memory(case, "note 3 about")
    same(case, ["delete", plain["id"], "--version", "1", "--yes"], "delete",
         cwd=case.work / "proj")
    assert same(case, ["show", plain["id"]], "show", code=2) == f"no such memory: {plain['id']}\n"
    assert "\ndeleted: " in same(case, ["show", plain["id"], "--deleted"], "show")
    for unknown in ("zzzzzzzzzz", "bad\nid", "y" * 256):
        same(case, ["show", unknown], "show", code=2)
    case.assert_same_store()


@pytest.mark.parametrize("query", [
    "release train", "deploy", "リリース", "ＡＢＣ", "全角スペース", "部署", "strasse", "file",
    "🚀", "", "   ", "nothing-matches-this", "DEPLOY script", "release deploy note 7"])
def test_search_ranks_the_same(tmp_path, query):
    case = seeded(tmp_path)
    everywhere = same(case, ["search", query], "search")
    assert everywhere == "(no matches)\n" or everywhere.startswith("  ")
    demo = project(case, "demo proj")
    limited = same(case, ["search", query, "--limit", "2"], "search")
    assert limited.count("\n") <= 2
    same(case, ["search", query, "--project", demo, "--limit", "3"], "search")
    same(case, ["search", query, "--project", "zzzzzzzzzz"], "search", code=2)
    case.assert_same_store()


def _tree(directory: Path) -> dict[str, tuple[int, bytes | None]]:
    return {str(path.relative_to(directory)):
            (stat.S_IMODE(path.lstat().st_mode), path.read_bytes() if path.is_file() else None)
            for path in sorted(directory.rglob("*"))}


def test_export_writes_the_same_tree(tmp_path):
    case = seeded(tmp_path)
    deleted = memory(case, "note 5 about")
    same(case, ["delete", deleted["id"], "--version", "1", "--yes"], "delete",
         cwd=case.work / "proj")
    py, rs = case.step(lambda side: side.run(["export", str(side.root / "snapshot")]))
    case.assert_same_output(py, rs, "export")
    assert py.stdout == b"exported 35 memories to <store>/snapshot\n".replace(
        b"<store>", str(case.py.root).encode()) and py.code == 0
    trees = [_tree(side.root / "snapshot") for side in (case.py, case.rs)]
    assert trees[0] == trees[1] and len(trees[0]) == 1 + 2 + 35
    assert all(mode == (0o600 if data is not None else 0o700) for mode, data in trees[0].values())
    for side in (case.py, case.rs):
        assert stat.S_IMODE((side.root / "snapshot").stat().st_mode) == 0o700
    # a second run finds the directory there, and so does one onto a dangling link
    py, rs = case.step(lambda side: side.run(["export", str(side.root / "snapshot")]))
    case.assert_same_output(py, rs, "export")
    assert py.code == 2 and py.stdout.endswith(b"already exists\n")
    for side in (case.py, case.rs):
        (side.root / "dangling").symlink_to(side.root / "nowhere")
    py, rs = case.step(lambda side: side.run(["export", str(side.root / "dangling")]))
    case.assert_same_output(py, rs, "export")
    assert py.code == 2
    py, rs = case.step(lambda side: side.run(["export", str(side.root / "no" / "parent")]))
    case.assert_same_output(py, rs, "export")
    assert py.code == 2 and b"could not be created (No such file or directory)" in py.stdout
    # a relative directory is made from the working directory
    for side in (case.py, case.rs):
        (side.root / "cwd").mkdir()
    py, rs = case.step(lambda side: side.run(["export", "out/"], cwd=side.root / "cwd"))
    case.assert_same_output(py, rs, "export")
    assert py.code == 0
    assert _tree(case.py.root / "cwd" / "out") == _tree(case.rs.root / "cwd" / "out") == trees[0]
    case.assert_same_store()


def test_sessions_as_text_and_as_json(tmp_path):
    case = seeded(tmp_path, sessions=True)
    text = same(case, ["sessions"], "sessions")
    assert text.count("  resume: ") == 4 and "  moved: " in text and "(1 hour ago)" in text
    assert "claude --resume 'it'\"'\"'s-b'" in text and "(omitted: secret)" in text
    assert "  project: pending -> repo with history (" in text and "  project: (unbound)" in text
    listed = same(case, ["sessions", "--json"], "sessions-json")
    assert listed.startswith("[\n  {\n    \"harness\": ") and "\\u30ea" in listed
    demo = project(case, "demo proj")
    for args in (["release"], ["release", "--limit", "1"], ["--project", demo],
                 ["train notes", "--project", demo], ["nothing-matches"], ["--limit", "2"]):
        same(case, ["sessions", *args], "sessions")
        same(case, ["sessions", *args, "--json"], "sessions-json")
    assert same(case, ["sessions", "nothing-matches"], "sessions") == "(no sessions)\n"
    assert same(case, ["sessions", "nothing-matches", "--json"], "sessions-json") == "[]\n"
    same(case, ["sessions", "--project", "zzzzzzzzzz"], "sessions", code=2)
    case.assert_same_store()


def test_history_lists_versions_and_shows_one(tmp_path):
    case = seeded(tmp_path)
    derived, plain = memory(case, "derived from"), memory(case, "note 9 about")
    out = same(case, ["history", derived["id"]], "history")
    assert out.count("\n") == 2 and "\n    sources: " in out
    same(case, ["history", derived["id"], "--show", "1"], "history-version")
    same(case, ["delete", plain["id"], "--version", "1", "--yes"], "delete",
         cwd=case.work / "proj")
    same(case, ["restore", plain["id"], "--to", "1", "--yes"], "restore")
    out = same(case, ["history", plain["id"]], "history")
    assert out.count("\n") == 3 and "  [deleted]  " in out and "  human  change " in out
    for version in ("1", "2", "3"):
        same(case, ["history", plain["id"], "--show", version], "history-version")
    for version in ("4", "99999999999999999999999"):
        assert same(case, ["history", plain["id"], "--show", version], "history-version",
                    code=2) == f"no such version: {plain['id']} v{version}\n"
    same(case, ["history", "zzzzzzzzzz"], "history", code=2)
    same(case, ["history", "zzzzzzzzzz", "--show", "1"], "history", code=2)
    # a version nothing recorded a change for
    for side in (case.py, case.rs):
        with closing(sqlite3.connect(side.root / "memriver.db")) as conn, conn:
            conn.execute("UPDATE memory_versions SET change_id = NULL WHERE memory_id = ? "
                         "AND version = 1", (plain["id"],))
    assert same(case, ["history", plain["id"]], "history").startswith("v1  imported  note 9")
    same(case, ["history", plain["id"], "--show", "1"], "history-version")
    case.assert_same_store()


def test_project_explain_in_six_directories(tmp_path):
    case = seeded(tmp_path)
    work = case.work
    states = []
    for directory in (work / "proj", work / "proj" / "sub", work / "repo" / "deep" / "er",
                      work / "repo-linked" / "deep", work / "repo-detached", work / "other"):
        out = same(case, ["project", "explain"], "project-explain", cwd=directory)
        states.append(out.splitlines()[2])
        same(case, ["project", "explain", "--project-dir", str(directory)], "project-explain")
    # a directory is explained as it lies: a linked worktree is not looked up in its
    # main tree here, that is what a session's registration does
    assert states == ["state: registered"] * 3 + ["state: none"] * 3
    # a directory that is not there, a relative one, and a root that was replaced
    same(case, ["project", "explain", "--project-dir", str(work / "missing")], "project-explain",
         code=1)
    same(case, ["project", "explain", "--project-dir", "proj/sub/"], "project-explain")
    (work / "proj").rename(work / "proj-moved")
    (work / "proj").symlink_to(work / "proj-moved")
    out = same(case, ["project", "explain"], "project-explain", cwd=work / "other", code=1)
    assert "state: degraded\ndiagnostic: " in out
    case.assert_same_store()


def test_a_store_that_is_not_there_is_read_as_empty_and_never_made(tmp_path):
    case = Case(tmp_path)
    for args, kind in ((["list"], "list"), (["search", "x"], "search"), (["sessions"], "sessions"),
                       (["sessions", "--json"], "sessions-json"),
                       (["project", "explain"], "project-explain")):
        same(case, args, kind)
    for args, kind in ((["show", "zzzzzzzzzz"], "show"), (["history", "zzzzzzzzzz"], "history"),
                       (["undo", "zzzzzzzzzz", "--yes"], "undo"),
                       (["restore", "zzzzzzzzzz", "--to", "1", "--yes"], "restore"),
                       (["delete", "zzzzzzzzzz", "--version", "1", "--yes"], "delete"),
                       (["delete", "zzzzzzzzzz", "--hard", "--dry-run"], "delete")):
        same(case, args, kind, code=2)
    assert not case.py.root.exists() and not case.rs.root.exists()


def test_a_broken_settings_file_or_variable_stops_a_command_with_one_line(tmp_path):
    case = seeded(tmp_path)
    for text in ("max_body_chars = true\n", "[broken\n", "search_limit_default = 0\ntags_max = -1\n"):
        for side in (case.py, case.rs):
            (side.root / "settings.toml").write_text(text)
        for args in (["list"], ["show", "zzzzzzzzzz"], ["project", "explain"],
                     ["project", "init", str(case.work / "other"), "--yes"],
                     ["delete", "zzzzzzzzzz", "--hard", "--dry-run"]):
            py, rs = case.step(lambda side, args=args: side.run(args))
            case.assert_same_output(py, rs)
            assert (py.stdout, py.code) == (b"", 1) and py.stderr.startswith(b"memriver: settings.toml ")
    case.assert_same_store()


@pytest.mark.parametrize("args", [
    ["delete", "zzzzzzzzzz"], ["delete", "zzzzzzzzzz", "--hard", "--version", "1"],
    ["delete", "zzzzzzzzzz", "--version", "1", "--dry-run"],
    ["delete", "zzzzzzzzzz", "--version", "1", "--confirm", "0123456789abcdef"],
    ["delete", "zzzzzzzzzz", "--hard", "--dry-run", "--confirm", "0123456789abcdef"],
    ["search"], ["search", "x", "--limit", "0"], ["search", "x", "--limit", "-1"],
    ["search", "x", "--limit", "many"], ["history", "zzzzzzzzzz", "--show", "0"],
    ["restore", "zzzzzzzzzz"], ["project"], ["project", "unbind", "zzzzzzzzzz"], ["nothing"],
    ["list", "--nothing"], ["show"], ["export"]])
def test_a_command_line_that_is_no_command_is_refused_before_anything_is_read(tmp_path, args):
    case = seeded(tmp_path)
    py, rs = case.step(lambda side: side.run(args))
    case.assert_same_output(py, rs, allow=("usage-text",))
    assert (py.stdout, py.code, rs.stdout, rs.code) == (b"", 2, b"", 2)
    case.assert_same_store()


def test_the_usage_mask_covers_a_refusal_of_the_parser_and_nothing_else():
    mask = MASKS["usage-text"]
    python = "usage: memriver delete [-h]\nmemriver delete: error: --version is required\n"
    rust = "error: --version is required without --hard\n\nUsage: memriver delete\n"
    assert mask.sub("<x>", python) == mask.sub("<x>", rust) == "<x>"
    for text in ("", "refused: the memory store could not be read\n",
                 "memriver: settings.toml could not be read\n", "usage: other\n",
                 "no such memory: error: x\n", " error: x\n", "errors: 2\n"):
        assert mask.sub("<x>", text) == text, text
