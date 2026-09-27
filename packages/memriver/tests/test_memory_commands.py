"""`memriver history`, `restore`, `undo` and `delete` (spec §8.2; §10 item 17,
umbrella part), over real services on a tmp_path store. A fake stands in only
where a test needs one specific refusal the real store cannot be steered into
at that moment."""

from __future__ import annotations

import io
import shlex
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
from memriver import cli, memory_commands
from memriver.memory_commands import (
    run_delete,
    run_history,
    run_restore,
    run_undo,
)
from memriver_core import (
    BatchConflict,
    ContentRejected,
    MemoryNotFound,
    PlanChanged,
)
from memriver_core.bootstrap import build_services
from memriver_core.models import (
    Create,
    MemoryVersion,
    SoftDelete,
    SourceRef,
    Update,
    new_id,
)
from memriver_core.settings import Settings


@pytest.fixture
def world(tmp_path):
    store, work, home = tmp_path / "mem", tmp_path / "work", tmp_path / "home"
    work.mkdir()
    home.mkdir()
    services = build_services(Settings(root=store), root=store, home=home)
    global_id = services.project.ensure_global()
    project = services.project.init_project("demo", services.project.plan_root(str(work)))
    context = services.project.open_project_context(str(work))
    memory = services.memory.record(content="line one\nline two", type="project", sync=True,
                                    harness="t", description="the cue", context=context)
    return SimpleNamespace(store=store, work=work, home=home, services=services,
                           global_id=global_id, project=project, context=context,
                           memory=memory)


def _yes(_prompt: str) -> str:
    return "y"


def _never_called(_prompt: str) -> str:
    raise AssertionError("input_fn must not be called")


def _answers(*steps):
    """An input_fn answering "y" each time, after running the next step if one is left:
    a change another writer makes while the prompt is open."""
    pending = list(steps)

    def input_fn(_prompt: str) -> str:
        if pending:
            pending.pop(0)()
        return "y"
    return input_fn


def _history(world, memory_id, show=None):
    out = io.StringIO()
    code = run_history(memory_id, show=show, root=world.store, stdout=out, home=world.home)
    return code, out.getvalue()


def _restore(world, memory_id, to_version, *, input_fn=_yes, yes=False, tty=True):
    out = io.StringIO()
    code = run_restore(memory_id, to_version=to_version, yes=yes, root=world.store,
                       stdin_is_tty=tty, input_fn=input_fn, stdout=out, home=world.home)
    return code, out.getvalue()


def _undo(world, change_id, *, input_fn=_yes, yes=False):
    out = io.StringIO()
    code = run_undo(change_id, yes=yes, root=world.store, stdin_is_tty=True,
                    input_fn=input_fn, stdout=out, home=world.home)
    return code, out.getvalue()


def _delete(world, memory_id, *, version=None, hard=False, dry_run=False, confirm_code=None,
            yes=False, input_fn=_yes, tty=True, cwd=None):
    out = io.StringIO()
    code = run_delete(memory_id, version=version, hard=hard, dry_run=dry_run,
                      confirm_code=confirm_code, yes=yes, root=world.store, stdin_is_tty=tty,
                      input_fn=input_fn, stdout=out, cwd=cwd or world.work, home=world.home)
    return code, out.getvalue()


def _versions(world, memory_id):
    return sorted(world.services.memory.versions(memory_id), key=lambda v: v.version)


def _dream_update(world, memory_id, expected_version, body):
    return world.services.memory.apply([Update(memory_id, expected_version, body=body)],
                                       changed_by="dream", changed_via="codex")


def _global_memory(world, *sources):
    """A global memory citing the (id, version) pairs in `sources`; returns its id."""
    change = world.services.memory.apply(
        [Create(world.global_id, "project", "a shared rule", "shared body",
                sources=tuple(SourceRef(memory_id, version) for memory_id, version in sources))],
        changed_by="dream", changed_via="codex")
    return change.steps[0].memory_id


def _gone(world, memory_id) -> bool:
    try:
        world.services.memory.show(memory_id, include_deleted=True)
    except MemoryNotFound:
        return True
    return False


