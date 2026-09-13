"""The four format-specific editors ``memriver install`` edits files through.

Every function here is pure: text in, ``EditResult`` out. No filesystem, no
harness knowledge, no ``memriver_core`` -- the install surface plans and
renders, it never runs memory policy.

Three rules hold across all four editors.

*Foreign content survives.* An edit touches exactly one key path or one marker
region; every other value in the user's file is carried through untouched, and
a merge or removal that changes nothing returns the original bytes.

Formatting survives with it, in every direction but one. TOML keeps its own
because tomlkit round-trips it; marker-block text keeps its own because only
the block and one newline sequence on each side of it are ever written; a JSON
*removal* keeps its own because it splices out the bytes of the one member it
takes back and copies the rest of the file through verbatim -- escapes, number
spellings, indentation, CRLF endings and trailing whitespace included. A JSON
*merge* is the exception: ``json.dumps`` re-renders the whole document at
indent 2 with LF endings, so accepting an install change normalizes those same
things. What a removal owes is the user's bytes, whoever last wrote them --
not a re-run of install's rendering.

*Ambiguity fails, it never guesses.* Two memriver hook entries, a memriver
handler sharing a group with someone else's, an unpaired marker: each raises
``PlanningError`` so the whole plan aborts before a single byte is written.

*Idempotency is byte-identical.* When the file already holds the expected
semantic value, the editor returns the original text with ``changed=False``,
so a reinstall rewrites nothing and reformats nothing.
"""

from __future__ import annotations

import json
import math
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import tomlkit
from tomlkit.exceptions import ParseError
from tomlkit.items import InlineTable

EditorKind = Literal["json-object", "hook-array", "toml-table", "marker-block"]

MARKER_BEGIN = "<!-- memriver:begin -->"
MARKER_END = "<!-- memriver:end -->"

# Spec 5.4 / DEFERRED-2: a takeover prints the new fragment and this fixed
# line. Old values are never rendered anywhere, so there is no diff engine.
TAKEOVER_NOTICE = (
    "existing memriver entry differs and will be replaced (old value not shown)"
)
# The native-memory toggles are the harness's own settings that memriver turns
# off, not memriver entries; the notice says which of the two it is replacing.
HARNESS_SETTING_TAKEOVER_NOTICE = (
    "existing harness setting differs and will be replaced (old value not shown)"
)


class PlanningError(Exception):
    """A file cannot be edited safely; the whole plan aborts, nothing is written."""


def mcp_server_payload() -> dict:
    """The memriver MCP server registration, identical across every harness."""
    return {"command": "uvx", "args": ["memriver"]}


def hook_identity(verb: str) -> tuple[str, ...]:
    """The leading command words that find memriver's own hook entry again.

    The installed command and the identity a reinstall matches it by are the
    same words; spelled apart, a rename to either would stop finding the entry
    and append a second one on every run.
    """
    return ("uvx", "memriver", "hook", verb)


def hook_group(verb: str, harness: str) -> dict:
    """A single-handler hook group invoking ``memriver hook <verb> --harness <harness>``."""
    return {
        "hooks": [{
            "type": "command",
            "command": f"{' '.join(hook_identity(verb))} --harness {harness}",
        }],
    }


@dataclass(frozen=True)
class Target:
    path: Path
    user_level: bool
    rollback_instruction: str
    # a file that is entirely memriver's own -- never shared with unrelated
    # harness settings -- is deleted outright when a removal empties it,
    # rather than left behind as an empty file (spec P2-6); every other
    # target is a shared harness file, and an emptied container in it is the
    # documented residue
    delete_if_emptied: bool = False


@dataclass(frozen=True)
class Snapshot:
    target: Target
    text: str | None
    mode: int | None


@dataclass(frozen=True)
class EditOperation:
    id: str
    target: Target
    label: str
    kind: EditorKind
    expected: object
    key_path: tuple[str, ...] = ()
    identity: tuple[str, ...] = ()
    optional: bool = False
    # the key belongs to the harness, not to memriver: only the takeover
    # wording differs, and it is kept apart from `optional` because "the user
    # may decline this" and "memriver does not own this key" are two facts
    harness_owned: bool = False


@dataclass(frozen=True)
class EditResult:
    rendered: str
    changed: bool
    takeover: bool


@dataclass(frozen=True)
class RemovalOperation:
    """The uninstall counterpart to ``EditOperation``.

    There is no ``expected`` value to write -- a removal only needs where to
    look and how to recognize memriver's own entry there, which for a
    hook-array is ``identity`` and for everything else is ``key_path`` alone.
    """

    id: str
    target: Target
    label: str
    kind: EditorKind
    key_path: tuple[str, ...] = ()
    identity: tuple[str, ...] = ()


