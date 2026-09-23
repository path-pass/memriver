from memriver_core.models import ReadWriteSet

P, G = "aaaaaaaaaa", "gggggggggg"


def test_a_project_session_reads_both_and_writes_its_project():
    read_write_set = ReadWriteSet(project_id=P, global_project_id=G)
    assert read_write_set.readable() == {P, G}
    assert read_write_set.writable() == {P}


def test_no_project_reads_global_only_and_writes_nothing():
    read_write_set = ReadWriteSet(project_id=None, global_project_id=G)
    assert read_write_set.readable() == {G}
    assert read_write_set.writable() == frozenset()


def test_an_uninitialized_store_reads_only_the_project():
    read_write_set = ReadWriteSet(project_id=P, global_project_id=None)
    assert read_write_set.readable() == {P}
    assert read_write_set.writable() == {P}


def test_global_is_never_writable_even_if_named_as_the_project():
    read_write_set = ReadWriteSet(project_id=G, global_project_id=G)
    assert read_write_set.readable() == {G}
    assert read_write_set.writable() == frozenset()


def test_an_empty_set_reads_and_writes_nothing():
    read_write_set = ReadWriteSet(project_id=None, global_project_id=None)
    assert read_write_set.readable() == frozenset() and read_write_set.writable() == frozenset()
