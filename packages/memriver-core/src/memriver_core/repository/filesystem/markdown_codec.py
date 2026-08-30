"""Memory <-> frontmatter markdown, the filesystem backend's storage format.

The storage-string form of a scope ("global" / "project:<id>") lives on this
side of the adapter only; models and application code carry `Scope` values.
"""

from __future__ import annotations

from typing import get_args

import frontmatter

from memriver_core.models import Memory, MemoryType, Scope


class UnparsableStoredScope(Exception):
    """A stored `scope:` value outside the "global" / "project:<id>" grammar.

    Kept distinct from an undecodable file: a `Scope` value can never equal the
    scope of the directory the file sits in, so such a file is a scope
    mismatch -- absent as far as callers go -- and not an unreadable one.
    """


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
    try:
        scope = Scope.parse(str(m["scope"]))
    except ValueError as err:
        raise UnparsableStoredScope(str(m["scope"])) from err
    return Memory(id=m["id"], type=mtype, scope=scope,
                  sync=bool(m["sync"]), created=str(m["created"]),
                  updated=str(m["updated"]), source=dict(m["source"]),
                  trust=m["trust"],
                  description=str(m.get("description", "") or "").strip(),
                  body=post.content.strip())
