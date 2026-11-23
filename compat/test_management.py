"""The commands that change memories: one script on two copies of a store, then each
implementation on the store the other one wrote."""

from __future__ import annotations

import re
from pathlib import Path

from .harness import ID, PY, RS, Case, Completed, run, seed_store, snapshot


def _memory(case: Case, description: str) -> dict:
    return next(row for row in snapshot(case.py.root).tables["memories"]
                if row["description"].startswith(description))


def _found(pattern: str, done: Completed) -> str:
    return re.search(pattern, done.stdout.decode())[1]


class Script:
    """Steps on both sides; each is compared as it is made."""

    def __init__(self, tmp_path: Path) -> None:
        self.case = Case(tmp_path, seed=seed_store(tmp_path))
        self.proj = self.case.work / "proj"

    def step(self, args, kind: str, *, code: int, cwd: Path | None = None,
             stdin: str = "") -> tuple[Completed, Completed]:
        """`args` is a list, or a function of the side for what each side names itself."""
        def action(side):
            return side.run(args(side) if callable(args) else args, cwd=cwd or self.proj,
                            stdin=stdin)
        py, rs = self.case.step(action)
        self.case.assert_same_output(py, rs, kind)
        assert (py.code, rs.code, py.stderr, rs.stderr) == (code, code, b"", b""), (args, py, rs)
        return py, rs

    def own(self, results: tuple[Completed, Completed], pattern: str) -> dict:
        """What each side printed at `pattern`, by side."""
        return {id(side): _found(pattern, done)
                for side, done in zip((self.case.py, self.case.rs), results)}


def test_the_same_script_leaves_the_same_store_and_each_reads_the_others(tmp_path):
    script = Script(tmp_path)
    case = script.case
    note, deploy, derived, who = (_memory(case, text) for text in (
        "note 1 about", "deploy script location", "derived from", "who the user is"))
    n, d, g = note["id"], deploy["id"], who["id"]
    made_derived = next(step["change_id"] for step in snapshot(case.py.root).tables["change_steps"]
                        if step["memory_id"] == derived["id"])

    # 1. a second version, by the one way there is to write one
    case.step(lambda side: side.api(
        f"services.memory.apply([Update({n!r}, 1, body='rewritten: the train is late')], "
        "changed_by='compat', changed_via='script')"))
    # 2. restore, 3. the same again
    restored = script.step(["restore", n, "--to", "1", "--yes"], "restore", code=0)
    assert b"  changes: content\n" in restored[0].stdout
    script.step(["restore", n, "--to", "1", "--yes"], "restore", code=2)
    script.step(["restore", n, "--to", "7", "--yes"], "restore", code=2)
    # 4. undo of the restore, 5. undo of that undo, 6. the first undo again
    restore_change = script.own(restored, rf"\(change ({ID})\)")
    undone = script.step(lambda side: ["undo", restore_change[id(side)], "--yes"], "undo", code=0)
    undo_change = script.own(undone, rf"by change ({ID})\n")
    again = script.step(lambda side: ["undo", undo_change[id(side)], "--yes"], "undo", code=0)
    assert b", an undo of " in again[0].stdout
    refused = script.step(lambda side: ["undo", undo_change[id(side)], "--yes"], "undo", code=2)
    assert refused[0].stdout.count(b"refused (changed): ") == 1
    # 7. without a terminal nothing is asked, and "no" at one cannot be given here
    script.step(["restore", n, "--to", "2"], "restore", code=2, stdin="y\n")
    script.step(["delete", n, "--version", "5"], "delete", code=2, stdin="yes\n")
    # 8. soft delete: the wrong version, from another project's directory, then done
    script.step(["delete", n, "--version", "2", "--yes"], "delete", code=2)
    script.step(["delete", n, "--version", "5", "--yes"], "delete", code=2, cwd=case.work / "other")
    script.step(["delete", n, "--version", "5", "--yes"], "delete", code=2, cwd=case.work / "repo")
    script.step(["delete", n, "--version", "5", "--yes"], "delete", code=0)
    script.step(["delete", n, "--version", "6", "--yes"], "delete", code=2)
    # 9. a global memory, from anywhere
    script.step(["delete", g, "--version", "1", "--yes"], "delete", code=0, cwd=case.work / "other")
    # 10. hard delete: the plan and its code, a code that is not the plan's, the code
    planned = script.step(["delete", d, "--hard", "--dry-run"], "delete", code=0)
    assert b"removes 2 memories" in planned[0].stdout and b"    because " in planned[0].stdout
    codes = script.own(planned, r"--confirm ([0-9a-f]{16})\n")
    assert codes[id(case.py)] == codes[id(case.rs)]
    script.step(["delete", d, "--hard", "--confirm", "0" * 16], "delete", code=2)
    script.step(["delete", d, "--hard"], "delete", code=2)
    purged = script.step(["delete", d, "--hard", "--confirm", codes[id(case.py)]], "delete", code=0)
    assert purged[0].stdout in (f"purged {d}, {derived['id']}\n".encode(),)
    script.step(["delete", d, "--hard", "--dry-run"], "delete", code=2)
    # 11. what a hard delete cut cannot be undone; 12. what never was cannot either
    script.step(["undo", made_derived, "--yes"], "undo", code=2)
    script.step(["undo", "zzzzzzzzzz", "--yes"], "undo", code=2)
    script.step(["undo", "no\x1b[31m such", "--yes"], "undo", code=2)
    case.assert_same_store()

    # each implementation on the store the other wrote
    def crossed(args, kind, code):
        def action(side):
            other = RS if side is case.py else PY
            return run(other, args(side) if callable(args) else args, home=case.home,
                       root=side.root, cwd=script.proj)
        on_py_store, on_rs_store = case.step(action)
        case.assert_same_output(on_py_store, on_rs_store, kind)
        assert (on_py_store.code, on_rs_store.code) == (code, code), (on_py_store, on_rs_store)
        return on_py_store, on_rs_store
    listed = crossed(["history", n], "history", 0)
    assert listed[0].stdout.count(b"\n") == 6
    crossed(["history", n, "--show", "6"], "history-version", 0)
    crossed(["list"], "list", 0)
    last = {id(side): _found(rf"\nv6  .*  change ({ID})  \[deleted\]", done)
            for side, done in zip((case.py, case.rs), listed)}
    crossed(lambda side: ["undo", last[id(side)], "--yes"], "undo", 0)
    crossed(["show", n], "show", 0)
    crossed(["delete", n, "--hard", "--dry-run"], "delete", 0)
    case.assert_same_store()


