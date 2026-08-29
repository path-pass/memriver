from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, get_args

import frontmatter
from ulid import ULID

MemoryType = Literal["preference", "fact", "decision", "state", "lesson", "pointer"]
Trust = Literal["user", "agent", "untrusted-derived"]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Entry:
    id: str
    type: str
    scope: str
    sync: bool
    created: str
    updated: str
    source: dict
    trust: str
    superseded_by: str | None
    body: str

    @classmethod
    def new(cls, *, body: str, type: str, scope: str, source: dict,
            trust: str = "agent", sync: bool = True) -> "Entry":
        if type not in get_args(MemoryType):
            raise ValueError(f"invalid memory type: {type!r}")
        if trust not in get_args(Trust):
            raise ValueError(f"invalid trust: {trust!r}")
        now = _now()
        return cls(id=str(ULID()), type=type, scope=scope, sync=sync,
                   created=now, updated=now, source=dict(source), trust=trust,
                   superseded_by=None, body=body.strip())

    def to_markdown(self) -> str:
        meta = {"id": self.id, "type": self.type, "scope": self.scope,
                "sync": self.sync, "created": self.created, "updated": self.updated,
                "source": self.source, "trust": self.trust,
                "superseded_by": self.superseded_by}
        post = frontmatter.Post(self.body, **meta)
        return frontmatter.dumps(post) + "\n"

    @classmethod
    def from_markdown(cls, text: str) -> "Entry":
        post = frontmatter.loads(text)
        m = post.metadata
        return cls(id=m["id"], type=m["type"], scope=m["scope"], sync=bool(m["sync"]),
                   created=str(m["created"]), updated=str(m["updated"]),
                   source=dict(m["source"]), trust=m["trust"],
                   superseded_by=m.get("superseded_by"), body=post.content.strip())
