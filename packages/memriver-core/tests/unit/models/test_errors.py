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


def test_content_rejected_carries_its_rule_and_memory_as_fields():
    from memriver_core.models.errors import ContentRejected as Rejected
    err = Rejected(rule_id="github-pat", memory_id="mmmmmmmmmm")
    assert (err.rule_id, err.memory_id) == ("github-pat", "mmmmmmmmmm")
    assert "github-pat" in str(err)
    worded = Rejected("content is empty; nothing to store", rule_id="empty")
    assert (str(worded), worded.rule_id, worded.memory_id) == \
        ("content is empty; nothing to store", "empty", None)


def test_batch_conflict_carries_index_memory_and_one_known_reason():
    from memriver_core.models.errors import BATCH_CONFLICT_REASONS, BatchConflict
    assert BATCH_CONFLICT_REASONS == {"version", "deleted", "same-state", "source", "cycle",
                                      "read-since", "missing"}
    err = BatchConflict(2, "mmmmmmmmmm", "version")
    assert (err.index, err.memory_id, err.reason) == (2, "mmmmmmmmmm", "version")
    assert BatchConflict(0, None, "source").memory_id is None
    with pytest.raises(ValueError):
        BatchConflict(0, None, "because")


def test_store_needs_upgrade_carries_the_store_version():
    from memriver_core.models.errors import StoreNeedsUpgrade
    assert StoreNeedsUpgrade(2).version == 2


@pytest.mark.parametrize("name", ["BatchConflict", "StoreNeedsUpgrade"])
def test_the_new_errors_are_public(name):
    from memriver_core import models
    assert getattr(memriver_core, name) is getattr(errors, name) is getattr(models, name)
    assert name in memriver_core.__all__ and name in models.__all__
