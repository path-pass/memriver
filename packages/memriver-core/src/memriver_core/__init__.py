"""The public core surface: the error taxonomy transports catch by name.

Re-exported here so a transport never reaches into ``models.errors``;
these are the same class objects, not copies.
"""

from .models.errors import (
    ContentRejected,
    GlobalReadOnly,
    MemoryError,
    MemoryNotFound,
    ProjectNotFound,
    ProjectUnavailable,
    StorageFailure,
)

__version__ = "0.1.0"

__all__ = [
    "ContentRejected",
    "GlobalReadOnly",
    "MemoryError",
    "MemoryNotFound",
    "ProjectNotFound",
    "ProjectUnavailable",
    "StorageFailure",
    "__version__",
]
