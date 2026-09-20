import pytest
from memriver_core.models import PROJECT_ID_RE, single_line


@pytest.mark.parametrize("value", ["work-0123456789abcdef", "a", "coding-agent-common-memory-e0d205"])
def test_project_id_accepts_kebab_ids(value):
    assert PROJECT_ID_RE.fullmatch(value)


@pytest.mark.parametrize("value", ["", "-a", "A", "a_b", "a/b", "a b", "a\n"])
def test_project_id_rejects_other_shapes(value):
    assert PROJECT_ID_RE.fullmatch(value) is None


def test_single_line_collapses_control_characters():
    assert single_line("a\nb c\x00d   e") == "a b c d e"


def test_single_line_collapses_unicode_line_separators():
    assert single_line("a b c") == "a b c"
