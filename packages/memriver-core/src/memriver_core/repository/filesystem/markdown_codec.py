"""Memory <-> frontmatter markdown, the filesystem backend's storage format.

The storage-string form of a scope ("global" / "project:<id>") lives on this
side of the adapter only; models and application code carry `Scope` values.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import ClassVar, get_args

import frontmatter
import yaml
from frontmatter.default_handlers import YAMLHandler

from memriver_core.models import Memory, MemoryType, Scope

logger = logging.getLogger(__name__)


class UnparsableStoredScope(Exception):
    """A stored `scope:` value outside the "global" / "project:<id>" grammar.

    Kept distinct from an undecodable file: a `Scope` value can never equal the
    scope of the directory the file sits in, so such a file is a scope
    mismatch -- absent as far as callers go -- and not an unreadable one.
    """


class _StrictBoolLoader(yaml.SafeLoader):
    """`SafeLoader` with YAML 1.1's yes/no/on/off bool aliases turned off.

    `sync` is the privacy boundary, so a hand-edited `yes`/`no`/`on`/`off`
    must read back as a plain string -- not silently collapse into the same
    `bool` value as `true`/`false` before `decode` ever sees it.
    """

    yaml_implicit_resolvers: ClassVar = {
        key: [r for r in resolvers if r[0] != "tag:yaml.org,2002:bool"]
        for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }


_StrictBoolLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


class _StrictBoolHandler(YAMLHandler):
    def load(self, fm: str, **kwargs: object) -> object:
        return yaml.load(fm, Loader=_StrictBoolLoader)


def _parse_sync(raw: object) -> bool:
    """True only for the literal `bool` True or a case-insensitive "true"."""
    if raw is True:
        return True
    return isinstance(raw, str) and raw.lower() == "true"


def _canonical_timestamp(raw: object) -> str:
    """`created`/`updated` -> the canonical "YYYY-MM-DDTHH:MM:SS.ffffffZ" form.

    A hand-edited, unquoted timestamp parses as a real `datetime` through
    PyYAML's own resolver, and `str()` on that (or on a legacy
    second-resolution string this server itself once wrote) does not match
    what `now()` emits -- breaking the lexicographic freshness sort in
    `search`/`index`/`dream`. Anything that cannot be parsed as a datetime is
    left untouched: a bad timestamp must never make the memory unreadable.
    """
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw))
        except ValueError:
            return str(raw)
    dt = dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def encode(memory: Memory) -> str:
    meta = {"id": memory.id, "type": memory.type,
            "scope": memory.scope.to_storage(),
            "sync": memory.sync, "created": memory.created,
            "updated": memory.updated, "source": memory.source,
            "trust": memory.trust, "description": memory.description}
    post = frontmatter.Post(memory.body, **meta)
    return frontmatter.dumps(post) + "\n"


def decode(text: str) -> Memory:
    post = frontmatter.loads(text, handler=_StrictBoolHandler())
    m = post.metadata
    # a hand-edited or pre-rename file may carry a type this version does
    # not know; reading it as "project" keeps it visible instead of lost
    if m["type"] in get_args(MemoryType):
        mtype = m["type"]
    else:
        # update_body re-encodes whatever decode() hands back, which would
        # otherwise persist this coercion with no trace of the type it
        # silently dropped; no path here -- this fires on every decode, not
        # just the file it happened to be read from
        logger.warning("coercing unknown type %r to 'project' for id %r",
                       m["type"], m["id"])
        mtype = "project"
    try:
        scope = Scope.parse(str(m["scope"]))
    except ValueError as err:
        raise UnparsableStoredScope(str(m["scope"])) from err
    return Memory(id=m["id"], type=mtype, scope=scope,
                  sync=_parse_sync(m["sync"]),
                  created=_canonical_timestamp(m["created"]),
                  updated=_canonical_timestamp(m["updated"]),
                  source=dict(m["source"]),
                  trust=m["trust"],
                  description=str(m.get("description", "") or "").strip(),
                  body=post.content.strip())