def apply_removal(operation: RemovalOperation, text: str) -> EditResult:
    """Run the remover named by ``operation.kind``, validating its fields first."""
    if operation.kind == "marker-block":
        return marker_block_remove(text)
    if not operation.key_path:
        raise PlanningError(
            f"operation {operation.id} is a {operation.kind} removal and needs a "
            "key path"
        )
    if operation.kind == "json-object":
        return json_object_remove(text, operation.key_path)
    if operation.kind == "toml-table":
        return toml_table_remove(text, operation.key_path)
    if operation.kind == "hook-array":
        if not operation.identity:
            raise PlanningError(
                f"operation {operation.id} is a hook-array removal and needs the "
                "command identity that finds memriver's entry"
            )
        if len(operation.key_path) != 2 or operation.key_path[0] != "hooks":
            raise PlanningError(
                f"operation {operation.id} is a hook-array removal and needs the "
                f"key path ('hooks', <event>), not {_dotted(operation.key_path)}"
            )
        return hook_array_identity_remove(
            text, operation.key_path[-1], operation.identity,
        )
    raise PlanningError(f"unknown editor kind {operation.kind!r}")


def apply_edit(operation: EditOperation, text: str) -> EditResult:
    """Run the editor named by ``operation.kind``, validating its fields first."""
    if operation.expected is None:
        raise PlanningError(f"operation {operation.id} has no expected value")
    if operation.kind == "marker-block":
        if not isinstance(operation.expected, str):
            raise PlanningError(
                f"operation {operation.id} is a marker-block edit and needs its "
                "expected block as text"
            )
        return marker_block(text, operation.expected)
    if not operation.key_path:
        raise PlanningError(
            f"operation {operation.id} is a {operation.kind} edit and needs a key path"
        )
    if operation.kind == "json-object":
        return json_object_merge(text, operation.key_path, operation.expected)
    if operation.kind == "toml-table":
        return toml_roundtrip(text, operation.key_path, operation.expected)
    if operation.kind == "hook-array":
        if not operation.identity:
            raise PlanningError(
                f"operation {operation.id} is a hook-array edit and needs the command "
                "identity that finds memriver's entry"
            )
        # The editor always edits hooks.<event>; the summary renders key_path.
        # Pinning the shape here keeps the two from describing different places.
        if len(operation.key_path) != 2 or operation.key_path[0] != "hooks":
            raise PlanningError(
                f"operation {operation.id} is a hook-array edit and needs the key path "
                f"('hooks', <event>), not {_dotted(operation.key_path)}"
            )
        handlers = (
            operation.expected.get("hooks")
            if isinstance(operation.expected, dict) else None
        )
        if (
            not isinstance(handlers, list)
            or not handlers
            or _matching_handlers(operation.expected, operation.identity) != handlers
        ):
            raise PlanningError(
                f"operation {operation.id} must expect a matcher group whose every "
                f"handler runs {' '.join(operation.identity)}; a group memriver "
                "cannot find again would be appended on every install"
            )
        return hook_array_identity_merge(
            text, operation.key_path[-1], operation.identity, operation.expected,
        )
    raise PlanningError(f"unknown editor kind {operation.kind!r}")


# --- json-object -----------------------------------------------------------


def json_object_merge(
    source: str, key_path: tuple[str, ...], expected: object,
) -> EditResult:
    """Set one nested key, creating missing parent objects, keeping everything else."""
    if not key_path:
        raise PlanningError("a json-object edit needs a key path")
    document = _parse_json_object(source, "install")
    parent = document
    for depth, key in enumerate(key_path[:-1]):
        if key not in parent:
            parent[key] = {}
        child = parent[key]
        if not isinstance(child, dict):
            raise PlanningError(
                f"{_dotted(key_path[: depth + 1])} is not a JSON object; "
                "memriver will not overwrite it"
            )
        parent = child
    leaf = key_path[-1]
    present = leaf in parent
    if present and parent[leaf] == expected:
        return EditResult(rendered=source, changed=False, takeover=False)
    parent[leaf] = expected
    return EditResult(rendered=_render_json(document, "install"), changed=True,
                      takeover=present)


def json_object_remove(source: str, key_path: tuple[str, ...]) -> EditResult:
    """Delete one nested key, keeping every parent object -- empty or not.

    The inverse of ``json_object_merge``: an absent key is already clean, not
    an error. A parent object left empty by this delete (whether install
    auto-created it or the user had it there already) is never pruned away.

    The parsed document decides *what* goes; the bytes outside that one member
    are spliced through untouched (see ``_member_removal_span``).
    """
    if not key_path:
        raise PlanningError("a json-object edit needs a key path")
    document = _parse_json_object(source, "uninstall")
    if not _delete_leaf(document, key_path):
        return EditResult(rendered=source, changed=False, takeover=False)
    span = _member_removal_span(source, key_path, "uninstall")
    return EditResult(rendered=_spliced(source, span, "uninstall"), changed=True,
                      takeover=False)


# --- hook-array ------------------------------------------------------------


