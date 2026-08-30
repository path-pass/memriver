import pytest
from memriver_core.models import Memory, ProjectId, Scope
from memriver_core.repository.filesystem.markdown_codec import (
    UnparsableStoredScope,
    decode,
    encode,
)

SOURCE = {"harness": "claude-code", "session": "s1", "method": "agent"}


def _m(**kw):
    kw.setdefault("body", "b")
    kw.setdefault("type", "user")
    kw.setdefault("scope", Scope.global_())
    kw.setdefault("source", SOURCE)
    return Memory.new(**kw)


def test_markdown_roundtrip():
    m = _m(body="line1\n\nline2", type="project",
           scope=Scope.project(ProjectId("demo-abc123")))
    text = encode(m)
    assert text.startswith("---\n")
    assert decode(text) == m


def test_scope_is_written_as_its_storage_string():
    assert "scope: global" in encode(_m())
    assert "scope: project:demo-abc123" in encode(
        _m(scope=Scope.project(ProjectId("demo-abc123"))))


def test_scope_is_decoded_back_into_a_value_object():
    assert decode(encode(_m())).scope == Scope.global_()
    project = Scope.project(ProjectId("demo-abc123"))
    assert decode(encode(_m(scope=project))).scope == project


@pytest.mark.parametrize("raw", ["nonsense", '""', '"project:"', "123"])
def test_ungrammatical_stored_scope_is_reported_as_a_scope_failure(raw):
    # readers must be able to tell this apart from an undecodable file: such a
    # file reads as absent (scope mismatch), never as "unreadable"
    text = encode(_m()).replace("scope: global", f"scope: {raw}")
    with pytest.raises(UnparsableStoredScope):
        decode(text)


@pytest.mark.parametrize("text", [
    "---\nid: [unclosed\n---\nbody\n",          # broken YAML
    "just some hand-written notes\n",           # no frontmatter at all
    "---\nid: n\ntype: user\n---\nbody\n",      # frontmatter keys missing
])
def test_other_broken_files_stay_plain_decode_failures(text):
    with pytest.raises(Exception) as excinfo:
        decode(text)
    assert not isinstance(excinfo.value, UnparsableStoredScope)


def test_unknown_type_reads_as_project():
    text = encode(_m()).replace("type: user", "type: lesson")
    assert decode(text).type == "project"


def test_unknown_keys_ignored_on_read():
    text = encode(_m()).replace("id:", "unknown_key: X\nid:")
    assert not hasattr(decode(text), "unknown_key")


def test_description_roundtrips_and_is_stripped():
    m = _m(description="  a one-line recall cue  ")
    assert m.description == "a one-line recall cue"
    text = encode(m)
    assert "description: a one-line recall cue" in text
    assert decode(text) == m


def test_description_defaults_empty_and_is_always_in_frontmatter():
    m = _m()
    assert m.description == ""
    assert "description:" in encode(m)


def test_old_files_without_description_parse_as_empty():
    text = encode(_m())
    # simulate a pre-existing file written before 'description' existed
    text = "\n".join(line for line in text.splitlines(keepends=False)
                     if not line.startswith("description:")) + "\n"
    assert decode(text).description == ""
