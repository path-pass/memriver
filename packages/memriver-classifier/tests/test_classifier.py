"""The source switches every backend sits behind."""

from __future__ import annotations

from memriver_classifier import build_classifier
from memriver_classifier.backends import (
    Classifier,
    ClaudeBackend,
    CodexBackend,
    JevBackend,
    backend_for,
)
from memriver_classifier.settings import check_classifier_table
from memriver_core import Verdict


def _classifier(*, agent_writes=True, dream_writes=True):
    seen: list[str] = []

    def check(text):
        seen.append(text)
        return Verdict("instruction")

    return Classifier(check, agent_writes=agent_writes, dream_writes=dream_writes), seen


def test_agent_and_dream_writes_are_checked_by_default_and_human_never():
    classifier, seen = _classifier()
    assert classifier.classify("a", changed_by="mcp") == Verdict("instruction")
    assert classifier.classify("b", changed_by="dream") == Verdict("instruction")
    assert classifier.classify("c", changed_by="human") is None
    assert seen == ["a", "b"]


def test_each_switch_turns_off_its_own_source_only():
    classifier, seen = _classifier(agent_writes=False)
    assert classifier.classify("a", changed_by="mcp") is None
    assert classifier.classify("b", changed_by="dream") == Verdict("instruction")
    classifier, _ = _classifier(dream_writes=False)
    assert classifier.classify("c", changed_by="dream") is None
    assert classifier.classify("d", changed_by="mcp") == Verdict("instruction")
    assert seen == ["b"]


def test_a_source_no_switch_names_is_checked():
    classifier, seen = _classifier(agent_writes=False, dream_writes=False)
    assert classifier.classify("a", changed_by="test") == Verdict("instruction")
    assert seen == ["a"]


def test_no_table_or_a_disabled_table_builds_no_classifier():
    assert build_classifier(None, {}) is None
    table = check_classifier_table({"backend": "jev", "enabled": False})
    assert build_classifier(table, {}) is None


def test_a_jev_table_builds_the_switched_jev_backend():
    table = check_classifier_table({"backend": "jev", "api_key_env": "UNSET_FOR_TEST",
                                    "agent_writes": False})
    classifier = build_classifier(table, {})
    # no key: an undecided verdict, and no request is made
    assert classifier.classify("a fact", changed_by="dream") == Verdict("unavailable",
                                                                       detail="no-key")
    assert classifier.classify("a fact", changed_by="mcp") is None
    assert classifier.classify("a fact", changed_by="human") is None


def test_backend_for_picks_the_configured_backend():
    claude = check_classifier_table({"backend": "claude", "executor_path": "/opt/bin/claude"})
    codex = check_classifier_table({"backend": "codex", "executor_path": "/opt/bin/codex"})
    jev = check_classifier_table({"backend": "jev"})
    assert isinstance(backend_for(claude, {}), ClaudeBackend)
    assert isinstance(backend_for(codex, {}), CodexBackend)
    assert isinstance(backend_for(jev, {}), JevBackend)
