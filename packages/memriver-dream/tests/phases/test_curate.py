"""Global curation over a real store and a scripted executor: the evidence a
promotion needs and keeps, demotion only after curate runs in a row, the four limits,
tags and their descriptions, one change per run, the report and dream.db's records."""

from __future__ import annotations

import json

import pytest
from memriver_core import Verdict
from memriver_core.bootstrap import build_services
from memriver_core.settings import Settings
from memriver_dream.phases import PassResult, curate
from memriver_dream.protocols import ExecutorResult
from memriver_dream.store import CurationRow, DreamStore, EvidenceRow

QUOTE = "never push to main"
RULE = f'The user said "{QUOTE}"; open a pull request instead.'
AT = RULE.index(QUOTE)                         # 15: where the quote stands in the body


def _entry(memory_id: str, level: int = 2, tags=("general",), *, quotes=(), restated_by=(),
           body: str = "", reason: str = "judged on its text") -> dict:
    return {"id": memory_id, "level": level, "tags": list(tags), "quotes": list(quotes),
            "restated_by": list(restated_by), "body": body, "reason": reason}


def _answer(*entries: dict, tags: dict | None = None) -> dict:
    """An answer; without `tags`, every name the entries use comes with a description."""
    if tags is None:
        tags = {name: f"{name} things" for entry in entries for name in entry["tags"]}
    return {"entries": list(entries),
            "tags": [{"name": name, "description": text} for name, text in tags.items()]}


def _global(world, body: str = RULE, *, type: str = "feedback") -> str:
    return world.create(world.global_id, body, type=type)


def _store(world) -> DreamStore:
    return DreamStore(world.root / "dream" / "dream.db")


def _pass(world, *replies, **overrides) -> tuple[PassResult, str]:
    world.executor.replies = list(replies)
    ctx = world.context(**overrides)
    result = curate.run(ctx)
    ctx.report.footer(status="completed", finished_at=world.now)
    text = ctx.report.path.read_text()
    ctx.report.path.unlink()
    return result, text


def _state(world, memory_id: str) -> tuple:
    memory = world.services.memory.show(memory_id)
    return memory.version, memory.level, memory.tags


def _limited(world, **caps):
    return build_services(Settings(root=world.root, **caps), root=world.root, home=world.home)


def _quote_row(memory_id: str, version: int, run_id: str, now: str) -> EvidenceRow:
    return EvidenceRow(memory_id, version, run_id, "quote", "body", AT, AT + len(QUOTE),
                       None, None, now)


def test_the_first_run_judges_every_entry_in_one_change(world):
    rule, note = _global(world), _global(world, "uv manages python", type="user")
    result, text = _pass(world, _answer(
        _entry(rule, 1, ("git",), quotes=(QUOTE,)), _entry(note, 2, ("python", "tools")),
        tags={"git": "branches, pushes, pull requests", "python": "uv, venvs, pytest",
              "tools": "editors and command line tools"}))
    assert result == PassResult(finished=True)
    assert _state(world, rule) == (2, 1, ("git",))
    assert _state(world, note) == (2, 2, ("python", "tools"))
    assert text.count("applying curate ") == 1
    assert f"applying curate {rule} {note} -> change " in text
    assert f"promoted {rule} (v1 body[{AT}:{AT + len(QUOTE)}]): judged on its text\n" in text
    assert (f"level 1 after this curation: 1 entries, {len(RULE)} characters "
            "(before it: 0, 0)\n") in text
    assert "tags added: git python tools; dropped: -\n" in text
    assert "tag git: description set\n" in text
    needs = text.split("== Needs you ==\n")[1]
    assert f"global changed: curate {rule} {note}" in needs
    assert world.services.memory.tag_descriptions(world.global_id)["git"] == \
        "branches, pushes, pull requests"


def test_the_input_carries_levels_tags_restatements_and_the_names_in_use(world):
    rule = _global(world)
    _pass(world, _answer(_entry(rule, 2, ("git",)), tags={"git": "branches"}))
    proof = world.create(world.project.id, "never push to main here either")
    _store(world).put_restatements(rule, [(proof, 1)], run_id="r1", at=world.now)
    _pass(world, _answer(_entry(rule, 2, ("git",))))
    call = world.executor.calls[-1]
    entry = json.loads(call["prompt"].split("<entries>\n")[1].split("\n</entries>")[0])
    assert (entry["id"], entry["level"], entry["tags"], entry["restated_by"]) == \
        (rule, 2, ["git"], [proof])
    assert '{"name": "git", "description": "branches"}' in call["prompt"]
    system = call["system_prompt"]
    for rule_text in ("how much would the user mind", "Reuse a name already in use",
                      "never lower one because nobody re-stated it", "At most 10 entries",
                      "1 to 3 names", "at most 12 names", "within 200 characters",
                      "Never merge, split or delete entries"):
        assert rule_text in system, rule_text