def hook_array_identity_merge(
    source: str, event: str, identity: tuple[str, ...], expected: object,
) -> EditResult:
    """Insert or replace only memriver's group in a hook array shared with others.

    memriver's group is the one whose handler command starts with ``identity``
    once ``shlex``-normalized, so spacing and added flags still match. Zero
    matches append; one single-handler match is compared and replaced in place;
    anything else is an existing configuration memriver cannot resolve.
    """
    if not identity:
        raise PlanningError("a hook-array edit needs a command identity")
    document = _parse_json_object(source, "install")
    if "hooks" not in document:
        document["hooks"] = {}
    hooks = document["hooks"]
    if not isinstance(hooks, dict):
        raise PlanningError("hooks is not a JSON object")
    if event not in hooks:
        hooks[event] = []
    groups = hooks[event]
    if not isinstance(groups, list):
        raise PlanningError(f"hooks.{event} is not a JSON array")

    for group in groups:
        for handler in _handlers(group):
            if _is_unlexable_memriver_command(handler.get("command"), identity):
                raise PlanningError(
                    f"a command in hooks.{event} looks like memriver's but is not a "
                    "parseable shell command; fix or remove it and run install again"
                )
    matched = [
        i for i, group in enumerate(groups) if _matching_handlers(group, identity)
    ]
    if len(matched) > 1:
        raise PlanningError(
            f"hooks.{event} already has {len(matched)} memriver entries; remove all "
            "but one and run install again"
        )
    if not matched:
        groups.append(expected)
        return EditResult(rendered=_render_json(document, "install"), changed=True,
                          takeover=False)

    index = matched[0]
    current = groups[index]
    if len(current["hooks"]) != 1:
        raise PlanningError(
            f"the memriver handler in hooks.{event} shares a group with other "
            "handlers; move it into its own group and run install again"
        )
    if current == expected:
        return EditResult(rendered=source, changed=False, takeover=False)
    groups[index] = expected
    return EditResult(rendered=_render_json(document, "install"), changed=True,
                      takeover=True)


def hook_array_identity_remove(
    source: str, event: str, identity: tuple[str, ...],
) -> EditResult:
    """Remove memriver's own group from a hook array shared with others.

    Mirrors ``hook_array_identity_merge``'s matching rules exactly, so
    uninstall finds precisely the entry a reinstall would find again: zero
    matches is already clean, more than one is an existing configuration this
    will not guess at, and a match sharing its group with a foreign handler is
    left alone rather than deleting handlers memriver never installed.
    """
    if not identity:
        raise PlanningError("a hook-array edit needs a command identity")
    document = _parse_json_object(source, "uninstall")
    hooks = document.get("hooks")
    if not isinstance(hooks, dict) or event not in hooks:
        return EditResult(rendered=source, changed=False, takeover=False)
    groups = hooks[event]
    if not isinstance(groups, list):
        raise PlanningError(f"hooks.{event} is not a JSON array")

    for group in groups:
        for handler in _handlers(group):
            if _is_unlexable_memriver_command(handler.get("command"), identity):
                raise PlanningError(
                    f"a command in hooks.{event} looks like memriver's but is not a "
                    "parseable shell command; fix or remove it and run uninstall again"
                )
    matched = [
        i for i, group in enumerate(groups) if _matching_handlers(group, identity)
    ]
    if len(matched) > 1:
        raise PlanningError(
            f"hooks.{event} has {len(matched)} memriver entries; remove all but "
            "one and run uninstall again"
        )
    if not matched:
        return EditResult(rendered=source, changed=False, takeover=False)

    index = matched[0]
    if len(groups[index]["hooks"]) != 1:
        raise PlanningError(
            f"the memriver handler in hooks.{event} shares a group with other "
            "handlers; move it into its own group and run uninstall again"
        )
    # the emptied event array, and "hooks" itself, are never pruned away here
    # -- P2-3: removal takes back only memriver's own group, leaving whatever
    # container (the user's own, or one install auto-created) held it
    span = _element_removal_span(source, ("hooks", event), index, "uninstall")
    return EditResult(rendered=_spliced(source, span, "uninstall"), changed=True,
                      takeover=False)


# --- where a JSON member's bytes are ----------------------------------------

# Re-serializing a JSON document normalizes escapes, number spellings,
# indentation and line endings across the whole file -- every byte of it, not
# just memriver's own entry. So a removal never re-serializes: the parsed
# document (already validated by `_parse_json_object`) decides *what* to
# remove, this scanner finds *where* those bytes are, and the removal is a
# splice around them.

_JSON_WHITESPACE = " \t\n\r"


class _UnscannableJson(Exception):
    """The scanner lost the thread of a document the parser had accepted."""


@dataclass(frozen=True)
class _JsonItem:
    """One member of an object, or one element of an array, and its bytes.

    ``start`` is the first byte of the member (its name's opening quote) or of
    the element; ``value_start`` is the first byte of the value; ``end`` is
    just past the value's last byte. Separators are not part of it.
    """

    name: str | None
    start: int
    value_start: int
    end: int


@dataclass(frozen=True)
class _JsonContainer:
    items: tuple[_JsonItem, ...]
    # the bytes between the braces/brackets, the braces themselves excluded
    inner_start: int
    inner_end: int


