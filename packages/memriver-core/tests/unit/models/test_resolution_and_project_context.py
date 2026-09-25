from memriver_core.models import Project, ProjectContext, ReadWriteSet, Resolution


def test_resolution_defaults():
    assert Resolution("none") == Resolution(state="none", project=None, diagnostic=None)


def test_project_context_carries_state_header_and_read_write_set():
    read_write_set = ReadWriteSet(project_id="aaaaaaaaaa", global_project_id="gggggggggg")
    project = Project(id="aaaaaaaaaa", name="demo", root="/w")
    project_context = ProjectContext("registered", "project: demo", read_write_set, project)
    assert (project_context.state, project_context.header, project_context.project,
           project_context.diagnostic) == ("registered", "project: demo", project, None)