@pytest.mark.parametrize(("quotes", "restated", "promoted"), [
    ((), (), False), (("push to main",), (), True), (("pushed to main",), (), False),
    (("  ",), (), False), ((), ("known",), True), ((), ("zzzzzzzzzz",), False)],
    ids=["none", "quote", "quote-not-in-text", "blank-quote", "restatement",
         "unknown-restatement"])
def test_level_1_needs_evidence_the_code_can_find(world, quotes, restated, promoted):
    rule = _global(world)
    proof = world.create(world.project.id, "never push to main")
    _store(world).put_restatements(rule, [(proof, 1)], run_id="r1", at=world.now)
    restated = tuple(proof if memory_id == "known" else memory_id for memory_id in restated)
    _, text = _pass(world, _answer(_entry(rule, 1, quotes=quotes, restated_by=restated)),
                    run_id="r2")
    assert _state(world, rule)[1] == (1 if promoted else 2)
    assert (f"refused level 1 {rule}: evidence\n" in text) is not promoted
    rows = _store(world).evidence(rule)
    if not promoted:
        assert rows == []
    elif quotes:
        start = RULE.index("push to main")
        assert rows == [EvidenceRow(rule, 1, "r2", "quote", "body", start, start + 12, None,
                                    None, world.now)]
    else:
        assert rows == [EvidenceRow(rule, 1, "r2", "restatement", None, None, None, proof, 1,
                                    world.now)]
        assert f"(restated by {proof} v1)" in text


def test_quote_evidence_stays_traceable_after_a_rewrite_and_a_demotion(world):
    long_rule = f'{RULE} {"Explain why in the pull request. " * 6}'.strip()
    rule = _global(world, long_rule)
    settings = world.dream.model_copy(update={"standing_demote_runs": 1})
    short = "Open a pull request; avoid direct pushes."
    _pass(world, _answer(_entry(rule, 1, quotes=(QUOTE,), body=short)), run_id="r1")
    assert world.services.memory.show(rule).body == short
    _pass(world, _answer(_entry(rule, 2)), settings=settings, run_id="r2")
    assert _state(world, rule)[1] == 2
    (row,) = _store(world).evidence(rule)
    assert row == _quote_row(rule, 1, "r1", world.now)
    judged = next(v for v in world.services.memory.versions(rule)
                  if v.version == row.memory_version)
    assert getattr(judged, row.field)[row.start:row.stop] == QUOTE


def test_demotion_needs_the_entry_judged_2_in_curate_runs_in_a_row(world):
    rule = _global(world)
    settings = world.dream.model_copy(update={"standing_demote_runs": 2})
    _pass(world, _answer(_entry(rule, 1, quotes=(QUOTE,))), settings=settings)
    _, text = _pass(world, _answer(_entry(rule, 2)), settings=settings)
    assert f"kept {rule} at level 1 (judged 2, 1 of 2): judged on its text\n" in text
    assert _state(world, rule)[1] == 1
    assert _store(world).curation(rule) == CurationRow(rule, 1, world.now)
    _pass(world, _answer(_entry(rule, 1)), settings=settings)        # in between: starts over
    assert _store(world).curation(rule) is None
    _pass(world, _answer(_entry(rule, 2)), settings=settings)
    _, text = _pass(world, _answer(_entry(rule, 2)), settings=settings)
    assert (f"demoted {rule} (judged 2 in 2 curate runs in a row): judged on its text\n"
            in text)
    assert _state(world, rule)[1] == 2 and _store(world).curation(rule) is None


