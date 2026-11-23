"""The five hooks: both implementations print the same and leave the same store.

Each step runs one hook on both sides, over copies of one store the Python
implementation wrote; stdout, stderr and the exit code are compared, the text a
hook hands the agent with its ids renamed (output kind `hook`), and at the end
every table -- `sessions`, `tool_calls` and `session_moves` above all -- with the
times inside the recorded prompts renamed too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .harness import GIT, Case, Completed, _checked, run, seed_store

SESSION_START = "session-start"


def hook(case: Case, event: str, harness: str, payload: object, cwd: Path, *,
         code: int = 0, extra_env: dict[str, str] | None = None) -> tuple[Completed, Completed]:
    """`memriver hook <event>` on both sides with `payload` on stdin (an object is sent as
    JSON, bytes as they are); the outputs compared, the exit code `code` on both."""
    stdin = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    args = ["hook", event, "--harness", harness]
    py, rs = case.step(lambda side: side.run(args, cwd=cwd, stdin=stdin, extra_env=extra_env))
    case.assert_same_output(py, rs, "hook")
    assert (py.code, rs.code) == (code, code), (event, payload, py, rs)
    return py, rs


def additional_context(done: Completed) -> str:
    return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]


@pytest.fixture
def case(tmp_path: Path) -> Case:
    seed = seed_store(tmp_path)
    # a branch that shares its name with a tag
    where = {"home": tmp_path / "home", "root": seed, "cwd": tmp_path / "work"}
    repo = str(tmp_path / "work" / "repo")
    _checked(run(GIT, ["-C", repo, "tag", "twin"], **where))
    _checked(run(GIT, ["-C", repo, "branch", "twin"], **where))
    return Case(tmp_path, seed=seed)


def test_session_start_over_every_kind_of_entry_and_input(case: Case) -> None:
    work = case.work
    proj, other, repo = work / "proj", work / "other", work / "repo"
    linked, detached = work / "repo-linked", work / "repo-detached"
    transcript = str(case.home / ".claude/projects/-x/t.jsonl")
    cases = [
        ("claude-code", {"session_id": "s-start", "source": "startup", "cwd": str(proj),
                         "transcript_path": transcript}, proj),
        ("codex", {"session_id": "c-start", "source": "startup", "cwd": str(proj)}, proj),
        ("claude-code", {"session_id": "s-sub", "source": "startup", "cwd": str(proj / "sub")},
         proj / "sub"),
        ("claude-code", {"session_id": "s-none", "source": "startup", "cwd": str(other)}, other),
        ("claude-code", {"session_id": "s-pending", "source": "resume", "cwd": str(proj)}, proj),
        ("claude-code", {"session_id": "s-pending-none", "source": "compact", "cwd": str(other)},
         other),
        ("claude-code", {"session_id": "s-clear", "source": "clear", "cwd": str(proj)}, proj),
        ("claude-code", {"session_id": "g-main", "source": "startup", "cwd": str(repo)}, repo),
        ("codex", {"session_id": "g-deep", "source": "startup", "cwd": str(repo / "deep/er")},
         repo),
        ("claude-code", {"session_id": "g-linked", "source": "startup", "cwd": str(linked)},
         linked),
        ("claude-code", {"session_id": "g-linked-deep", "source": "resume",
                         "cwd": str(linked / "deep")}, linked),
        ("claude-code", {"session_id": "g-detached", "source": "startup", "cwd": str(detached)},
         detached),
        ("claude-code", {"session_id": "s-start", "source": "resume", "cwd": str(other),
                         "transcript_path": transcript + "2"}, other),
        ("claude-code", {"session_id": "s-start", "source": "compact", "cwd": str(proj),
                         "transcript_path": transcript + "2"}, proj),
        ("claude-code", {"session_id": "s-gone", "source": "startup",
                         "cwd": str(work / "missing")}, work),
        ("claude-code", {"source": "startup", "cwd": str(proj)}, proj),
        ("claude-code", {"session_id": "a b", "source": "startup", "cwd": str(proj)}, proj),
        ("claude-code", b"not json", proj),
        ("claude-code", b"[]", proj),
        ("claude-code", b"\xff\xfe", proj),
        ("codex", {"session_id": "s-source", "source": 7, "cwd": str(proj)}, proj),
    ]
    for harness, payload, cwd in cases:
        hook(case, SESSION_START, harness, payload, cwd)
    # the branch that shares its name with a tag
    where = {"home": case.home, "root": case.py.root, "cwd": work}
    _checked(run(GIT, ["-C", str(repo), "checkout", "-q", "twin"], **where))
    py, _ = hook(case, SESSION_START, "claude-code",
                 {"session_id": "g-twin", "source": "startup", "cwd": str(repo)}, repo)
    assert additional_context(py).startswith("[memriver] Your persistent memory index")
    for name, payload in [
        ("recorded", {"session_id": "s-start", "tool_use_id": "toolu_01", "cwd": str(proj / "sub")}),
        ("a relative cwd is dropped", {"session_id": "s-none", "tool_use_id": "toolu_02", "cwd": "rel"}),
        ("an invalid call id is ignored", {"session_id": "s-none", "tool_use_id": "bad id"}),
    ]:
        py, _ = hook(case, "pre-tool-use", "claude-code", payload, proj)
        assert (py.stdout, py.stderr) == (b"", b""), name
    case.assert_same_store()


def test_a_large_index_is_cut_to_what_each_harness_forwards(case: Case) -> None:
    # settings.toml raises the line budget, so the hook must drop whole lines to fit
    # 10,000 UTF-16 units (Claude Code) or 10,000 UTF-8 bytes (Codex)
    case.step(lambda side: side.api("""
