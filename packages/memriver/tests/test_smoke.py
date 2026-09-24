from importlib.metadata import requires

import fastmcp
import memriver


def test_version():
    assert memriver.__version__ == "0.1.0"


def test_fastmcp_floor_matches_the_api_the_server_uses():
    """The server raises ToolError(message, log_level=...), which fastmcp
    3.0-3.2 does not accept as a keyword argument, so the floor must say so.

    Reads the installed distribution's own metadata rather than locating
    pyproject.toml via `memriver.__file__`, which only resolves for an
    editable workspace install -- a non-editable install (the published
    wheel) has no such path to walk up from.
    """
    deps = requires("memriver") or []
    fastmcp_req = next(d for d in deps if d.startswith("fastmcp"))
    assert fastmcp_req == "fastmcp>=3.4.7"
    assert int(fastmcp.__version__.split(".")[0]) >= 3
