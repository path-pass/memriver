"""Published session summaries through SessionService (spec §4.3; acceptance §10 item 8)."""

from __future__ import annotations

import pytest
from memriver_core.models import SessionKey
from memriver_core.models.errors import ContentRejected, SessionMoved

SECRET = "aws key AKIAIOSFODNN7EXAMPLE ok"          # from the secret-scanner tests
KEY = SessionKey("codex", "session-1")


def _start(world, key=KEY, directory=None):
    return world["services"].session.start_session(
        key, source="startup", entry_dir=str(directory or world["work"]), transcript_path=None)


def _stored(world, key=KEY):
    return next(s for s in world["services"].session.list_sessions() if s.key == key)


def _observed(world) -> str:
    [session] = world["services"].session.bound_sessions()
    return session.last_active_at


def test_bound_sessions_lists_only_sessions_bound_to_a_project(world, tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    _start(world)
    _start(world, SessionKey("claude-code", "unbound"), loose)
    world["services"].session.observe_prompt(SessionKey("claude-code", "pending"), prompt="hi",
                                             entry_dir=str(world["work"]),
                                             transcript_path=None)
    [bound] = world["services"].session.bound_sessions()
    assert (bound.key, bound.project_id, bound.summary, bound.summary_at) == \
        (KEY, world["mine"], None, None)


def test_publish_sets_the_summary_and_its_time_and_search_finds_it(world):
    sessions = world["services"].session
    _start(world)
    observed = _observed(world)
    sessions.publish_summary(KEY, "Fixed the flaky login test.",
                             expected_last_active_at=observed)
    stored = _stored(world)
    assert stored.summary == "Fixed the flaky login test."
    assert stored.summary_at >= observed and stored.last_active_at == observed
    assert [s.key for s in sessions.search_sessions("flaky", world["context"])] == [KEY]
    assert sessions.bound_sessions()[0].summary_at == stored.summary_at


def test_publish_refuses_a_session_that_moved_and_writes_nothing(world):
    _start(world)
    observed = _observed(world)
    world["services"].session.observe_prompt(KEY, prompt="one more thing",
                                             entry_dir=str(world["work"]),
                                             transcript_path=None)
    with pytest.raises(SessionMoved):
        world["services"].session.publish_summary(KEY, "a stale summary",
                                                  expected_last_active_at=observed)
    assert _stored(world).summary is None


def test_publish_refuses_an_unbound_or_unknown_session(world, tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    unbound = SessionKey("claude-code", "unbound")
    _start(world, unbound, loose)
    at = _stored(world, unbound).last_active_at
    for key in (unbound, SessionKey("codex", "never-seen")):
        with pytest.raises(SessionMoved):
            world["services"].session.publish_summary(key, "a summary",
                                                      expected_last_active_at=at)
    assert _stored(world, unbound).summary is None


@pytest.mark.parametrize(("text", "rule_id"), [
    (SECRET, None), ("x" * 1201, "too-large"), ("   ", "empty")])
def test_publish_runs_the_content_policy_and_the_length_limit(world, text, rule_id):
    _start(world)
    with pytest.raises(ContentRejected) as excinfo:
        world["services"].session.publish_summary(KEY, text,
                                                  expected_last_active_at=_observed(world))
    if rule_id is None:
        assert excinfo.value.rule_id not in ("", "empty", "too-large")
    else:
        assert excinfo.value.rule_id == rule_id
    assert SECRET not in str(excinfo.value)
    assert _stored(world).summary is None


def test_a_summary_of_exactly_the_limit_is_published(world):
    _start(world)
    world["services"].session.publish_summary(KEY, "y" * 1200,
                                              expected_last_active_at=_observed(world))
    assert len(_stored(world).summary) == 1200
