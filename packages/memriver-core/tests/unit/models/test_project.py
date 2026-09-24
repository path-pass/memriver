import pytest
from memriver_core.models import ID_RE, Project, RootPlan, UnbindPlan, project_name


def test_new_generates_an_id_and_normalizes_the_name():
    project = Project.new("  my\nwork  ", max_chars=120)
    assert ID_RE.fullmatch(project.id)
    assert (project.name, project.root) == ("my work", None)


def test_project_is_frozen():
    project = Project.new("demo", max_chars=120)
    with pytest.raises(AttributeError):
        project.name = "other"  # type: ignore[misc]


@pytest.mark.parametrize("raw", ["", "   ", "\n\t"])
def test_an_empty_name_is_refused(raw):
    with pytest.raises(ValueError, match="empty"):
        project_name(raw, 120)


def test_a_name_longer_than_the_cap_is_refused():
    assert project_name("x" * 7, 7) == "x" * 7
    with pytest.raises(ValueError, match="7"):
        project_name("x" * 8, 7)


def test_names_are_not_identities():
    assert Project.new("same", max_chars=120).id != Project.new("same", max_chars=120).id


def test_the_name_cap_is_not_a_models_constant():
    from memriver_core import models
    assert not hasattr(models, "PROJECT_NAME_MAX_CHARS")


def test_plans_are_frozen_values():
    plan = RootPlan(root="/w", store="/s", nested=(), already_bound=False)
    unbind = UnbindPlan(project_id="aaaaaaaaaa", root="/w", store="/s")
    with pytest.raises(AttributeError):
        plan.root = "/x"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        unbind.root = "/x"  # type: ignore[misc]