class _JsonScan:
    """A cursor over the source text of a document already known to be valid.

    It has no opinion on what JSON means -- ``json.loads`` has settled that --
    and only tracks strings (escapes included), nesting and item boundaries
    well enough to say which bytes belong to which member.
    """

    def __init__(self, source: str) -> None:
        self.source = source
        self.at = 0

    def container(self, start: int) -> _JsonContainer:
        """Scan the object or array starting at ``start``, listing its items.

        Leaves the cursor just past the closing brace or bracket, which is what
        lets ``_skip_value`` step over a nested container in one call.
        """
        self.at = start
        self._skip_whitespace()
        opening = self._peek()
        if opening not in "{[":
            raise _UnscannableJson(f"no container at {start}")
        closing = "}" if opening == "{" else "]"
        named = opening == "{"
        self.at += 1
        inner_start = self.at
        self._skip_whitespace()
        if self._peek() == closing:
            self.at += 1
            return _JsonContainer((), inner_start, self.at - 1)
        items: list[_JsonItem] = []
        while True:
            self._skip_whitespace()
            item_start = self.at
            name = None
            if named:
                name_start = self.at
                self._skip_string()
                name = self._decoded(name_start, self.at)
                self._skip_whitespace()
                self._take(":")
                self._skip_whitespace()
            value_start = self.at
            self._skip_value()
            items.append(_JsonItem(name, item_start, value_start, self.at))
            self._skip_whitespace()
            if self._peek() == ",":
                self.at += 1
                continue
            inner_end = self.at
            self._take(closing)
            return _JsonContainer(tuple(items), inner_start, inner_end)

    def _peek(self) -> str:
        if self.at >= len(self.source):
            raise _UnscannableJson("the document ends mid-value")
        return self.source[self.at]

    def _take(self, char: str) -> None:
        if self._peek() != char:
            raise _UnscannableJson(f"expected {char!r} at {self.at}")
        self.at += 1

    def _skip_whitespace(self) -> None:
        while self.at < len(self.source) and self.source[self.at] in _JSON_WHITESPACE:
            self.at += 1

    def _skip_string(self) -> None:
        self._take('"')
        while True:
            char = self._peek()
            self.at += 1
            if char == "\\":  # the escaped byte is never a closing quote
                self._peek()
                self.at += 1
            elif char == '"':
                return

    def _skip_value(self) -> None:
        char = self._peek()
        if char in "{[":
            self.container(self.at)
        elif char == '"':
            self._skip_string()
        else:  # a number, true, false or null -- it ends where the syntax does
            start = self.at
            while (self.at < len(self.source)
                   and self.source[self.at] not in _JSON_WHITESPACE + ",]}"):
                self.at += 1
            if self.at == start:
                raise _UnscannableJson(f"no value at {start}")

    def _decoded(self, start: int, stop: int) -> str:
        try:
            return json.loads(self.source[start:stop])
        except ValueError as error:
            raise _UnscannableJson(f"unreadable member name at {start}") from error


def _container_at(scan: _JsonScan, key_path: tuple[str, ...]) -> _JsonContainer:
    """The container ``key_path`` names; an empty path names the document."""
    container = scan.container(0)
    for key in key_path:
        item = _named(container, key)
        container = scan.container(item.value_start)
    return container


def _named(container: _JsonContainer, name: str) -> _JsonItem:
    for item in container.items:
        if item.name == name:
            return item
    raise _UnscannableJson(f"no member named {name!r}")


def _member_removal_span(source: str, key_path: tuple[str, ...],
                         command_name: str) -> tuple[int, int]:
    """The bytes to cut so ``key_path``'s member -- and one comma -- disappear."""
    def locate(scan: _JsonScan) -> tuple[int, int]:
        container = _container_at(scan, key_path[:-1])
        item = _named(container, key_path[-1])
        return _span_without(container, container.items.index(item))

    return _located(source, locate, command_name)


def _element_removal_span(source: str, key_path: tuple[str, ...], index: int,
                          command_name: str) -> tuple[int, int]:
    """The bytes to cut so one array element -- and one comma -- disappear."""
    def locate(scan: _JsonScan) -> tuple[int, int]:
        return _span_without(_container_at(scan, key_path), index)

    return _located(source, locate, command_name)


def _located(source: str, locate: Callable[[_JsonScan], tuple[int, int]],
             command_name: str) -> tuple[int, int]:
    """Run ``locate``, turning every way it can fail into a ``PlanningError``.

    The document has already been parsed and validated by the time this runs,
    so a failure here is memriver's own scanner falling behind the parser, not
    a diagnosis about the user's file -- it aborts the plan with nothing
    written rather than reaching them as a traceback.
    """
    try:
        return locate(_JsonScan(source))
    except RecursionError as error:  # the same foreign nesting `json.loads` hits
        raise PlanningError(_too_deeply_nested(command_name)) from error
    except (_UnscannableJson, IndexError) as error:
        raise PlanningError(
            "memriver cannot tell exactly which bytes of this file hold its own "
            f"entry, so nothing was changed; remove the entry by hand and run "
            f"{command_name} again"
        ) from error


