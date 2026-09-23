from pathlib import Path

from memriver.project_context import ProjectResolution, bind, header_field, resolve
from memriver.session import NONE_HEADER, STORE_UNREADABLE_HEADER, open_session
from memriver_core.bootstrap import build_service
from memriver_core.config import Settings
from memriver_core.models import ReadWriteSet, new_id


def _service(store: Path):
    return build_service(Settings(root=store), root=store)


def test_a_registered_existing_project_names_itself_and_reads_global(tmp_path):
    store, work = tmp_path / "store", tmp_path / "work"
    work.mkdir()
    service = _service(store)
    global_id = service.ensure_global()
    project = service.create_project("demo")
    bind(store, service, project.id, str(work.resolve()))
    session = open_session(service, resolve(store, work))
    # the root field is capped like every header field; a long macOS TMPDIR is
    # longer than the cap, so the expectation goes through the same function
    assert session.header == f"project: demo [{project.id}] (root {header_field(str(work.resolve()))})"
    assert session.read_write_set == ReadWriteSet(project_id=project.id, global_project_id=global_id)
    assert session.state == "registered"
    assert session.project == project


def test_a_registered_id_missing_from_the_store_is_unavailable_and_writes_nothing(tmp_path):
    store = tmp_path / "store"
    service = _service(store)
    global_id = service.ensure_global()
    missing = new_id()
    session = open_session(service, ProjectResolution("registered", missing, "/x", None))
    assert session.header == (f"project: unavailable — registered project {missing} does not "
                              "exist; ask the user to run memriver project explain")
    assert session.read_write_set == ReadWriteSet(project_id=None, global_project_id=global_id)
    assert session.state == "missing"


def test_a_project_that_vanishes_between_the_two_reads_is_missing_without_write_rights():
    from memriver_core import ProjectNotFound

    project_id, global_id = new_id(), new_id()

    class Racing:
        def read_write_set(self, requested):
            return ReadWriteSet(project_id=requested, global_project_id=global_id)

        def read_project(self, requested):
            raise ProjectNotFound(requested)

    session = open_session(Racing(), ProjectResolution("registered", project_id, "/x", None))
    assert session.state == "missing"
    assert session.read_write_set == ReadWriteSet(project_id=None, global_project_id=global_id)
    assert session.read_write_set.writable() == frozenset()
    assert "does not exist" in session.header


def test_no_project_and_degraded_headers(tmp_path):
    service = _service(tmp_path / "store")
    none = open_session(service, ProjectResolution("none", None, None, None))
    assert (none.header, none.state) == (NONE_HEADER, "none")
    degraded = open_session(service, ProjectResolution("degraded", None, None,
                                                       "registry/x.toml: bad"))
    assert degraded.header == ("project: unavailable — registry invalid (registry/x.toml: bad); "
                               "ask the user to run memriver project explain")
    assert degraded.read_write_set == ReadWriteSet(project_id=None, global_project_id=None)


def test_an_invalid_manifest_degrades_to_an_empty_read_write_set_instead_of_failing(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    (store / "store.toml").write_text("global_project = 'nope'\n")
    session = open_session(_service(store), ProjectResolution("registered", new_id(), "/x", None))
    assert session.header == STORE_UNREADABLE_HEADER
    assert session.read_write_set == ReadWriteSet(project_id=None, global_project_id=None)
    assert session.state == "unavailable"


def test_header_fields_are_single_line_and_capped(tmp_path):
    store = tmp_path / "store"
    service = _service(store)
    project = service.create_project("x" * 120)
    session = open_session(service, ProjectResolution("registered", project.id,
                                                      "/x/\nevil " + "a" * 300, None))
    assert "\n" not in session.header
    root_part = session.header.split("(root ", 1)[1][:-1]
    assert root_part.startswith("/x/ evil a") and len(root_part) == 120
