"""The memriver packages are released together, so one must not accept
just any future version of another: PyPI metadata cannot be edited after
release, so a loose ``>=`` bound on a sibling package would let a later,
incompatible release satisfy an old ``memriver==`` pin.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _pyproject(name: str) -> dict:
    return tomllib.loads((REPO / "packages" / name / "pyproject.toml").read_text())


def _version(name: str) -> str:
    return _pyproject(name)["project"]["version"]


def test_memriver_pins_its_own_packages_to_their_released_version():
    deps = _pyproject("memriver")["project"]["dependencies"]
    assert f"memriver-core=={_version('memriver-core')}" in deps
    assert f"memriver-dream=={_version('memriver-dream')}" in deps


def test_memriver_dream_pins_memriver_core_to_its_released_version():
    deps = _pyproject("memriver-dream")["project"]["dependencies"]
    assert f"memriver-core=={_version('memriver-core')}" in deps


def test_memriver_classifier_pins_memriver_core_to_its_released_version():
    deps = _pyproject("memriver-classifier")["project"]["dependencies"]
    assert f"memriver-core=={_version('memriver-core')}" in deps