def _spliced(source: str, span: tuple[int, int], command_name: str) -> str:
    """``source`` with ``span`` cut out of it, re-parsed before it is returned.

    Every other editor proves what it renders; a splice has to prove it too,
    and re-parsing is what catches a span that would have left the document
    malformed before those bytes can reach a file.
    """
    start, end = span
    rendered = source[:start] + source[end:]
    _parse_json_object(rendered, command_name)
    return rendered


def _span_without(container: _JsonContainer, index: int) -> tuple[int, int]:
    """The span covering one item plus exactly one adjacent separator.

    A following item's start is the cut's end when there is one, so the removed
    item's own leading whitespace becomes the next item's; the last item takes
    the comma and whitespace in front of it instead. The sole item of a
    container takes the container's whole interior, which leaves ``{}``/``[]``
    standing -- emptied, never pruned -- rather than a container holding only
    the indentation of something that is gone.
    """
    items = container.items
    if index >= len(items):
        raise _UnscannableJson(f"no item {index} in a container of {len(items)}")
    if len(items) == 1:
        return container.inner_start, container.inner_end
    if index + 1 < len(items):
        return items[index].start, items[index + 1].start
    return items[index - 1].end, items[index].end


def _handlers(group: object) -> list[dict[str, Any]]:
    if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
        return []
    return [handler for handler in group["hooks"] if isinstance(handler, dict)]


def _matching_handlers(group: object, identity: tuple[str, ...]) -> list[object]:
    return [
        handler for handler in _handlers(group)
        if _has_identity(handler.get("command"), identity)
    ]


def _words(command: object) -> tuple[str, ...] | None:
    """The shell words of a command, or None when it cannot be lexed."""
    if not isinstance(command, str):
        return None
    try:
        return tuple(shlex.split(command))
    except ValueError:
        return None


def _has_identity(command: object, identity: tuple[str, ...]) -> bool:
    words = _words(command)
    return words is not None and words[: len(identity)] == identity


def _is_unlexable_memriver_command(command: object, identity: tuple[str, ...]) -> bool:
    """A command memriver would own, broken badly enough that identity can't be read.

    Treating it as foreign would append a second memriver group on every run,
    so it is an ambiguous existing configuration instead.
    """
    return (
        isinstance(command, str)
        and _words(command) is None
        and " ".join(identity[:2]) in command
    )


# --- toml-table ------------------------------------------------------------


def toml_roundtrip(
    source: str, key_path: tuple[str, ...], expected: object,
) -> EditResult:
    """Set one table or scalar leaf, preserving the formatting of everything else."""
    if not key_path:
        raise PlanningError("a toml-table edit needs a key path")
    try:
        document = tomlkit.parse(source)
    except ParseError as error:
        raise PlanningError(f"file is not valid TOML: {error}") from error
    parent: Any = document
    for depth, key in enumerate(key_path[:-1]):
        if key not in parent:
            parent[key] = tomlkit.table(True)
        child = parent[key]
        if not isinstance(child, Mapping):
            raise PlanningError(
                f"{_dotted(key_path[: depth + 1])} is not a TOML table; "
                "memriver will not overwrite it"
            )
        if isinstance(child, InlineTable):
            # TOML forbids a table inside an inline table, and rewriting the
            # user's inline structure is not memriver's call.
            raise PlanningError(
                f"{_dotted(key_path[: depth + 1])} is an inline table and cannot "
                "hold memriver's table; convert it to a regular table and run "
                "install again"
            )
        parent = child
    leaf = key_path[-1]
    present = leaf in parent
    if present and _plain(parent[leaf]) == expected:
        return EditResult(rendered=source, changed=False, takeover=False)
    # An inline table at the leaf *is* replaceable: it is memriver's own node,
    # it compares semantically like any other, and tomlkit rewrites it as a
    # standard table. Only inline parents (above) are refused.
    try:
        parent[leaf] = _toml_value(expected)
    except ValueError as error:  # tomlkit refused the shape of the existing node
        raise PlanningError(
            f"{_dotted(key_path)} cannot be replaced in this file; move it to a "
            "regular table and run install again"
        ) from error
    rendered = _one_newline_before_an_appended_table(source, _render_toml(document))
    return EditResult(rendered=rendered, changed=True, takeover=present)


def _one_newline_before_an_appended_table(source: str, rendered: str) -> str:
    """Separate a table appended at the end of the file by exactly one newline.

    tomlkit pads a freshly appended table up to a blank line, which renders
    ``'x = 1\\n'`` and ``'x = 1\\n\\n'`` identically -- information
    ``toml_table_remove`` would then have no way to give back. Fixing the
    separator at one newline, and leaving whatever the file already ended with
    in front of it, makes the pair invertible. Only a pure append is touched;
    an edit that landed anywhere else in the document is left exactly as
    tomlkit rendered it.
    """
    if not source or not rendered.startswith(source):
        return rendered
    return source + "\n" + rendered[len(source):].lstrip("\n")


