import memriver_core
import pytest
from memriver_core.application.errors import (
    ContentRejected,
    GlobalReadOnly,
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
    GlobalReadOnly,
    StorageFailure,
]

# the facade identity check also covers the base class: a transport catching
# MemoryError itself (a catch-all across the whole taxonomy) must get the
# same class object memriver_core.application.errors defines.
TAXONOMY = [MemoryError, *SUBCLASSES]


def _memory() -> Memory:
    return Memory.new(body="b", type="project", scope=Scope.global_(), source={})


def test_base_does_not_subclass_builtin_key_error():
    assert not issubclass(MemoryError, KeyError)


@pytest.mark.parametrize("cls", SUBCLASSES)
def test_all_taxonomy_members_subclass_the_base(cls):
    assert issubclass(cls, MemoryError)


def test_name_taken_carries_the_memory_holding_the_name():
    m = _memory()
    err = NameTaken("x", existing=m)
    assert err.memory_id == "x"
    assert err.existing is m


def test_name_taken_requires_the_memory_holding_the_name():
    # a name reservation only ever collides inside the caller's visible
    # scopes, so the memory holding the name is by construction one the caller
    # may see: there is no refusal left that has nothing to hand back, and an
    # adapter must not invent one by omitting the argument
    with pytest.raises(TypeError):
        NameTaken("x")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "make",
    [lambda: MemoryNotFound("some-name"),
     lambda: UnreadableMemory("some-name"),
     lambda: NameTaken("some-name", existing=_memory())],
    ids=["MemoryNotFound", "UnreadableMemory", "NameTaken"],
)
def test_storage_boundary_errors_carry_the_memory_id_as_a_field(make):
    assert make().memory_id == "some-name"


def test_global_read_only_message_is_fixed():
    from memriver_core import GlobalReadOnly, MemoryError

    err = GlobalReadOnly()
    assert isinstance(err, MemoryError)
    assert str(err) == "global memories are read-only to agents; no change was made"


def test_storage_failure_accepts_no_adapter_detail():
    # fieldless by construction: an adapter cannot attach a path, an errno or
    # a driver message that a transport might then echo to a client
    with pytest.raises(TypeError):
        StorageFailure("could not open /home/alice/store/.lock")  # type: ignore[call-arg]


@pytest.mark.parametrize("cls", TAXONOMY, ids=[c.__name__ for c in TAXONOMY])
def test_the_root_facade_re_exports_the_same_class(cls):
    # transports import the taxonomy from `memriver_core`, never from
    # `memriver_core.application.errors`; the facade must hand back the very
    # same class object so `except` clauses keep matching across both spellings
    assert getattr(memriver_core, cls.__name__) is cls
