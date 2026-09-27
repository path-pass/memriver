"""The public core surface: the error taxonomy transports catch by name.

Re-exported here so a transport never reaches into ``models.errors``;
these are the same class objects, not copies.
"""

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
    StorageFailure,
    StoreNeedsUpgrade,
    UndoRefused,
    VersionConflict,
)

__version__ = "0.1.0"

__all__ = [
    "BatchConflict",
    "BindingRefused",
    "ContentRejected",
    "GlobalReadOnly",
    "MemoryError",
    "MemoryNotFound",
    "PlanChanged",
    "ProjectNotFound",
    "ProjectUnavailable",
    "StorageFailure",
    "StoreNeedsUpgrade",
    "UndoRefused",
    "VersionConflict",
    "__version__",
]
