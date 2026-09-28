"""memriver-classifier: an optional check of new memory text before memriver writes it.

Installed with memriver[classifier] and switched on by a [classifier] table in
<root>/settings.toml; without both, memriver calls no classifier at all. The
umbrella builds the classifier and hands it to core's build_services; this package
implements core's ContentClassifier port and knows nothing of the umbrella or of
memriver dream.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from .backends import Classifier, backend_for
from .settings import ClassifierSettings, load_classifier_settings

if TYPE_CHECKING:
    from memriver_core import ContentClassifier

__version__ = "0.1.0"
__all__ = ["ClassifierSettings", "__version__", "build_classifier",
           "load_classifier_settings"]


def build_classifier(table: ClassifierSettings | None,
                     env: Mapping[str, str]) -> ContentClassifier | None:
    """The configured classifier; None when there is no [classifier] table or it says
    enabled = false. `env` is the environment the harness runs get and the jev key is
    read from (at each call)."""
    if table is None or not table.enabled:
        return None
    return Classifier(backend_for(table, env).check, agent_writes=table.agent_writes,
                      dream_writes=table.dream_writes)
