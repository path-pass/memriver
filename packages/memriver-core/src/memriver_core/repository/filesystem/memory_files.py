"""Memory-file reading: locate and decode one memory, or scan every memory file."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

from memriver_core.models import ID_RE, Memory
from memriver_core.models.errors import StorageFailure

from .files import MEMORIES_DIRNAME, data_dir_exists, memory_path, read_regular_text
from .markdown_codec import decode

logger = logging.getLogger(__name__)


def _read_memory_file(root: Path, memory_id: str) -> Memory | None:
    """Read and decode one memory file, assuming the container was already checked.

    Same damage rules as `load_memory` (not a regular file, unreadable,
    undecodable, or naming a different id -- all StorageFailure), minus the
    `memories/` container check: a caller that checks the container once for
    a whole scan uses this directly, per file, instead of `load_memory`.
    """
    try:
        text = read_regular_text(memory_path(root, memory_id))
    except (OSError, UnicodeDecodeError) as err:
        raise StorageFailure from err
    if text is None:
        return None
    try:
        memory = decode(text)
    except Exception as err:
        raise StorageFailure from err
    if memory.id != memory_id:
        raise StorageFailure
    return memory


def load_memory(root: Path, memory_id: str) -> Memory | None:
    """The memory stored under `memory_id`; None when nothing is stored there.

    A malformed id and an absent file are "nothing". A file that is present
    but unusable -- not a regular file, unreadable, undecodable, or naming a
    different id -- is StorageFailure: damage is reported as damage, never
    disguised as absence (`doctor` names the file). An unsafe `memories/`
    container is StorageFailure too.
    """
    if not ID_RE.fullmatch(memory_id):
        return None
    try:
        if not data_dir_exists(root, MEMORIES_DIRNAME):
            return None
    except OSError as err:
        raise StorageFailure from err
    return _read_memory_file(root, memory_id)


def iter_memories(root: Path) -> Iterator[Memory]:
    """Every usable memory in the store, in file-name order.

    One damaged file does not fail a scan: it is skipped with a path-free log
    line and `doctor` reports it. A file gone since the listing (deleted
    between the scan's directory listing and its read) is likewise skipped,
    same as any other absence. An unsafe or unlistable `memories/` directory
    fails the whole scan with StorageFailure.

    The container is checked once, up front; per-file reads do not repeat
    that check. A container swapped out from under a scan already in
    progress (e.g. `memories/` replaced by a symlink mid-scan) is not
    detected within that scan: the next scan's up-front check catches it
    then.
    """
    try:
        if not data_dir_exists(root, MEMORIES_DIRNAME):
            return
        names = sorted(entry.name for entry in os.scandir(root / MEMORIES_DIRNAME))
    except OSError as err:
        raise StorageFailure from err
    for name in names:
        memory_id = name[: -len(".md")] if name.endswith(".md") else ""
        if not ID_RE.fullmatch(memory_id):
            continue
        try:
            memory = _read_memory_file(root, memory_id)
        except StorageFailure:
            logger.warning("skipping unusable memory file %s", memory_id)
            continue
        if memory is not None:
            yield memory