@pytest.mark.parametrize("gap", ["left out", "bad reason", "call failed", "not applied"])
def test_a_curate_run_that_does_not_judge_the_rule_breaks_its_streak(world, gap):
    rule, other = _global(world), _global(world, "uv manages python", type="user")
    settings = world.dream.model_copy(update={"standing_demote_runs": 2})
    _pass(world, _answer(_entry(rule, 1, quotes=(QUOTE,)), _entry(other)), settings=settings)
    _pass(world, _answer(_entry(rule, 2), _entry(other)), settings=settings)
    assert _store(world).curation(rule).judged_two == 1
    reply = {"left out": _answer(_entry(other, 2, ("tools",))),
             "bad reason": _answer(_entry(rule, 2, reason=world.secret), _entry(other)),
             "call failed": ExecutorResult(error="timeout"),
             "not applied": _answer(_entry(rule, 2), _entry(other, 2, ("tools",)))}[gap]
    limited = settings.model_copy(update={"max_groups_per_run": 1})
    overrides = {"settings": limited, "groups_used": 1} if gap == "not applied" else \
        {"settings": settings}
    result, _ = _pass(world, reply, **overrides)
    assert not result.finished and _store(world).curation(rule) is None
    _, text = _pass(world, _answer(_entry(rule, 2), _entry(other)), settings=settings)
    assert _state(world, rule)[1] == 1                     # 1 of 2 again, not 2 of 2
    assert f"kept {rule} at level 1 (judged 2, 1 of 2)" in text


def test_a_rule_kept_at_level_1_needs_no_new_evidence(world):
    rule = _global(world)
    _pass(world, _answer(_entry(rule, 1, quotes=(QUOTE,))))
    result, text = _pass(world, _answer(_entry(rule, 1, ("git",))))
    assert result.finished and _state(world, rule) == (3, 1, ("git",))
    assert "refused" not in text
    assert len(_store(world).evidence(rule)) == 1          # the promotion's, kept as it was


def test_a_long_rule_is_promoted_only_with_a_rewrite_within_the_limit(world):
    long_rule = f'{RULE} {"Explain why in the pull request. " * 6}'.strip()
    assert len(long_rule) > 200
    rule = _global(world, long_rule)
    _, text = _pass(world, _answer(_entry(rule, 1, quotes=(QUOTE,), body="x" * 201)))
    assert f"refused level 1 {rule}: body\n" in text and _state(world, rule)[1] == 2
    short = "Never push to main; open a pull request."
    _pass(world, _answer(_entry(rule, 1, quotes=(QUOTE,), body=short)))
    memory = world.services.memory.show(rule)
    assert (memory.level, memory.body) == (1, short)


def test_more_candidates_than_the_room_keep_the_most_restated(world):
    services = _limited(world, standing_max_count=1)
    first, second = _global(world), _global(world, f"{RULE} Always.")
    proof = world.create(world.project.id, "never push to main")
    _store(world).put_restatements(second, [(proof, 1)], run_id="r1", at=world.now)
    _, text = _pass(world, _answer(_entry(first, 1, quotes=(QUOTE,)),
                                   _entry(second, 1, restated_by=(proof,))),
                    services=services)
    assert _state(world, second)[1] == 1 and _state(world, first)[1] == 2
    assert f"held {first} at level 2: standing_max_count\n" in text
    assert (f"global curation: {first} judged a standing rule but held at level 2 by "
            "standing_max_count (1)") in text.split("== Needs you ==\n")[1]
    assert _store(world).evidence(first) == []


def test_a_full_limit_never_demotes_an_existing_rule_for_a_stronger_candidate(world):
    services = _limited(world, standing_max_count=1)
    rule, candidate = _global(world), _global(world, f"{RULE} Always.")
    _pass(world, _answer(_entry(rule, 1, quotes=(QUOTE,)), _entry(candidate)),
          services=services)
    proof = world.create(world.project.id, "never push to main")
    _store(world).put_restatements(candidate, [(proof, 1)], run_id="r1", at=world.now)
    _, text = _pass(world, _answer(_entry(rule, 1), _entry(candidate, 1, restated_by=(proof,))),
                    services=services)
    assert _state(world, rule)[1] == 1 and _state(world, candidate)[1] == 2
    assert f"held {candidate} at level 2: standing_max_count\n" in text
    # a rule waiting out its demotion streak keeps its place too
    _, text = _pass(world, _answer(_entry(rule, 2), _entry(candidate, 1, restated_by=(proof,))),
                    services=services)
    assert _state(world, rule)[1] == 1 and _state(world, candidate)[1] == 2