def _use_services(monkeypatch, world):
    """Make the commands use world.services, so a test can replace one method on it."""
    monkeypatch.setattr(memory_commands, "_services", lambda root, home: world.services)


SECRET = "aws key AKIAIOSFODNN7EXAMPLE ok"          # from the secret-scanner tests


def _plant_policy_hit(world, memory_id: str) -> None:
    """Overwrite `memory_id`'s stored text so its current state now fails the content
    policy -- the way core's test_a_soft_delete_of_a_memory_whose_text_now_hits_the_policy_is_refused
    does, one statement behind the services' backs, committed."""
    with closing(sqlite3.connect(world.store / "memriver.db")) as conn, conn:
        conn.execute("UPDATE memories SET body = ? WHERE id = ?", (SECRET, memory_id))
        conn.execute("UPDATE memory_versions SET body = ? WHERE memory_id = ?",
                     (SECRET, memory_id))


# --- history -----------------------------------------------------------------

# §10 item 17 / §8.2: number, time, changed_by (changed_via), change id, deleted flag,
# description, source ids; global included
def test_history_lists_every_version_with_its_change_and_state(world):
    memory_id = world.memory.id
    update = _dream_update(world, memory_id, 1, "line three")
    shared = _global_memory(world, (memory_id, 2))
    world.services.memory.apply([SoftDelete(memory_id, 2)], changed_by="human")

    code, out = _history(world, memory_id)
    assert code == 0
    lines = out.splitlines()
    assert [line.split("  ")[0] for line in lines] == ["v1", "v2", "v3"]
    assert "  mcp" in lines[0] and lines[0].endswith("  the cue")
    assert f"  dream (codex)  change {update.change_id}" in lines[1]
    assert "[deleted]" in lines[2] and "[deleted]" not in lines[1]
    assert "  human  change " in lines[2]

    code, out = _history(world, shared)
    assert code == 0
    assert out.splitlines()[1] == f"    sources: {memory_id} v2"


def test_history_show_prints_one_version_in_full(world):
    _dream_update(world, world.memory.id, 1, "line three")
    code, out = _history(world, world.memory.id, show=1)
    assert code == 0
    assert "version: 1\n" in out and "deleted: false\n" in out and "sources: -\n" in out
    assert out.endswith("---\nline one\nline two\n")


def test_history_refuses_an_unknown_memory_or_version(world):
    unknown = new_id()
    assert _history(world, unknown) == (2, f"no such memory: {unknown}\n")
    assert _history(world, world.memory.id, show=7) == (
        2, f"no such version: {world.memory.id} v7\n")


# spec §9: migrated rows carry no change; memriver history still labels them "imported"
def test_a_version_with_no_recorded_change_shows_imported():
    version = MemoryVersion(memory_id="mmmmmmmmmm", version=1, type="project", trust="agent",
                            sync=True, description="kept", body="b", deleted=False, sources=(),
                            change=None)
    assert memory_commands._version_line(version) == "v1  imported  kept\n"


# --- restore -----------------------------------------------------------------

# §8.2: current and target shown with what changes, y/N, then
# restore(expected_version = the version shown, changed_by="human")
def test_restore_shows_both_states_then_records_a_human_change(world):
    memory_id = world.memory.id
    world.services.memory.apply([Update(memory_id, 1, description="new cue", body="rewritten")],
                                changed_by="dream")
    code, out = _restore(world, memory_id, 1)
    assert code == 0
    assert out.startswith(f"memriver restore: {memory_id} from v2 to the state of v1\n"
                          "  now (v2): new cue\n"
                          "  target (v1): the cue\n"
                          "  changes: content\n")
    latest = _versions(world, memory_id)[-1]
    assert out.endswith(f"restored {memory_id} to the state of v1 as v3 "
                        f"(change {latest.change.change_id})\n")
    assert (latest.version, latest.description, latest.body) == (3, "the cue",
                                                                 "line one\nline two")
    assert (latest.change.changed_by, latest.change.changed_via) == ("human", None)


