"""The content classifier, built into memriver and off without a [classifier] table.

`memriver serve` and `memriver dream run` build it from the table (memriver.settings
reads it) and hand it to core's build_services; `memriver doctor` states it in one line.
No table, or enabled = false: no classifier at all, so no model outside the user's
harness sees a memory. The hooks and the management commands never import this
package: they write no new text.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from ..executor import make_executor
from ..project_context import visible
from ..settings import (
    CLASSIFIER_SCRATCH_PREFIX,
    DEFAULT_JEV_MODEL,
    ClassifierSettings,
    load_classifier_settings,
)
from .adapter import Classifier

if TYPE_CHECKING:
    from memriver_core import ContentClassifier

__all__ = ["build_classifier", "classifier_state", "load_classifier"]


def build_classifier(table: ClassifierSettings | None,
                     env: Mapping[str, str]) -> ContentClassifier | None:
    """The configured classifier; None when there is no [classifier] table or it says
    enabled = false. `env` is what the executor's runs get and the jev key is read
    from (at each call)."""
    if table is None or not table.enabled:
        return None
    executor = make_executor(table, env=env, scratch_prefix=CLASSIFIER_SCRATCH_PREFIX)
    return Classifier(executor, timeout_s=table.timeout, block_threshold=table.block_threshold,
                      agent_writes=table.agent_writes, dream_writes=table.dream_writes)


def load_classifier(root: Path, *, env: Mapping[str, str]) -> ContentClassifier | None:
    """build_classifier for <root>/settings.toml. An invalid table raises SettingsError
    (one line, like every table)."""
    return build_classifier(load_classifier_settings(root), env)


def classifier_state(root: Path) -> str:
    """doctor's classifier line, after "classifier: "; it never calls a model."""
    table = load_classifier_settings(root)
    if table is None:
        return "not configured"
    if not table.enabled:
        return "off (enabled = false)"
    if table.executor == "jev":
        return (f"jev (model {visible(table.model or DEFAULT_JEV_MODEL)}, "
                f"key from {table.api_key_env})")
    return f"{table.executor} ({visible(table.executor_path)})"