def toml_table_remove(source: str, key_path: tuple[str, ...]) -> EditResult:
    """Delete one table or scalar leaf, keeping every parent table -- empty or not.

    The inverse of ``toml_roundtrip``: an absent key is already clean. A
    parent table left empty by this delete is never pruned away -- an
    auto-created super table (no header of its own) then simply renders as
    nothing, same as before it existed; a table the user wrote by hand (with
    its own header, or a comment) survives exactly as they wrote it.
    """
    if not key_path:
        raise PlanningError("a toml-table edit needs a key path")
    try:
        document = tomlkit.parse(source)
    except ParseError as error:
        raise PlanningError(f"file is not valid TOML: {error}") from error
    if not _delete_leaf(document, key_path):
        return EditResult(rendered=source, changed=False, takeover=False)
    rendered = _undo_added_newline(source, _render_toml(document))
    return EditResult(rendered=rendered, changed=True, takeover=False)


def _undo_added_newline(source: str, rendered: str) -> str:
    """Take back exactly one newline orphaned right at the point this deletion
    touched -- the separator ``toml_roundtrip`` puts in front of a table it
    appends, and the only trailing whitespace a removal is ever entitled to
    touch. Scoped to that single spot (never the whole document) so a run of
    the user's own blank lines, or one the deletion never touched at all,
    survives byte-for-byte.
    """
    limit = min(len(source), len(rendered))
    prefix_len = 0
    while prefix_len < limit and source[prefix_len] == rendered[prefix_len]:
        prefix_len += 1
    suffix_len = 0
    limit -= prefix_len
    while (suffix_len < limit
          and source[len(source) - 1 - suffix_len] == rendered[len(rendered) - 1 - suffix_len]):
        suffix_len += 1
    prefix = rendered[:prefix_len]
    if prefix_len + suffix_len == len(rendered) and prefix.endswith("\n"):
        return _without_one_trailing_newline(prefix) + rendered[prefix_len:]
    return rendered


def _without_one_trailing_newline(text: str) -> str:
    """``text`` less the newline it ends with -- ``"\\r\\n"`` counted as one.

    A separator is a newline *sequence*: taking the ``"\\n"`` off a CRLF line
    ending would leave the carriage return behind as an orphan byte in the
    middle of a file that has none anywhere else.
    """
    return text[:-2] if text.endswith("\r\n") else text[:-1]


def _without_one_leading_newline(text: str) -> str:
    """``text`` less the newline it starts with -- ``"\\r\\n"`` counted as one."""
    if text.startswith("\r\n"):
        return text[2:]
    return text.removeprefix("\n")


def _toml_value(value: object) -> Any:
    if isinstance(value, Mapping):
        table = tomlkit.table()
        for key, item in value.items():
            table[key] = _toml_value(item)
        return table
    return value


def _plain(value: object) -> object:
    unwrap = getattr(value, "unwrap", None)
    return unwrap() if callable(unwrap) else value


# --- marker-block ----------------------------------------------------------


def marker_block(source: str, body: str) -> EditResult:
    """Append or replace the single ``memriver:begin/end`` region of a text file.

    Appending adds exactly one newline in front of the block and one behind it,
    and touches nothing else: whatever the file already ended with -- no
    newline, one, a run of blank lines, trailing spaces, CRLF -- is carried
    through verbatim, so ``marker_block_remove`` can give those bytes back.
    Normalizing the tail here instead would collapse ``"notes"``,
    ``"notes\\n"`` and ``"notes\\n\\n"`` onto one rendering that no removal
    could tell apart again.
    """
    block = _block_text(body)
    start, stop = _marker_span(source, "install")
    if start is None:
        separator = "\n" if source else ""
        rendered = source + separator + block + "\n"
    elif source[start:stop] == block:
        return EditResult(rendered=source, changed=False, takeover=False)
    else:
        rendered = source[:start] + block + source[stop:]
    _marker_span(rendered, "install")
    return EditResult(rendered=rendered, changed=True, takeover=start is not None)


def marker_block_remove(source: str) -> EditResult:
    """Remove the whole memriver marker block, markers included.

    The exact inverse of ``marker_block``: one newline comes off each side of
    the block, because one newline is all ``marker_block`` ever put there --
    and a CRLF ending is one newline, taken back whole rather than split into
    a stranded carriage return. Every other byte around the block -- a run of
    the user's own blank lines, trailing spaces, a missing final newline --
    is theirs and survives untouched.
    """
    start, stop = _marker_span(source, "uninstall")
    if start is None:
        return EditResult(rendered=source, changed=False, takeover=False)
    before, after = source[:start], source[stop:]
    if before.endswith("\n"):
        before = _without_one_trailing_newline(before)
    rendered = before + _without_one_leading_newline(after)
    return EditResult(rendered=rendered, changed=True, takeover=False)