def test_restoring_a_live_version_undeletes(world):
    memory_id = world.memory.id
    world.services.memory.delete(memory_id, world.context, expected_version=1)
    code, out = _restore(world, memory_id, 1)
    assert code == 0 and "  changes: deleted state\n" in out
    assert world.services.memory.show(memory_id).version == 3


def test_restore_declined_or_without_a_terminal_changes_nothing(world):
    _dream_update(world, world.memory.id, 1, "line three")
    code, out = _restore(world, world.memory.id, 1, input_fn=lambda _: "n")
    assert code == 1 and out.endswith("aborted; nothing was changed\n")
    code, out = _restore(world, world.memory.id, 1, input_fn=_never_called, tty=False)
    assert code == 2 and "pass --yes to confirm non-interactively" in out
    assert len(_versions(world, world.memory.id)) == 2


# §8.2: BatchConflict(version) re-reads and asks again
def test_a_restore_conflict_rereads_and_asks_again(world):
    memory_id = world.memory.id
    _dream_update(world, memory_id, 1, "line three")
    code, out = _restore(world, memory_id, 1, input_fn=_answers(
        lambda: _dream_update(world, memory_id, 2, "line four")))
    assert code == 0
    assert f"memriver restore: {memory_id} from v2 to the state of v1\n" in out
    assert f"{memory_id} changed while waiting; nothing was changed yet\n" in out
    assert f"memriver restore: {memory_id} from v3 to the state of v1\n" in out
    assert out.endswith(f"as v4 (change {_versions(world, memory_id)[-1].change.change_id})\n")


def test_a_restore_conflict_under_yes_fails_instead_of_asking(world, monkeypatch):
    _dream_update(world, world.memory.id, 1, "line three")
    _use_services(monkeypatch, world)

    def conflict(*args, **kwargs):
        raise BatchConflict(0, world.memory.id, "version")

    monkeypatch.setattr(world.services.memory, "restore", conflict)
    code, out = _restore(world, world.memory.id, 1, yes=True, input_fn=_never_called)
    assert code == 2
    assert out.endswith(f"refused (version): {world.memory.id} changed while waiting; nothing "
                        "was changed; run the command again\n")


# §8.2: ContentRejected prints the rule id
def test_a_restore_refused_by_the_content_policy_prints_the_rule(world, monkeypatch):
    _dream_update(world, world.memory.id, 1, "line three")
    _use_services(monkeypatch, world)

    def rejected(*args, **kwargs):
        raise ContentRejected(rule_id="github-pat", memory_id=world.memory.id)

    monkeypatch.setattr(world.services.memory, "restore", rejected)
    code, out = _restore(world, world.memory.id, 1)
    assert code == 2
    assert out.endswith("refused (content policy): the state of v1 fails rule github-pat; "
                        "nothing was changed\n")


def test_restore_to_an_unknown_version_is_refused(world):
    assert _restore(world, world.memory.id, 5, input_fn=_never_called) == (
        2, f"no such version: {world.memory.id} v5\n")


# --- undo --------------------------------------------------------------------

def _merge(world):
    """Two project memories merged into one global entry, the way dream does it."""
    first = world.memory
    second = world.services.memory.record(content="another fact", type="project", sync=True,
                                          harness="t", description="second cue",
                                          context=world.context)
    change = world.services.memory.apply(
        [Create(world.global_id, "project", "merged cue", "both facts",
                sources=(SourceRef(first.id, 1), SourceRef(second.id, 1))),
         SoftDelete(first.id, 1), SoftDelete(second.id, 1)],
        changed_by="dream", changed_via="codex")
    return change, change.steps[0].memory_id, first.id, second.id


