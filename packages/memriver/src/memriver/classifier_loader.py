"""The optional memriver-classifier package, as the umbrella uses it.

`memriver serve` and `memriver dream run` build the content classifier here, and
`memriver doctor` states it. It is built only when the package is importable and a
[classifier] table exists; a table without the package is reported (one warning, one
doctor line), never silently ignored. The hooks and the management commands never
import this module: they write no new text.
"""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from memriver_core.settings import SettingsError, settings_file

from .project_context import visible

if TYPE_CHECKING:
    from memriver_core import ContentClassifier

logger = logging.getLogger("memriver")

TABLE_WITHOUT_PACKAGE = ("a [classifier] table is set in settings.toml but "
                         "memriver-classifier is not installed; writes are not classified")


def _package() -> ModuleType | None:
    """memriver_classifier, or None when it is not installed. A missing module inside an
    installed package is raised, never taken for "not installed": that would switch a
    configured classifier off without a word."""
    try:
        import memriver_classifier
    except ModuleNotFoundError as err:
        if err.name != "memriver_classifier":
            raise
        return None
    return memriver_classifier


def _has_table(root: Path) -> bool:
    """Whether settings.toml has a [classifier] table -- read without the package."""
    path = settings_file(root)
    if path is None:
        return False
    try:
        return "classifier" in tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SettingsError(unreadable=True) from None


def load_classifier(root: Path, *, env: Mapping[str, str]) -> ContentClassifier | None:
    """The configured classifier, or None: the package absent, no table, or enabled =
    false. An invalid table raises SettingsError (one line, like every table)."""
    package = _package()
    if package is None:
        if _has_table(root):
            logger.warning("memriver: %s", TABLE_WITHOUT_PACKAGE)
        return None
    return package.build_classifier(package.load_classifier_settings(root), env)


def classifier_state(root: Path) -> str:
    """doctor's classifier line, after "classifier: "; it never calls a model."""
    package = _package()
    if package is None:
        return (f"not installed; {TABLE_WITHOUT_PACKAGE}" if _has_table(root)
                else "not installed")
    table = package.load_classifier_settings(root)
    if table is None:
        return "installed, not configured"
    if not table.enabled:
        return "off (enabled = false)"
    if table.backend == "jev":
        return f"jev (model {visible(table.jev_model)}, key from {table.api_key_env})"
    return f"{table.backend} ({visible(table.executor_path)})"
