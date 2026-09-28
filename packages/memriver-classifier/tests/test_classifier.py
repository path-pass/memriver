"""The source switches every backend sits behind."""

from __future__ import annotations

from memriver_classifier.backends import Classifier
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
