import re

import pytest
from memriver_core.entry import Entry

SOURCE = {"harness": "claude-code", "session": "s1", "method": "agent"}

def test_new_generates_ulid_and_timestamps():
    e = Entry.new(body="用户偏好中文回复", type="user", scope="global", source=SOURCE)
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", e.id)  # ULID
    assert e.created == e.updated
    assert e.created.endswith("Z") or "+" in e.created
    assert e.sync is True and e.trust == "agent"

def test_markdown_roundtrip():
    e = Entry.new(body="line1\n\nline2", type="project", scope="project:demo-abc123", source=SOURCE)
    text = e.to_markdown()
    assert text.startswith("---\n")
    e2 = Entry.from_markdown(text)
    assert e2 == e

def test_invalid_type_rejected():
    with pytest.raises(ValueError):
        Entry.new(body="x", type="task", scope="global", source=SOURCE)


def test_new_accepts_caller_id():
    e = Entry.new(body="b", type="project", scope="global",
                  source={}, id="my-slug")
    assert e.id == "my-slug"


def test_new_generates_ulid_without_id():
    e = Entry.new(body="b", type="project", scope="global", source={})
    assert len(e.id) == 26


def test_old_types_rejected():
    with pytest.raises(ValueError):
        Entry.new(body="b", type="preference", scope="global", source={})


def test_unknown_type_reads_as_project():
    e = Entry.new(body="b", type="user", scope="global", source={})
    text = e.to_markdown().replace("type: user", "type: lesson")
    assert Entry.from_markdown(text).type == "project"


def test_unknown_keys_ignored_on_read():
    e = Entry.new(body="b", type="user", scope="global", source={})
    text = e.to_markdown().replace("id:", "unknown_key: X\nid:")
    loaded = Entry.from_markdown(text)
    assert not hasattr(loaded, "unknown_key")


def test_description_roundtrips_and_is_stripped():
    e = Entry.new(body="b", type="user", scope="global", source={},
                  description="  a one-line recall cue  ")
    assert e.description == "a one-line recall cue"
    text = e.to_markdown()
    assert "description: a one-line recall cue" in text
    e2 = Entry.from_markdown(text)
    assert e2 == e


def test_description_defaults_empty_and_is_always_in_frontmatter():
    e = Entry.new(body="b", type="user", scope="global", source={})
    assert e.description == ""
    assert "description:" in e.to_markdown()


def test_old_files_without_description_parse_as_empty():
    e = Entry.new(body="b", type="user", scope="global", source={})
    text = e.to_markdown()
    # simulate a pre-existing file written before 'description' existed
    text = "\n".join(line for line in text.splitlines(keepends=False)
                     if not line.startswith("description:")) + "\n"
    loaded = Entry.from_markdown(text)
    assert loaded.description == ""
