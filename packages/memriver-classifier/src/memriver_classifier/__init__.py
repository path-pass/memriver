"""memriver-classifier: an optional check of new memory text before memriver writes it.

Installed with memriver[classifier] and switched on by a [classifier] table in
<root>/settings.toml; without both, memriver calls no classifier at all. The
umbrella builds the classifier and hands it to core's build_services; this package
implements core's ContentClassifier port and knows nothing of the umbrella or of
memriver dream.
"""

from .settings import ClassifierSettings, load_classifier_settings

__version__ = "0.1.0"
__all__ = ["ClassifierSettings", "__version__", "load_classifier_settings"]