def test_a_rule_the_answer_leaves_out_keeps_its_place_under_the_limit(world):
    services = _limited(world, standing_max_count=1)
    kept, other = _global(world), _global(world, f"{RULE} Always.")
    _pass(world, _answer(_entry(kept, 1, quotes=(QUOTE,))), services=services)
    result, text = _pass(world, _answer(_entry(other, 1, quotes=(QUOTE,))), services=services)
    assert not result.finished and f"not judged: {kept}\n" in text
    assert _state(world, kept)[1] == 1 and _state(world, other)[1] == 2
    assert f"held {other} at level 2: standing_max_count\n" in text


def test_an_entry_the_policy_scan_left_out_still_counts_against_the_limits(world):
    services = _limited(world, standing_max_count=1, tags_max=1)
    hidden = _global(world)
    _pass(world, _answer(_entry(hidden, 1, ("git",), quotes=(QUOTE,))), services=services)
    other = _global(world, f"{RULE} Always.")
    world.executor.replies = [_answer(_entry(other, 1, ("other",), quotes=(QUOTE,)))]
    ctx = world.context(services=services, excluded={hidden})
    assert curate.run(ctx).finished
    text = ctx.report.path.read_text()
    assert _state(world, other) == (2, 2, ())
    assert f"held {other} at level 2: standing_max_count\n" in text
    assert "level 1 after this curation: 1 entries" in text
    assert json.loads(world.executor.calls[-1]["prompt"].split(
        "<entries>\n")[1].split("\n</entries>")[0])["id"] == other


@pytest.mark.parametrize("tags", [(), ("a", "b", "c", "d"), ("Git",), ("git", "git")],
                         ids=["none", "four", "upper", "repeated"])
def test_tags_that_break_a_rule_leave_the_entry_its_tags(world, tags):
    note = _global(world, "uv manages python", type="user")
    _pass(world, _answer(_entry(note, 2, ("python",))))
    result, text = _pass(world, _answer(_entry(note, 2, tags)))
    assert result.finished and _state(world, note) == (2, 2, ("python",))
    assert f"refused tags {note}: tags\n" in text


def test_the_configured_tags_per_memory_max_is_the_limit_not_three(world):
    services = _limited(world, tags_per_memory_max=4)
    note = _global(world, "uv manages python", type="user")
    result, _ = _pass(world, _answer(_entry(note, 2, ("a", "b", "c", "d"))),
                      services=services)
    assert result.finished and _state(world, note) == (2, 2, ("a", "b", "c", "d"))
    assert "1 to 4 names" in world.executor.calls[-1]["system_prompt"]


def test_more_tag_names_than_the_limit_change_no_tags(world):
    services = _limited(world, tags_max=2)
    a, b = _global(world, "one", type="user"), _global(world, "two", type="user")
    _, text = _pass(world, _answer(_entry(a, 2, ("x", "y")), _entry(b, 2, ("z",))),
                    services=services)
    assert _state(world, a) == (2, 2, ()) and _state(world, b) == (2, 2, ())
    assert ("global curation proposed 3 tag names, more than tags_max (2); no tags were "
            "changed") in text.split("== Needs you ==\n")[1]


def test_a_new_tag_name_without_a_description_is_not_applied(world):
    note = _global(world, "uv manages python", type="user")
    result, text = _pass(world, _answer(_entry(note, 2, ("python",)), tags={}))
    assert result == PassResult(finished=False)
    assert _state(world, note) == (2, 2, ())              # the level, not the tag
    assert f"refused tags {note}: no description for python\n" in text
    assert world.services.memory.tag_descriptions(world.global_id) == {}
    # a name described before needs no new description
    world.services.memory.describe_tag(world.global_id, "python", "uv", changed_by="test")
    result, _ = _pass(world, _answer(_entry(note, 2, ("python",)), tags={}))
    assert result.finished and _state(world, note) == (3, 2, ("python",))


def test_tag_changes_and_descriptions_are_reported(world):
    note = _global(world, "uv manages python", type="user")
    _pass(world, _answer(_entry(note, 2, ("old",)), tags={"old": "the old group"}))
    _, text = _pass(world, _answer(_entry(note, 2, ("python", "new")),
                                   tags={"python": "uv and venvs", "new": "the new group",
                                         "old": "never carried now"}))
    assert "tags added: new python; dropped: old\n" in text
    assert "tag python: description set\n" in text and "tag new: description set\n" in text
    assert "tag old:" not in text
    assert world.services.memory.tag_descriptions(world.global_id) == {
        "old": "the old group", "python": "uv and venvs", "new": "the new group"}


