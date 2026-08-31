"""The public core surface: the error taxonomy transports catch by name.

Re-exported here so a transport never reaches into ``application.errors``;
these are the same class objects, not copies, so ``except`` clauses match
whichever spelling a caller used.
"""

from .application.errors import (
    ContentRejected,
    InvalidScope,
    MemoryError,
    MemoryNotFound,
    NameTaken,
    ProjectUnavailable,
    StorageFailure,
    UnreadableMemory,
)

__version__ = "0.1.0"

__all__ = [
    "ContentRejected",
    "InvalidScope",
    "MemoryError",
    "MemoryNotFound",
    "NameTaken",
    "ProjectUnavailable",
    "StorageFailure",
    "UnreadableMemory",
    "__version__",
]
