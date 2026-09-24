from memriver_core.models import Project, ReadWriteSet, Resolution, Session


def test_resolution_defaults():
    assert Resolution("none") == Resolution(state="none", project=None, diagnostic=None)


def test_session_carries_state_header_and_read_write_set():
    read_write_set = ReadWriteSet(project_id="aaaaaaaaaa", global_project_id="gggggggggg")
    project = Project(id="aaaaaaaaaa", name="demo", root="/w")
    session = Session("registered", "project: demo", read_write_set, project)
    assert (session.state, session.header, session.project, session.diagnostic) == \
        ("registered", "project: demo", project, None)
