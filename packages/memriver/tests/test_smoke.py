import tomllib
from pathlib import Path

import fastmcp
import memriver


def test_version():
    assert memriver.__version__ == "0.1.0"


def test_fastmcp_floor_matches_the_api_the_server_uses():
    """The server relies on fastmcp 3.x behaviour, so the floor must say so."""
    pyproject = Path(memriver.__file__).parents[2] / "pyproject.toml"
    deps = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"]
    fastmcp_req = next(d for d in deps if d.startswith("fastmcp"))
    assert fastmcp_req == "fastmcp>=3.0"
    assert int(fastmcp.__version__.split(".")[0]) >= 3
