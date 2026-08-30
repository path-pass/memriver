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
    assert err.existing is None


def test_name_taken_existing_none_explicit():
    err = NameTaken("x", existing=None)
    assert err.existing is None


def test_name_taken_existing_set():
    m = Memory.new(body="b", type="project", scope=Scope.global_(), source={})
    err = NameTaken("x", existing=m)
    assert err.existing is m
