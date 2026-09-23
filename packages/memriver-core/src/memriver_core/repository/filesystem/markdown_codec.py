"""Memory <-> frontmatter markdown, the filesystem backend's storage format."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import ClassVar, get_args

import frontmatter
import yaml
from frontmatter.default_handlers import YAMLHandler

from memriver_core.models import ID_RE, Memory, MemoryType

logger = logging.getLogger(__name__)


class _StrictBoolLoader(yaml.SafeLoader):
    """`SafeLoader` with YAML 1.1's yes/no/on/off bool aliases turned off.

    `sync` is a privacy boundary, so a hand-edited `yes`/`no`/`on`/`off`
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

    A hand-edited, unquoted timestamp parses as a real `datetime`, and a
    legacy second-resolution string does not match what `now()` emits --
    either would break the lexicographic recency sort. Anything that cannot
    be canonicalized is left untouched: a bad timestamp must never make the
    memory unreadable, so diagnostics -- not the decoder -- reports it.
    """
    try:
        dt = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
        dt = dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    except (ValueError, OverflowError):
        return str(raw)


def _stored_id(raw: object, field: str) -> str:
    value = str(raw)
    if not ID_RE.fullmatch(value):
        raise ValueError(f"stored {field} is not a valid id")
    return value


def encode(memory: Memory) -> str:
    meta = {"id": memory.id, "project_id": memory.project_id, "type": memory.type,
            "sync": memory.sync, "created": memory.created, "updated": memory.updated,
            "source": memory.source, "trust": memory.trust,
            "description": memory.description}
    return frontmatter.dumps(frontmatter.Post(memory.body, **meta)) + "\n"


def decode(text: str) -> Memory:
    post = frontmatter.loads(text, handler=_StrictBoolHandler())
    m = post.metadata
    memory_id = _stored_id(m["id"], "id")
    project_id = _stored_id(m["project_id"], "project_id")
    # a hand-edited file may carry a type this version does not know; reading
    # it as "project" keeps it visible instead of lost
    if m["type"] in get_args(MemoryType):
        memory_type = m["type"]
    else:
        # the raw value is never logged: a scan decodes every project's files,
        # so it would carry another project's hand-edited field into this log
        logger.warning("coercing unknown type to 'project' for id %s", memory_id)
        memory_type = "project"
    return Memory(id=memory_id, project_id=project_id, type=memory_type,
                  source=dict(m["source"]), trust=m["trust"], sync=_parse_sync(m["sync"]),
                  created=_canonical_timestamp(m["created"]),
                  updated=_canonical_timestamp(m["updated"]),
                  description=str(m.get("description", "") or "").strip(),
                  body=post.content.strip())