def test_project_commands_write_the_same_bindings(tmp_path):
    script = Script(tmp_path)
    case = script.case
    work = case.work
    (work / "new" / "child" / ".git").mkdir(parents=True)
    (work / "new" / "plain").mkdir()
    demo = next(row["id"] for row in snapshot(case.py.root).tables["projects"]
                if row["name"] == "demo proj")
    where = {"cwd": work}
    # a parent of bound directories: they stay independent. Before anything else is
    # bound below it: the list is in the order of the ids, and only the seed's are equal
    parent = script.step(["project", "init", str(work), "--yes"], "project-init", code=0, **where)
    assert b"These registered sub-projects stay independent:\n" in parent[0].stdout
    script.step(["project", "init", str(work / "new")], "project-init", code=2, **where)
    made = script.step(["project", "init", "new/", "--name", "  a  new\tone ", "--yes"],
                       "project-init", code=0, **where)
    assert b"marker: 1\n" in made[0].stdout and b"created project a new one [" in made[0].stdout
    script.step(["project", "init", str(work / "new"), "--yes"], "project-init", code=2, **where)
    script.step(["project", "init", str(work / "new" / "plain"), "--name", " ", "--yes"],
                "project-init", code=2, **where)
    script.step(["project", "init", str(work / "new" / "plain"), "--name", "x" * 121, "--yes"],
                "project-init", code=2, **where)
    for target in ("/", str(case.home), str(work / "nowhere"), str(tmp_path)):
        script.step(["project", "init", target, "--yes"], "project-init", code=2, **where)
    # adopt: refusals first
    script.step(["project", "adopt", "zzzzzzzzzz", str(work / "other"), "--yes"], "plain",
                code=2, **where)
    script.step(["project", "adopt", "bad\nid", "--yes"], "plain", code=2, **where)
    script.step(["project", "adopt", demo, str(work / "other"), "--yes"], "plain", code=2, **where)
    script.step(["project", "adopt", demo, str(work / "proj"), "--yes"], "plain", code=0, **where)
    # unbind, then adopt the directory again and another one
    script.step(["project", "unbind", demo, str(work / "other"), "--yes"], "plain", code=2, **where)
    script.step(["project", "unbind", "zzzzzzzzzz", str(work / "proj"), "--yes"], "plain",
                code=2, **where)
    script.step(["project", "unbind", demo, "proj"], "project-unbind", code=2, **where)
    unbound = script.step(["project", "unbind", demo, str(work / "proj"), "--yes"], "project-unbind", code=0,
                          cwd=work / "proj" / "sub")
    assert b"the current directory resolves afterwards: registered " in unbound[0].stdout
    script.step(["project", "adopt", demo, str(work / "other"), "--yes"], "plain", code=0, **where)
    for directory in (work, work / "proj", work / "other", work / "new" / "child"):
        script.step(["project", "explain"], "project-explain", code=0, cwd=directory)
    script.step(["list"], "list", code=0, **where)
    case.assert_same_store()
