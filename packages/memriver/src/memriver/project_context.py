"""Git project detection, the umbrella's job.

The core knows a project only as a `ProjectId`; deciding which directory is a
project, and what it is called, is transport-side context resolution and lives
here so nothing in the core probes for `.git` or hashes paths.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from memriver_core.models import AccessContext, ProjectId


def find_git_root(start: Path) -> Path | None:
    """The nearest ``.git`` root at or above ``start``, or ``None`` outside a repo."""
    cur = start.resolve()
    for p in [cur, *cur.parents]:
        if (p / ".git").exists():
            return p
    return None


_MAX_FILENAME_BYTES = 255


def _fit_filename_bytes(name: str, suffix: str, limit: int = _MAX_FILENAME_BYTES) -> str:
    """Truncate `name` so `name + suffix` fits in `limit` UTF-8 bytes.

    Cuts on a UTF-8 byte boundary (never mid-codepoint): a raw byte slice can
    split a multi-byte character, so the tail is decoded with `errors="ignore"`
    to drop whatever incomplete bytes that left behind.
    """
    budget = limit - len(suffix.encode())
    encoded = name.encode()
    if len(encoded) <= budget:
        return name
    return encoded[:budget].decode("utf-8", errors="ignore")


def project_slug(project_dir: Path) -> str | None:
    root = find_git_root(project_dir)
    if root is None:
        return None
    name = re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-") or "project"
    digest = hashlib.sha1(str(root).encode()).hexdigest()[:6]
    suffix = f"-{digest}"
    # the slug becomes a filesystem path component (projects/<slug>/entries/
    # ...); a long git-root basename must not push it past the OS's own
    # per-component byte limit and turn every write in the project into
    # ENAMETOOLONG
    name = _fit_filename_bytes(name, suffix).rstrip("-") or "project"
    return f"{name}{suffix}"


def build_context(project_dir: Path) -> AccessContext:
    """The access context for a working directory: its project, or global-only."""
    slug = project_slug(project_dir)
    return AccessContext(project_id=ProjectId(slug) if slug is not None else None)
