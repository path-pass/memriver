from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path


def storage_root() -> Path:
    env = os.environ.get("MEMRIVER_ROOT")
    return Path(env) if env else Path.home() / "agent-memory"


def _git_root(start: Path) -> Path | None:
    cur = start.resolve()
    for p in [cur, *cur.parents]:
        if (p / ".git").exists():
            return p
    return None


def project_slug(project_dir: Path) -> str | None:
    root = _git_root(project_dir)
    if root is None:
        return None
    name = re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-") or "project"
    digest = hashlib.sha1(str(root).encode()).hexdigest()[:6]
    return f"{name}-{digest}"


def resolve_scope(scope: str, project_dir: Path) -> str:
    if scope == "global" or scope.startswith("project:"):
        return scope
    if scope == "project":
        slug = project_slug(project_dir)
        if slug is None:
            raise ValueError(f"not inside a git project: {project_dir}")
        return f"project:{slug}"
    raise ValueError(f"invalid scope: {scope!r}")
