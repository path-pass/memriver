from memriver_core.entry import Entry
from memriver_core.render import render_index

SOURCE = {"harness": "test", "session": "s", "method": "agent"}


def test_render_lines_and_budget(store):
    for i in range(5):
        store.write(Entry.new(body=f"记忆条目内容 {i}", type="fact",
                              scope="global", source=SOURCE))
    out = render_index(store, scopes=["global"], budget_lines=3)
    lines = out.splitlines()
    assert len(lines) == 4  # 3 entries + 1 omitted-notice line
    assert lines[0].startswith("- [fact] ")
    assert "2 more entries omitted" in lines[-1]

def test_render_empty(store):
    assert "no memories yet" in render_index(store, scopes=["global"])
