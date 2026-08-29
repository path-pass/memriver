import re

import pytest
from memriver_core.entry import Entry

SOURCE = {"harness": "claude-code", "session": "s1", "method": "agent"}

def test_new_generates_ulid_and_timestamps():
    e = Entry.new(body="用户偏好中文回复", type="preference", scope="global", source=SOURCE)
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", e.id)  # ULID
    assert e.created == e.updated
    assert e.created.endswith("Z") or "+" in e.created
    assert e.sync is True and e.trust == "agent" and e.superseded_by is None

def test_markdown_roundtrip():
    e = Entry.new(body="line1\n\nline2", type="fact", scope="project:demo-abc123", source=SOURCE)
    text = e.to_markdown()
    assert text.startswith("---\n")
    e2 = Entry.from_markdown(text)
    assert e2 == e

def test_invalid_type_rejected():
    with pytest.raises(ValueError):
        Entry.new(body="x", type="task", scope="global", source=SOURCE)