# §8.2: the change's steps with ids and projects, global flagged, and what the undo does
def test_undo_shows_the_change_and_its_inverse_then_round_trips(world):
    change, merged, first, second = _merge(world)
    project = world.project.id
    code, out = _undo(world, change.change_id)
    assert code == 0
    assert out.startswith(
        f"memriver undo: change {change.change_id} at {change.at} by dream (codex)\n"
        f"  {merged}  global  create v1  merged cue\n"
        f"  {first}  project {project}  soft_delete v1->v2  the cue\n"
        f"  {second}  project {project}  soft_delete v1->v2  second cue\n"
        "the undo:\n"
        f"  {merged}: soft delete\n"
        f"  {first}: restore the state of v1\n"
        f"  {second}: restore the state of v1\n")
    undo = world.services.memory.change(out.split()[-1])
    assert out.endswith(f"undone change {change.change_id} by change {undo.change_id}\n")
    assert (undo.undoes, undo.changed_by, undo.changed_via) == (change.change_id, "human", None)
    with pytest.raises(MemoryNotFound):
        world.services.memory.show(merged)
    assert world.services.memory.show(first).version == 3

    # an undo is a change like any other, and can itself be undone
    code, out = _undo(world, undo.change_id)
    assert code == 0
    assert out.splitlines()[0].endswith(f"by human, an undo of {change.change_id}")


# §8.2: refusals print their reason (not-found, changed with ids)
def test_undo_refusals_print_their_reason(world):
    unknown = new_id()
    assert _undo(world, unknown, input_fn=_never_called) == (
        2, f"refused (not-found): no change {unknown}\n")

    created = _versions(world, world.memory.id)[0].change.change_id
    _dream_update(world, world.memory.id, 1, "line three")
    code, out = _undo(world, created)
    assert code == 2
    assert out.endswith(f"refused (changed): {world.memory.id} changed after change {created}; "
                        "nothing was changed; use memriver history and memriver restore\n")


# §8.2: refusals print their reason (hard-deleted)
def test_undo_of_a_change_a_hard_delete_cut_is_refused(world):
    pair = world.services.memory.apply(
        [Create(world.project.id, "project", "x cue", "x"),
         Create(world.project.id, "project", "y cue", "y")],
        changed_by="dream")
    doomed = pair.steps[1].memory_id
    plan = world.services.maintenance.plan_hard_delete(doomed)
    world.services.maintenance.hard_delete(doomed, expected=plan.expected)
    code, out = _undo(world, pair.change_id)
    assert code == 2
    assert "  (1 more step removed by a hard delete)\n" in out
    assert out.endswith(f"refused (hard-deleted): a hard delete removed part of change "
                        f"{pair.change_id}; it cannot be undone\n")


# §8.2: refusals print the rule id of a policy hit
def test_an_undo_refused_by_the_content_policy_prints_the_rule(world, monkeypatch):
    created = _versions(world, world.memory.id)[0].change.change_id
    _use_services(monkeypatch, world)

    def rejected(*args, **kwargs):
        raise ContentRejected(rule_id="github-pat", memory_id=world.memory.id)

    monkeypatch.setattr(world.services.memory, "undo", rejected)
    code, out = _undo(world, created)
    assert code == 2
    assert out.endswith("refused (content policy): the undo would restore content failing "
                        "rule github-pat; nothing was changed\n")


def test_undo_declined_changes_nothing(world):
    created = _versions(world, world.memory.id)[0].change.change_id
    code, out = _undo(world, created, input_fn=lambda _: "n")
    assert code == 1 and out.endswith("aborted; nothing was changed\n")
    assert world.services.memory.show(world.memory.id).version == 1


# --- delete (soft) -----------------------------------------------------------

# §8.2: soft delete as today, from the memory's project directory; the change is human
def test_soft_delete_from_the_project_directory_is_a_human_change(world):
    memory_id = world.memory.id
    code, out = _delete(world, memory_id, version=1)
    assert code == 0
    assert out == (f"memriver delete: {memory_id} [project] in project {world.project.id}: "
                   f"the cue  (soft)\n"
                   f"deleted {memory_id}\n")
    latest = _versions(world, memory_id)[-1]
    assert latest.deleted and latest.change.changed_by == "human"


def test_soft_delete_needs_the_current_version(world):
    code, out = _delete(world, world.memory.id, version=5)
    assert code == 2 and "changed since version 5" in out
    assert world.services.memory.show(world.memory.id).version == 1


def test_soft_delete_from_outside_the_project_names_the_owning_project(world, tmp_path):
    code, out = _delete(world, world.memory.id, version=1, input_fn=_never_called, cwd=tmp_path)
    assert code == 2
    assert out == (f"refused: {world.memory.id} belongs to project {world.project.id}, not "
                   "this directory's project; run memriver delete from that project's "
                   "directory\n")