def _block_text(body: object) -> str:
    if not isinstance(body, str):
        raise PlanningError("a marker-block edit needs its block as text")
    inner = body.strip()
    if inner.startswith(MARKER_BEGIN) and inner.endswith(MARKER_END):
        inner = inner[len(MARKER_BEGIN) : -len(MARKER_END)].strip()
    if MARKER_BEGIN in inner or MARKER_END in inner:
        raise PlanningError("the managed block body must not contain memriver markers")
    return f"{MARKER_BEGIN}\n{inner}\n{MARKER_END}"


def _marker_span(text: str, command_name: str) -> tuple[int, int] | tuple[None, None]:
    begins, ends = _positions(text, MARKER_BEGIN), _positions(text, MARKER_END)
    if len(begins) > 1 or len(ends) > 1 or len(begins) != len(ends):
        raise PlanningError(
            f"expected one memriver marker pair, found {len(begins)} begin and "
            f"{len(ends)} end markers; fix the markers and run {command_name} again"
        )
    if not begins:
        return None, None
    if begins[0] > ends[0]:
        raise PlanningError(
            f"{MARKER_END} appears before {MARKER_BEGIN}; fix the markers and run "
            f"{command_name} again"
        )
    return begins[0], ends[0] + len(MARKER_END)


def validate_document(text: str, kind: EditorKind,
                      command_name: str = "install") -> None:
    """Re-parse a fully rendered document, raising ``PlanningError`` if unsound.

    The editors already validate what they render; the orchestrator runs this
    over the *final* text of every target -- once after planning and again
    after re-applying only the accepted edits -- so a file is proven whole
    before it is a candidate for replacement. ``command_name`` is the command
    the user ran, which is what the remediation in any raised message names;
    it defaults to install, the only command there was when this became part
    of the package's public surface, so a two-argument call still works.
    """
    if kind == "marker-block":
        _marker_span(text, command_name)
    elif kind == "toml-table":
        try:
            tomlkit.parse(text)
        except ParseError as error:
            raise PlanningError(f"file is not valid TOML: {error}") from error
    else:
        _parse_json_object(text, command_name)


def _positions(text: str, marker: str) -> list[int]:
    found, index = [], text.find(marker)
    while index != -1:
        found.append(index)
        index = text.find(marker, index + len(marker))
    return found


# --- change summary --------------------------------------------------------


def operation_label(operation: EditOperation, home: Path) -> str:
    """``<harness>: <what changes> -> <where>``, the one line that identifies it.

    An install run over several harnesses repeats the same wording -- four
    changes "register memriver MCP server" -- so the harness (the prefix every
    operation id already carries) and the file being written are what tell one
    confirmation from the next.
    """
    harness = operation.id.split(":", 1)[0]
    return f"{harness}: {operation.label} -> {display_path(operation.target.path, home)}"


def display_path(path: Path, home: Path) -> str:
    """Home-relative targets render as ``~/...``; everything else stays absolute.

    Summaries get pasted into issues and transcripts, and a real home directory
    names its user.
    """
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) else str(path)


def render_change_summary(operation: EditOperation, result: EditResult,
                          home: Path) -> str:
    """Render the label, the managed region, and the NEW fragment -- nothing else.

    The pre-existing fragment is never passed in, so it can never leak into
    output; a takeover says only that something differed. [spec 5.4, DEFERRED-2]
    """
    region = (
        f"{MARKER_BEGIN} ... {MARKER_END}"
        if operation.kind == "marker-block"
        else _dotted(operation.key_path)
    )
    lines = [operation_label(operation, home), region, _fragment(operation)]
    if result.takeover:
        lines.append(HARNESS_SETTING_TAKEOVER_NOTICE if operation.harness_owned
                     else TAKEOVER_NOTICE)
    return "\n".join(lines) + "\n"


def render_removal_summary(operation: RemovalOperation, result: EditResult,
                           home: Path) -> str:
    """Render the label and the managed region being removed -- no old value.

    ``result`` is accepted only to keep the same call shape as
    ``render_change_summary``, so the planning pipeline can render either kind
    of change through one uniform callback; a removal never takes anything
    over, so there is nothing in it left to say.
    """
    del result
    region = (
        f"{MARKER_BEGIN} ... {MARKER_END}"
        if operation.kind == "marker-block"
        else _dotted(operation.key_path)
    )
    return f"{operation_label(operation, home)}\n{region}\nremoved\n"


def _fragment(operation: EditOperation) -> str:
    if operation.kind == "marker-block":
        return _block_text(operation.expected)
    if operation.kind == "toml-table":
        document = tomlkit.document()
        parent: Any = document
        for key in operation.key_path[:-1]:
            parent[key] = tomlkit.table(True)
            parent = parent[key]
        parent[operation.key_path[-1]] = _toml_value(operation.expected)
        return tomlkit.dumps(document).rstrip("\n")
    return json.dumps(operation.expected, indent=2, ensure_ascii=False)


