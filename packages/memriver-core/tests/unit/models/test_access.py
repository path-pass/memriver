from memriver_core.models import AccessContext

P, G = "aaaaaaaaaa", "gggggggggg"


def test_a_project_session_reads_both_and_writes_its_project():
    ctx = AccessContext(project_id=P, global_project_id=G)
    assert ctx.readable() == {P, G}
    assert ctx.writable() == {P}


def test_no_project_reads_global_only_and_writes_nothing():
    ctx = AccessContext(project_id=None, global_project_id=G)
    assert ctx.readable() == {G}
    assert ctx.writable() == frozenset()


def test_an_uninitialized_store_reads_only_the_project():
    ctx = AccessContext(project_id=P, global_project_id=None)
    assert ctx.readable() == {P}
    assert ctx.writable() == {P}


def test_global_is_never_writable_even_if_named_as_the_project():
    ctx = AccessContext(project_id=G, global_project_id=G)
    assert ctx.readable() == {G}
    assert ctx.writable() == frozenset()


def test_an_empty_context_reads_and_writes_nothing():
    ctx = AccessContext(project_id=None, global_project_id=None)
    assert ctx.readable() == frozenset() and ctx.writable() == frozenset()
