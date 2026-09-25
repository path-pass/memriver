from importlib.metadata import requires

import fastmcp
import memriver


def test_version():
    assert memriver.__version__ == "0.1.0"


def test_fastmcp_floor_and_ceiling_match_the_api_the_server_uses():
    """The floor: the server raises ToolError(message, log_level=...), which
    fastmcp 3.0-3.2 does not accept as a keyword argument. The ceiling: a
    plain install without uv.lock must still resolve the major version this
    package is tested against (Task 14 / R8) -- `_meta_as_dict` reads request
    meta tolerantly across fastmcp 3's pydantic model and fastmcp 4's plain
    dict, but the dependency stays capped so an unlocked install matches
    what CI actually exercises.

    Reads the installed distribution's own metadata rather than locating
    pyproject.toml via `memriver.__file__`, which only resolves for an
    editable workspace install -- a non-editable install (the published
    wheel) has no such path to walk up from.
    """
    deps = requires("memriver") or []
    fastmcp_req = next(d for d in deps if d.startswith("fastmcp"))
    assert fastmcp_req == "fastmcp<4,>=3.4.7"
    major = int(fastmcp.__version__.split(".")[0])
    assert major >= 3
    assert major < 4
