import memriver_core
import pytest
from memriver_core.models import errors
from memriver_core.models.errors import (
    BindingRefused,
    ContentRejected,
    GlobalReadOnly,
    IdCollision,
    MemoryError,
    MemoryNotFound,
    ProjectNotFound,
    ProjectUnavailable,
    StorageFailure,
    VersionConflict,
)

PUBLIC = [MemoryNotFound, ProjectNotFound, ContentRejected, ProjectUnavailable,
          GlobalReadOnly, StorageFailure, VersionConflict, BindingRefused]
SUBCLASSES = [*PUBLIC, IdCollision]
TAXONOMY = [MemoryError, *PUBLIC]


def test_base_does_not_subclass_builtin_key_error():
    assert not issubclass(MemoryError, KeyError)


@pytest.mark.parametrize("cls", SUBCLASSES)
def test_all_taxonomy_members_subclass_the_base(cls):
    assert issubclass(cls, MemoryError)


def test_not_found_errors_carry_only_their_id_field():
    assert MemoryNotFound("m").memory_id == "m"
    assert ProjectNotFound("p").project_id == "p"
    assert IdCollision("x").identifier == "x"


def test_id_collision_never_leaves_core():
    assert not hasattr(memriver_core, "IdCollision")


def test_storage_failure_and_global_read_only_take_no_arguments():
    assert str(StorageFailure()) == "storage failure"
    assert "read-only" in str(GlobalReadOnly())


@pytest.mark.parametrize("name", ["NameTaken", "UnreadableMemory", "InvalidScope"])
def test_identity_proposal_errors_are_gone(name):
    assert not hasattr(errors, name)
    assert not hasattr(memriver_core, name)


@pytest.mark.parametrize("cls", TAXONOMY)
def test_the_public_facade_reexports_the_same_class_objects(cls):
    assert getattr(memriver_core, cls.__name__) is cls


def test_version_conflict_and_binding_refused_carry_fields_not_words():
    assert VersionConflict("m").memory_id == "m"
    refused = BindingRefused("bound-elsewhere", "pppppppppp")
    assert (refused.reason, refused.project_id) == ("bound-elsewhere", "pppppppppp")
    assert BindingRefused("plan-changed").project_id is None


def test_project_unavailable_carries_a_reason_field_that_defaults_to_empty():
    assert ProjectUnavailable().reason == ""
    assert ProjectUnavailable(reason="candidate-changed").reason == "candidate-changed"


def test_binding_reasons_are_the_eleven_the_spec_lists():
    from memriver_core.models.errors import BINDING_REASONS
    assert BINDING_REASONS == {
        "not-a-directory", "covers-home", "covers-store", "inside-store", "bound-elsewhere",
        "unverifiable", "is-global", "has-directory", "plan-changed", "binding-changed",
        "no-such-project"}


def test_an_unknown_binding_reason_is_a_programming_error():
    with pytest.raises(ValueError):
        BindingRefused("because")


def test_memory_referenced_carries_fields_only():
    from memriver_core import MemoryReferenced

    err = MemoryReferenced("aaaaaaaaaa", ("bbbbbbbbbb", "cccccccccc"))
    assert (err.memory_id, err.derived_ids) == ("aaaaaaaaaa", ("bbbbbbbbbb", "cccccccccc"))
    assert str(err) == "memory referenced: aaaaaaaaaa"


def test_group_and_undo_conflicts_carry_fields_only():
    from memriver_core import GroupConflict, UndoConflict

    group = GroupConflict(None, ("aaaaaaaaaa",))
    undo = UndoConflict("bbbbbbbbbb", ("aaaaaaaaaa",))
    assert (group.change_id, group.ids) == (None, ("aaaaaaaaaa",))
    assert (undo.change_id, undo.ids) == ("bbbbbbbbbb", ("aaaaaaaaaa",))