# --- shared helpers --------------------------------------------------------


def _dotted(key_path: tuple[str, ...]) -> str:
    return ".".join(key_path)


def _delete_leaf(document: Any, key_path: tuple[str, ...]) -> bool:
    """Delete only ``key_path``'s leaf; every ancestor container is left as-is.

    A container left empty by this delete is never pruned -- whether install
    auto-created it on the way down (``json_object_merge``, ``toml_roundtrip``)
    or the user had it there already, removal only ever takes back memriver's
    own leaf entry. Returns ``False``, changing nothing, when any step of the
    path is already absent.
    """
    chain = [document]
    for key in key_path[:-1]:
        parent = chain[-1]
        if key not in parent or not isinstance(parent[key], Mapping):
            return False
        chain.append(parent[key])
    leaf = key_path[-1]
    if leaf not in chain[-1]:
        return False
    del chain[-1][leaf]
    return True


def _non_standard_number(command_name: str) -> str:
    return (
        "file holds a number JSON cannot represent (an infinity or a NaN); "
        "memriver will not rewrite it, because writing it back produces a "
        f"document strict parsers reject. Fix the value and run {command_name} "
        "again"
    )


def _duplicate_name_guard(command_name: str) -> Callable[[list[tuple[str, Any]]],
                                                         dict[str, Any]]:
    """``object_pairs_hook`` that refuses what the default decoder would drop.

    ``json.loads`` keeps the last value of a repeated name, so re-serializing
    would erase a foreign value the summary never showed and the user never
    confirmed. Ambiguity fails.
    """
    def no_duplicate_names(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: dict[str, Any] = {}
        for name, value in pairs:
            if name in seen:
                raise PlanningError(
                    f"file has two JSON members named {name!r}; memriver will not "
                    "rewrite it because re-serializing keeps only the last one. "
                    f"Remove the duplicate and run {command_name} again"
                )
            seen[name] = value
        return seen

    return no_duplicate_names


def _finite_number_guard(command_name: str) -> Callable[[str], float]:
    """``parse_float`` guard: ``1e400`` decodes to ``inf`` without complaint."""
    def finite_number(raw: str) -> float:
        value = float(raw)
        if not math.isfinite(value):
            raise PlanningError(_non_standard_number(command_name))
        return value

    return finite_number


def _constant_guard(command_name: str) -> Callable[[str], object]:
    """``parse_constant`` guard: ``NaN``/``Infinity`` are not JSON [RFC 8259 §6]."""
    def reject_constant(name: str) -> object:
        del name  # the offending token is the user's content; fixed text says enough
        raise PlanningError(_non_standard_number(command_name))

    return reject_constant


def _parse_json_object(source: str, command_name: str) -> dict[str, Any]:
    if not source.strip():
        return {}
    try:
        document = json.loads(source,
                              object_pairs_hook=_duplicate_name_guard(command_name),
                              parse_constant=_constant_guard(command_name),
                              parse_float=_finite_number_guard(command_name))
    except ValueError as error:
        # every rejection the decoder itself raises, not only the
        # JSONDecodeError subclass: a syntactically legal integer past
        # CPython's integer-string conversion limit raises a plain ValueError
        # from inside `json.loads`, and outside this boundary it reaches the
        # user as a traceback. The guards above raise PlanningError, which is
        # not a ValueError, so their own wording survives this clause. The
        # decoder's message is position-and-limit text with no path in it.
        raise PlanningError(f"file is not valid JSON: {error}") from error
    except RecursionError as error:
        # `json.loads` recurses once per nesting level, so a syntactically
        # legal document nested past the interpreter's recursion limit raises
        # this instead of a ValueError -- not caught above, and otherwise a
        # traceback past the PlanningError-only boundary. The message names no
        # path and no depth number, both of which would just repeat what the
        # traceback would have shown.
        raise PlanningError(_too_deeply_nested(command_name)) from error
    if not isinstance(document, dict):
        raise PlanningError("file is not a JSON object")
    return document


def _too_deeply_nested(command_name: str) -> str:
    return (
        f"file nests too deeply for memriver to parse; flatten it and run "
        f"{command_name} again"
    )


def _render_json(document: dict[str, Any], command_name: str) -> str:
    try:
        rendered = json.dumps(document, indent=2, ensure_ascii=False,
                              allow_nan=False) + "\n"
    except ValueError as error:  # the value itself never goes in the message
        raise PlanningError(_non_standard_number(command_name)) from error
    except RecursionError as error:  # same foreign nesting, the encoding side
        raise PlanningError(_too_deeply_nested(command_name)) from error
    _parse_json_object(rendered, command_name)
    return rendered


def _render_toml(document: Any) -> str:
    rendered = tomlkit.dumps(document)
    try:
        tomlkit.parse(rendered)
    except ParseError as error:  # pragma: no cover - a tomlkit bug, not user input
        raise PlanningError(f"rendered TOML does not parse: {error}") from error
    return rendered
