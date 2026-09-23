import memriver_core
import pytest
from memriver_core.models import errors
from memriver_core.models.errors import (
    ContentRejected,
    GlobalReadOnly,
    IdCollision,
    MemoryError,
    MemoryNotFound,
    ProjectNotFound,
    ProjectUnavailable,
    StorageFailure,
)

PUBLIC = [MemoryNotFound, ProjectNotFound, ContentRejected, ProjectUnavailable,
          GlobalReadOnly, StorageFailure]
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
