"""The public core surface: the error taxonomy transports catch by name, and the
content classifier port a plug-in implements.

Re-exported here so a transport never reaches into ``models.errors``;
these are the same class objects, not copies.
"""

from .content_policy.protocol import ContentClassifier, Verdict
from .models.errors import (
    BatchConflict,
    BindingRefused,
    ContentRejected,
    GlobalReadOnly,
    MemoryError,
    MemoryNotFound,
    PlanChanged,
    ProjectNotFound,
    ProjectUnavailable,
    SessionMoved,
    StorageFailure,
    StoreNeedsUpgrade,
    UndoRefused,
    VersionConflict,
)

__version__ = "0.1.0"

__all__ = [
    "BatchConflict",
    "BindingRefused",
    "ContentClassifier",
    "ContentRejected",
    "GlobalReadOnly",
    "MemoryError",
    "MemoryNotFound",
    "PlanChanged",
    "ProjectNotFound",
    "ProjectUnavailable",
    "SessionMoved",
    "StorageFailure",
    "StoreNeedsUpgrade",
    "UndoRefused",
    "Verdict",
    "VersionConflict",
    "__version__",
]
