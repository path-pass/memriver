import pytest
from memriver_core.models import ID_RE, PROJECT_NAME_MAX_CHARS, Project, project_name


def test_new_generates_an_id_and_normalizes_the_name():
    project = Project.new("  my\nwork  ")
    assert ID_RE.fullmatch(project.id)
    assert project.name == "my work"


def test_project_is_frozen():
    project = Project.new("demo")
    with pytest.raises(AttributeError):
        project.name = "other"  # type: ignore[misc]


@pytest.mark.parametrize("raw", ["", "   ", "\n\t"])
def test_an_empty_name_is_refused(raw):
    with pytest.raises(ValueError, match="empty"):
        project_name(raw)


def test_a_name_longer_than_the_cap_is_refused():
    assert project_name("x" * PROJECT_NAME_MAX_CHARS) == "x" * PROJECT_NAME_MAX_CHARS
    with pytest.raises(ValueError, match="120"):
        project_name("x" * (PROJECT_NAME_MAX_CHARS + 1))


def test_names_are_not_identities():
    assert Project.new("same").id != Project.new("same").id
