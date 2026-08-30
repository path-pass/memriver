import memriver_core
import pytest
from memriver_core.application.errors import (
    ContentRejected,
    InvalidScope,
    MemoryError,
    MemoryNotFound,
    NameTaken,
    ProjectUnavailable,
    StorageFailure,
    UnreadableMemory,
)
from memriver_core.models import Memory, Scope

SUBCLASSES = [
    MemoryNotFound,
    UnreadableMemory,
    NameTaken,
    ContentRejected,
    InvalidScope,
    ProjectUnavailable,
    StorageFailure,
]


def test_base_does_not_subclass_builtin_key_error():
    assert not issubclass(MemoryError, KeyError)


@pytest.mark.parametrize("cls", SUBCLASSES)
def test_all_taxonomy_members_subclass_the_base(cls):
    assert issubclass(cls, MemoryError)


def test_name_taken_defaults_existing_to_none():
    err = NameTaken("x")
    assert err.memory_id == "x"
    assert err.existing is None


def test_name_taken_existing_none_explicit():
    err = NameTaken("x", existing=None)
    assert err.existing is None


def test_name_taken_existing_set():
    m = Memory.new(body="b", type="project", scope=Scope.global_(), source={})
    err = NameTaken("x", existing=m)
    assert err.existing is m


@pytest.mark.parametrize("cls", [MemoryNotFound, UnreadableMemory, NameTaken])
def test_storage_boundary_errors_carry_the_memory_id_as_a_field(cls):
    assert cls("some-name").memory_id == "some-name"


def test_storage_failure_accepts_no_adapter_detail():
    # fieldless by construction: an adapter cannot attach a path, an errno or
    # a driver message that a transport might then echo to a client
    with pytest.raises(TypeError):
        StorageFailure("could not open /home/alice/store/.lock")  # type: ignore[call-arg]


@pytest.mark.parametrize("cls", SUBCLASSES, ids=[c.__name__ for c in SUBCLASSES])
def test_the_root_facade_re_exports_the_same_class(cls):
    # transports import the taxonomy from `memriver_core`, never from
    # `memriver_core.application.errors`; the facade must hand back the very
    # same class object so `except` clauses keep matching across both spellings
    assert getattr(memriver_core, cls.__name__) is cls
