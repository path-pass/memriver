"""Memory <-> frontmatter markdown, the filesystem backend's storage format."""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime
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

    def compose_node(self, parent: yaml.Node | None, index: object) -> yaml.Node | None:
        # memriver never writes aliases, and one alias can blow a small file up
        # into a huge value (or a cycle) in every scan and response: refuse it
        # before any node is built, so the file is damaged like malformed YAML
        if self.check_event(yaml.events.AliasEvent):
            raise yaml.composer.ComposerError(None, None, "YAML aliases are not accepted",
                                              self.peek_event().start_mark)
        return super().compose_node(parent, index)


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


# YAML tags can load values no transport can serialize -- a "\udXXX" escape
# decodes to a lone surrogate, !!binary to bytes -- so a decoded memory may
# hold only plain values; anything else makes the file damaged, not a memory.
# Aliases never get this far (the loader refuses them), so every value is a
# tree: no shared container, no cycle, and the walk is linear in the file
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def _is_plain(value: object) -> bool:
    """Whitelist: surrogate-free str, int/float/bool/None, date/datetime, and
    dict/list/tuple/set/frozenset of those (omap/pairs load as tuples, !!set as a set)."""
    if isinstance(value, str):
        return _SURROGATE_RE.search(value) is None
    if isinstance(value, int | float | date | None):     # bool is an int, datetime a date
        return True
    if not isinstance(value, dict | list | tuple | set | frozenset):
        return False
    children = [x for pair in value.items() for x in pair] if isinstance(value, dict) else value
    return all(_is_plain(child) for child in children)


_NOT_STORABLE = "memory cannot be stored: its fields must be plain values without shared references"


def encode(memory: Memory) -> str:
    meta = {"id": memory.id, "project_id": memory.project_id, "type": memory.type,
            "sync": memory.sync, "created": memory.created, "updated": memory.updated,
            "source": memory.source, "trust": memory.trust,
            "description": memory.description}
    text = frontmatter.dumps(frontmatter.Post(memory.body, **meta)) + "\n"
    text.encode("utf-8")      # a lone surrogate stays the writer's UnicodeEncodeError
    try:
        # the dumper writes a shared container (or a cycle) as an alias, which
        # the reader refuses: never hand back a file that cannot be read back
        decode(text)
    except Exception as err:
        raise ValueError(_NOT_STORABLE) from err
    return text


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
    memory = Memory(id=memory_id, project_id=project_id, type=memory_type,
                    source=dict(m["source"]), trust=m["trust"], sync=_parse_sync(m["sync"]),
                    created=_canonical_timestamp(m["created"]),
                    updated=_canonical_timestamp(m["updated"]),
                    description=str(m.get("description", "") or "").strip(),
                    body=post.content.strip())
    if not _is_plain(vars(memory)):
        raise ValueError("stored value is not plain text or data")
    return memory
