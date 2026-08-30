"""Memory <-> frontmatter markdown, the filesystem backend's storage format.

The storage-string form of a scope ("global" / "project:<id>") lives on this
side of the adapter only; models and application code carry `Scope` values.
"""

from __future__ import annotations

from typing import get_args

import frontmatter

from memriver_core.models import Memory, MemoryType, Scope


def encode(memory: Memory) -> str:
    meta = {"id": memory.id, "type": memory.type,
            "scope": memory.scope.to_storage(),
            "sync": memory.sync, "created": memory.created,
            "updated": memory.updated, "source": memory.source,
            "trust": memory.trust, "description": memory.description}
    post = frontmatter.Post(memory.body, **meta)
    return frontmatter.dumps(post) + "\n"


def decode(text: str) -> Memory:
    post = frontmatter.loads(text)
    m = post.metadata
    # a hand-edited or pre-rename file may carry a type this version does
    # not know; reading it as "project" keeps it visible instead of lost
    mtype = m["type"] if m["type"] in get_args(MemoryType) else "project"
    return Memory(id=m["id"], type=mtype, scope=Scope.parse(str(m["scope"])),
                  sync=bool(m["sync"]), created=str(m["created"]),
                  updated=str(m["updated"]), source=dict(m["source"]),
                  trust=m["trust"],
                  description=str(m.get("description", "") or "").strip(),
                  body=post.content.strip())
