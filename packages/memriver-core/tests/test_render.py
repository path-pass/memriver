from memriver_core.entry import Entry
from memriver_core.render import render_index

SOURCE = {"harness": "test", "session": "s", "method": "agent"}


def test_render_lines_and_budget(store):
    for i in range(5):
        store.write(Entry.new(body=f"记忆条目内容 {i}", type="project",
                              scope="global", source=SOURCE))
    out = render_index(store, scopes=["global"], budget_lines=3)
    lines = out.splitlines()
    assert len(lines) == 4  # 3 entries + 1 omitted-notice line
    assert lines[0].startswith("- [project] ")
    assert "2 more entries omitted; use memory_search" in lines[-1]
    assert "index_budget_lines" not in lines[-1]  # agents cannot change this knob

def test_render_empty(store):
    assert "no memories yet" in render_index(store, scopes=["global"])


def test_render_tolerates_empty_body(store):
    # entry files are hand-editable, so an empty body can reach the store
    # without passing through the write gate; it must not break the index
    e = Entry.new(body="placeholder", type="project", scope="global", source=SOURCE)
    e.body = ""
    store.write(e)
    out = render_index(store, scopes=["global"])
    assert e.id in out


def test_render_orders_newest_first_with_ulid_tiebreak(store):
    a = Entry.new(body="older entry", type="project", scope="global", source=SOURCE)
    b = Entry.new(body="tied but larger id", type="project", scope="global", source=SOURCE)
    c = Entry.new(body="newest entry", type="project", scope="global", source=SOURCE)
    a.id = "01" + "A" * 24; a.updated = "2026-08-01T00:00:00Z"
    b.id = "01" + "B" * 24; b.updated = "2026-08-01T00:00:00Z"
    c.id = "01" + "C" * 24; c.updated = "2026-08-02T00:00:00Z"
    for e in (a, b, c):
        store.write(e)
    lines = render_index(store, scopes=["global"]).splitlines()
    assert "newest entry" in lines[0]
    assert "tied but larger id" in lines[1]  # ULID tiebreak within a timestamp tie
    assert "older entry" in lines[2]


def test_line_leads_with_name(store):
    store.write(Entry.new(body="runtimes are managed by mise", type="user",
                          scope="global", source=SOURCE, id="mise-runtimes"))
    out = render_index(store, scopes=["global"])
    assert out.splitlines()[0].startswith("- [user] mise-runtimes: runtimes")
