"""The public core surface: the error taxonomy transports catch by name.

Re-exported here so a transport never reaches into ``models.errors``;
these are the same class objects, not copies.
"""

from .models.errors import (
    BindingRefused,
    ContentRejected,
    GlobalReadOnly,
    GroupConflict,
    MemoryError,
    MemoryNotFound,
    MemoryReferenced,
    ProjectNotFound,
    ProjectUnavailable,
    StorageFailure,
    UndoConflict,
    VersionConflict,
)

__version__ = "0.1.0"

__all__ = [
    "BindingRefused",
    "ContentRejected",
    "GlobalReadOnly",
    "GroupConflict",
    "MemoryError",
    "MemoryNotFound",
    "MemoryReferenced",
    "ProjectNotFound",
    "ProjectUnavailable",
    "StorageFailure",
    "UndoConflict",
    "VersionConflict",
    "__version__",
]