# §8.2: a global memory by id goes through apply with changed_by='human'
def test_a_global_memory_is_soft_deleted_by_id_from_anywhere(world, tmp_path):
    shared = _global_memory(world)
    code, out = _delete(world, shared, version=1, cwd=tmp_path)
    assert code == 0
    assert out.startswith(f"memriver delete: {shared} [project] in global: a shared rule  "
                          "(soft)\n")
    latest = _versions(world, shared)[-1]
    assert latest.deleted and latest.change.changed_by == "human"


# spec C3-b: every apply is checked against the content policy, a soft delete included;
# a memory whose stored text now hits the policy cannot be soft-deleted -- the way out
# is the hard delete
def test_a_soft_delete_of_a_project_memory_whose_text_now_hits_the_policy_is_refused(world):
    memory_id = world.memory.id
    _plant_policy_hit(world, memory_id)
    with pytest.raises(ContentRejected) as excinfo:
        world.services.memory.apply([SoftDelete(memory_id, 1)], changed_by="human")
    rule_id = excinfo.value.rule_id
    before = _versions(world, memory_id)

    code, out = _delete(world, memory_id, version=1)

    assert code == 2
    assert out == (f"memriver delete: {memory_id} [project] in project {world.project.id}: "
                   "the cue  (soft)\n"
                   f"refused (content policy): {memory_id} fails rule {rule_id}; remove it "
                   f"with memriver delete {memory_id} --hard\n")
    assert _versions(world, memory_id) == before


def test_a_soft_delete_of_a_global_memory_whose_text_now_hits_the_policy_is_refused(world,
                                                                                    tmp_path):
    shared = _global_memory(world)
    _plant_policy_hit(world, shared)
    with pytest.raises(ContentRejected) as excinfo:
        world.services.memory.apply([SoftDelete(shared, 1)], changed_by="human")
    rule_id = excinfo.value.rule_id
    before = _versions(world, shared)

    code, out = _delete(world, shared, version=1, cwd=tmp_path)

    assert code == 2
    assert out == (f"memriver delete: {shared} [project] in global: a shared rule  (soft)\n"
                   f"refused (content policy): {shared} fails rule {rule_id}; remove it "
                   f"with memriver delete {shared} --hard\n")
    assert _versions(world, shared) == before


def test_soft_delete_declined_at_eof_or_without_a_terminal_changes_nothing(world):
    def eof(_prompt):
        raise EOFError

    memory_id = world.memory.id
    assert _delete(world, memory_id, version=1, input_fn=lambda _: "n")[0] == 1
    assert _delete(world, memory_id, version=1, input_fn=eof)[0] == 1
    code, out = _delete(world, memory_id, version=1, input_fn=_never_called, tty=False)
    assert code == 2 and "stdin is not a terminal" in out
    assert world.services.memory.show(memory_id).version == 1


def test_soft_delete_with_yes_skips_the_prompt(world):
    code, out = _delete(world, world.memory.id, version=1, yes=True, input_fn=_never_called,
                        tty=False)
    assert code == 0 and out.endswith(f"deleted {world.memory.id}\n")


# --- delete --hard -----------------------------------------------------------

def _cited(world):
    """world.memory (in the project) and a global memory citing its v1: a two-member plan."""
    return world.memory.id, _global_memory(world, (world.memory.id, 1))


def _command_tail(world, code: str) -> str:
    return f"--confirm {code} --root {shlex.quote(str(world.store))}\n"


# §8.2 --dry-run: the plan and the command with --confirm <code>; nothing deleted
def test_hard_dry_run_prints_the_plan_and_the_confirm_command(world):
    target, citing = _cited(world)
    code, out = _delete(world, target, hard=True, dry_run=True, input_fn=_never_called)
    plan_code = world.services.maintenance.plan_hard_delete(target).code
    assert code == 0
    assert out == ("memriver delete --hard: removes 2 memories with every version, source "
                   "and read:\n"
                   f"  {target}  project {world.project.id}  v1  the cue\n"
                   f"  {citing}  global  v1  a shared rule\n"
                   f"    because {citing} v1 cites {target} v1\n"
                   "  changes that touched them stay in the log but can no longer be undone\n"
                   f"to delete exactly these, run: memriver delete {target} --hard "
                   + _command_tail(world, plan_code))
    assert not _gone(world, target) and not _gone(world, citing)