def test_a_project_type_entry_not_yet_judged_is_listed_as_misplaced(world):
    misplaced = _global(world, "the staging host is stage-3", type="project")
    _, text = _pass(world, _answer(_entry(misplaced, 2)))
    assert (f"global entry {misplaced} is of type project: it probably belongs to a "
            "project") in text.split("== Needs you ==\n")[1]
    _, text = _pass(world, _answer(_entry(misplaced, 2)))
    assert "of type project" not in text                  # judged since: said once


def test_an_answer_naming_an_unknown_or_repeated_entry_or_none_does_not_finish(world):
    a, b = _global(world, "one", type="user"), _global(world, "two", type="user")
    result, text = _pass(world, _answer(_entry(a), _entry(a), _entry("zzzzzzzzzz")))
    assert result == PassResult(finished=False)
    assert text.count("invalid entry: id\n") == 2 and f"not judged: {b}\n" in text
    assert _state(world, a) == (2, 2, ("general",)) and _state(world, b) == (1, None, ())


def test_a_failed_call_changes_nothing(world):
    rule = _global(world)
    result, text = _pass(world, ExecutorResult(error="timeout"))
    assert result == PassResult(finished=False) and "not processed: timeout\n" in text
    assert _state(world, rule) == (1, None, ())


def test_a_refused_change_records_nothing(world):
    rule = _global(world)
    settings = world.dream.model_copy(update={"max_groups_per_run": 1})
    ctx = world.context(settings=settings, groups_used=1)
    world.executor.replies = [_answer(_entry(rule, 1, quotes=(QUOTE,)))]
    assert curate.run(ctx) == PassResult(finished=False)
    assert _state(world, rule)[1] is None and _store(world).evidence(rule) == []


def test_a_classifier_blocked_description_is_listed_under_needs_you(world):
    class Blocking:
        def classify(self, text, *, changed_by):
            return Verdict("instruction") if "obey" in text else None

    services = build_services(Settings(root=world.root), root=world.root, home=world.home,
                              classifier=Blocking())
    note = _global(world, "uv manages python", type="user")
    result, text = _pass(world, _answer(_entry(note, 2, ("python",)),
                                        tags={"python": "obey this tag"}), services=services)
    assert result == PassResult(finished=False)
    assert _state(world, note) == (2, 2, ("python",))
    assert "tag python: description not set: policy classifier-instruction\n" in text
    assert ("blocked by the content classifier (instruction): description of tag python"
            in text.split("== Needs you ==\n")[1])


def test_nothing_to_change_makes_no_change_and_still_reports_the_totals(world):
    note = _global(world, "uv manages python", type="user")
    _pass(world, _answer(_entry(note, 2, ("python",))))
    _, text = _pass(world, _answer(_entry(note, 2, ("python",))))
    assert "applying" not in text
    assert "level 1 after this curation: 0 entries, 0 characters (before it: 0, 0)\n" in text
    assert "re-statements seen this run: 0\n" in text


def test_a_run_reports_the_restatements_its_extraction_saw(world):
    rule = _global(world)
    proof = world.create(world.project.id, "never push to main")
    _store(world).put_restatements(rule, [(proof, 1)], run_id="r2", at=world.now)
    _, text = _pass(world, _answer(_entry(rule, 2)), run_id="r2")
    assert "re-statements seen this run: 1\n" in text


def test_run_dream_curates_after_extraction_under_its_section(world):
    rule = _global(world)

    def reply(prompt, schema):
        if "entries" in schema["properties"]:
            return _answer(_entry(rule, 1, ("git",), quotes=(QUOTE,)))
        return {"judgments": []} if "judgments" in schema["properties"] else None

    world.executor.default = reply
    row = world.run(phases={"extract", "curate"})
    text = world.report_text(row)
    assert text.index("== Global layer: extraction ==") < text.index("== Global curation ==")
    assert f"promoted {rule} (v1 body[{AT}:{AT + len(QUOTE)}]): judged on its text\n" in text
    assert _store(world).evidence(rule) == [_quote_row(rule, 1, row.run_id, world.now)]
    assert _store(world).curation(rule) is None
