"""The change and history shapes (spec §3-§4): frozen values, standard library only."""

from __future__ import annotations

import dataclasses

import pytest
from memriver_core.models import (
    Create,
    HardDeleteItem,
    HardDeletePlan,
    Op,
    Restore,
    SoftDelete,
    SourceRef,
    Update,
)


def test_create_defaults_match_the_spec():
    create = Create("pppppppppp", "project", "cue", "body")
    assert (create.sources, create.trust, create.sync) == ((), "agent", True)


def test_update_keeps_every_value_it_is_not_given():
    update = Update("mmmmmmmmmm", 3)
    assert (update.description, update.body, update.sources) == (None, None, None)


def test_every_operation_is_an_op_and_frozen():
    ops = [Create("pppppppppp", "user", "", "b"), Update("mmmmmmmmmm", 1, body="x"),
           SoftDelete("mmmmmmmmmm", 1), Restore("mmmmmmmmmm", 2, 1)]
    for op in ops:
        assert isinstance(op, Op)
        with pytest.raises(dataclasses.FrozenInstanceError):
            op.memory_id = "x"  # type: ignore[misc]
    assert not isinstance(SourceRef("mmmmmmmmmm", 1), Op)


def test_a_plan_expects_every_item_at_its_current_version():
    plan = HardDeletePlan("aaaaaaaaaa", (
        HardDeleteItem("aaaaaaaaaa", "pppppppppp", 3, False, ()),
        HardDeleteItem("bbbbbbbbbb", "pppppppppp", 1, True, ())), "0123456789abcdef")
    assert plan.expected == frozenset({("aaaaaaaaaa", 3), ("bbbbbbbbbb", 1)})