# §8.2 --confirm CODE: no prompt; the code is the two-step link, nothing stored between
def test_confirm_with_the_printed_code_purges_every_member(world):
    target, citing = _cited(world)
    plan_code = world.services.maintenance.plan_hard_delete(target).code
    code, out = _delete(world, target, hard=True, confirm_code=plan_code,
                        input_fn=_never_called)
    assert code == 0 and out.startswith("purged ")
    assert set(out.removeprefix("purged ").strip().split(", ")) == {target, citing}
    assert _gone(world, target) and _gone(world, citing)


# §8.2 --confirm: PlanChanged fails with exit 2; the new plan and command are printed
def test_a_stale_code_deletes_nothing_and_prints_the_new_plan(world):
    target, citing = _cited(world)
    stale = world.services.maintenance.plan_hard_delete(target).code
    late = _global_memory(world, (target, 1))
    code, out = _delete(world, target, hard=True, confirm_code=stale, input_fn=_never_called)
    fresh = world.services.maintenance.plan_hard_delete(target).code
    assert code == 2 and fresh != stale
    assert out.startswith("refused: the plan changed since it was printed; nothing was "
                          "deleted\n"
                          "memriver delete --hard: removes 3 memories")
    assert f"    because {late} v1 cites {target} v1\n" in out
    assert out.endswith(_command_tail(world, fresh))
    assert not any(_gone(world, memory_id) for memory_id in (target, citing, late))


# §8.2 default: plan, y/N, hard_delete(expected = the set shown); PlanChanged asks again
def test_interactive_hard_delete_asks_again_when_the_plan_changes(world):
    target, citing = _cited(world)
    late: list[str] = []
    code, out = _delete(world, target, hard=True, input_fn=_answers(
        lambda: late.append(_global_memory(world, (target, 1)))))
    assert code == 0
    assert "the plan changed while waiting; nothing was deleted yet\n" in out
    assert out.count("memriver delete --hard: removes") == 2 and "removes 3 memories" in out
    assert all(_gone(world, memory_id) for memory_id in (target, citing, *late))


def test_hard_delete_under_yes_fails_when_the_plan_changes(world, monkeypatch):
    target, _citing = _cited(world)
    _use_services(monkeypatch, world)
    plan = world.services.maintenance.plan_hard_delete(target)

    def changed(*args, **kwargs):
        raise PlanChanged(plan)

    monkeypatch.setattr(world.services.maintenance, "hard_delete", changed)
    code, out = _delete(world, target, hard=True, yes=True, input_fn=_never_called, tty=False)
    assert code == 2
    assert out.endswith("refused: the plan changed while waiting; nothing was deleted; run "
                        "the command again\n")


def test_hard_delete_declined_deletes_nothing(world):
    target, citing = _cited(world)
    code, out = _delete(world, target, hard=True, input_fn=lambda _: "n")
    assert code == 1 and out.endswith("aborted; nothing was changed\n")
    assert not _gone(world, target) and not _gone(world, citing)


def test_hard_delete_of_an_unknown_memory_is_refused(world):
    unknown = new_id()
    assert _delete(world, unknown, hard=True, dry_run=True, input_fn=_never_called) == (
        2, f"no such memory: {unknown}\n")


# --- through the CLI ---------------------------------------------------------

def test_cli_wires_history_and_a_hard_dry_run(world, capsys):
    assert cli.main(["history", world.memory.id, "--root", str(world.store)]) == 0
    assert capsys.readouterr().out.startswith("v1  ")
    assert cli.main(["delete", world.memory.id, "--hard", "--dry-run",
                     "--root", str(world.store)]) == 0
    assert f"memriver delete {world.memory.id} --hard --confirm " in capsys.readouterr().out
    assert not _gone(world, world.memory.id)