context = services.project.open_project_context(str(WORK / "proj"))
for n in range(260):
    services.memory.record(content=f"条目 {n}：发布列车在第 {n} 天出发 🚆", type="project",
                           sync=True, harness="codex", context=context,
                           description=f"第 {n} 条关于发布列车的备忘，带一个表情 🚆 和足够长的说明文字")
"""))
    for side in (case.py, case.rs):
        (side.root / "settings.toml").write_text(
            "index_budget_lines = 400\nsearch_limit_max = 80\n[dream]\nttl_days = 3\n")
    for harness in ("claude-code", "codex"):
        payload = {"session_id": "big-" + harness, "source": "startup",
                   "cwd": str(case.work / "proj")}
        py, _ = hook(case, SESSION_START, harness, payload, case.work / "proj")
        text = additional_context(py)
        assert "more entries omitted" in text
        size = len(text.encode("utf-16-le")) // 2 if harness == "claude-code" else len(text.encode())
        assert 9_000 < size <= 10_000
    case.assert_same_store()


@pytest.mark.parametrize(("name", "text", "env"), [
    ("a boolean where a number belongs", "max_body_chars = true\n", None),
    ("a default above the max", "search_limit_default = 9\nsearch_limit_max = 3\n", None),
    ("not TOML", "= = =\n", None),
    ("a bad environment value", "", {"MEMRIVER_INDEX_BUDGET_LINES": "0"}),
    ("environment and file both bad", "tags_max = -1\n", {"MEMRIVER_MAX_BODY_CHARS": "x"}),
])
def test_a_settings_error_is_the_same_line_and_exit_1(case: Case, name: str, text: str,
                                                      env: dict[str, str] | None) -> None:
    for side in (case.py, case.rs):
        (side.root / "settings.toml").write_text(text)
    proj = case.work / "proj"
    payload = {"session_id": "bad-settings", "source": "startup", "cwd": str(proj),
               "prompt": "hi", "stop_hook_active": False, "tool_use_id": "toolu_1"}
    for event in (SESSION_START, "user-prompt-submit", "session-end"):
        py, _ = hook(case, event, "claude-code", payload, proj, code=1, extra_env=env)
        assert py.stderr.startswith(b"memriver: settings.toml") or py.stderr.startswith(
            b"memriver: environment"), py.stderr
    # Stop and PreToolUse read no setting
    for event in ("stop", "pre-tool-use"):
        hook(case, event, "claude-code", payload, proj, extra_env=env)
    case.assert_same_store()


def test_prompts_moves_stops_and_ends(case: Case) -> None:
    proj, repo, other = case.work / "proj", case.work / "repo", case.work / "other"

    def prompt(session: str, cwd: Path, text: object, transcript: str | None = None,
               harness: str = "claude-code") -> tuple[Completed, Completed]:
        payload = {"session_id": session, "prompt": text, "cwd": str(cwd)}
        if transcript is not None:
            payload["transcript_path"] = transcript
        return hook(case, "user-prompt-submit", harness, payload, cwd)

    def stop(session: str, active: object = False) -> tuple[Completed, Completed]:
        return hook(case, "stop", "claude-code",
                    {"session_id": session, "stop_hook_active": active}, proj)

    hook(case, SESSION_START, "claude-code",
         {"session_id": "m", "source": "startup", "cwd": str(proj), "transcript_path": "/t/a/m.jsonl"},
         proj)
    # plain prompts, a secret, one past the scan limit, one that is no text
    for text in ("first task: リリース 🚀", "second\ttask\nwith a line break",
                 "password = hunter2hunter2hunter2", "x" * 70_000, 17):
        py, _ = prompt("m", proj, text, "/t/a/m.jsonl")
        assert py.stdout == b""
    stop("m")                                       # due: five prompts
    stop("m")                                       # not due again
    stop("m", True)                                 # the loop guard
    stop("m", "false")
    # a /cd: the transcript's directory changed and the session moves to repo
    py, _ = prompt("m", repo, "go on", "/t/b/m.jsonl")
    assert additional_context(py).startswith("[memriver] This session moved with /cd")
    prompt("m", repo, "again", "/t/b/m.jsonl")      # nothing left to say
    prompt("m", other, "shell cd", "/t/b/m.jsonl")  # the transcript stayed: no move
    # a first prompt from a session never seen: the pending notice alone
    py, _ = prompt("unseen", proj, "carry on")
    assert additional_context(py).startswith("[memriver] This session is not registered yet.")
    prompt("unseen", proj, "carry on")
    # a pending session's /cd: the proposal changes
    hook(case, SESSION_START, "claude-code",
         {"session_id": "p", "source": "resume", "cwd": str(proj), "transcript_path": "/t/a/p.jsonl"},
         proj)
    py, _ = prompt("p", repo, "go", "/t/b/p.jsonl")
    assert b"the project proposed for it changed" in py.stdout
    # a Codex sub-agent is not counted
    hook(case, SESSION_START, "codex", {"session_id": "c", "source": "startup", "cwd": str(proj)}, proj)
    hook(case, "user-prompt-submit", "codex",
         {"session_id": "c", "prompt": "sub", "cwd": str(proj), "agent_id": "a-1"}, proj)
    prompt("c", proj, "root", harness="codex")
    for session, harness in (("m", "claude-code"), ("c", "codex"), ("nobody", "codex")):
        hook(case, "session-end", harness, {"session_id": session}, proj)
    case.assert_same_store()
