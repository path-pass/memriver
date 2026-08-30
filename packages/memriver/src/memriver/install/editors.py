"""The four format-specific editors ``memriver install`` edits files through.

Every function here is pure: text in, ``EditResult`` out. No filesystem, no
harness knowledge, no ``memriver_core`` -- the install surface plans and
renders, it never runs memory policy.

Three rules hold across all four editors.

*Foreign content survives.* An edit touches exactly one key path or one marker
region; everything else in the user's file is carried through untouched, and
TOML keeps its original formatting because tomlkit round-trips it.

*Ambiguity fails, it never guesses.* Two memriver hook entries, a memriver
handler sharing a group with someone else's, an unpaired marker: each raises
``PlanningError`` so the whole plan aborts before a single byte is written.

*Idempotency is byte-identical.* When the file already holds the expected
semantic value, the editor returns the original text with ``changed=False``,
so a reinstall rewrites nothing and reformats nothing.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
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
    document = _parse_json_object(source)
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
    return EditResult(rendered=_render_json(document), changed=True, takeover=present)


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
    document = _parse_json_object(source)
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
        return EditResult(rendered=_render_json(document), changed=True, takeover=False)

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
    return EditResult(rendered=_render_json(document), changed=True, takeover=True)


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
    return EditResult(rendered=_render_toml(document), changed=True, takeover=present)


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
    """Append or replace the single ``memriver:begin/end`` region of a text file."""
    block = _block_text(body)
    start, stop = _marker_span(source)
    if start is None:
        separator = "\n\n" if source.strip() else ""
        rendered = source.rstrip("\n") + separator + block + "\n"
    elif source[start:stop] == block:
        return EditResult(rendered=source, changed=False, takeover=False)
    else:
        rendered = source[:start] + block + source[stop:]
    _marker_span(rendered)
    return EditResult(rendered=rendered, changed=True, takeover=start is not None)


def _block_text(body: object) -> str:
    if not isinstance(body, str):
        raise PlanningError("a marker-block edit needs its block as text")
    inner = body.strip()
    if inner.startswith(MARKER_BEGIN) and inner.endswith(MARKER_END):
        inner = inner[len(MARKER_BEGIN) : -len(MARKER_END)].strip()
    if MARKER_BEGIN in inner or MARKER_END in inner:
        raise PlanningError("the managed block body must not contain memriver markers")
    return f"{MARKER_BEGIN}\n{inner}\n{MARKER_END}"


def _marker_span(text: str) -> tuple[int, int] | tuple[None, None]:
    begins, ends = _positions(text, MARKER_BEGIN), _positions(text, MARKER_END)
    if len(begins) > 1 or len(ends) > 1 or len(begins) != len(ends):
        raise PlanningError(
            f"expected one memriver marker pair, found {len(begins)} begin and "
            f"{len(ends)} end markers; fix the markers and run install again"
        )
    if not begins:
        return None, None
    if begins[0] > ends[0]:
        raise PlanningError(
            f"{MARKER_END} appears before {MARKER_BEGIN}; fix the markers and run "
            "install again"
        )
    return begins[0], ends[0] + len(MARKER_END)


def validate_document(text: str, kind: EditorKind) -> None:
    """Re-parse a fully rendered document, raising ``PlanningError`` if unsound.

    The editors already validate what they render; the orchestrator runs this
    over the *final* text of every target -- once after planning and again
    after re-applying only the accepted edits -- so a file is proven whole
    before it is a candidate for replacement.
    """
    if kind == "marker-block":
        _marker_span(text)
    elif kind == "toml-table":
        try:
            tomlkit.parse(text)
        except ParseError as error:
            raise PlanningError(f"file is not valid TOML: {error}") from error
    else:
        _parse_json_object(text)


def _positions(text: str, marker: str) -> list[int]:
    found, index = [], text.find(marker)
    while index != -1:
        found.append(index)
        index = text.find(marker, index + len(marker))
    return found


# --- change summary --------------------------------------------------------


def render_change_summary(operation: EditOperation, result: EditResult) -> str:
    """Render the label, the managed region, and the NEW fragment -- nothing else.

    The pre-existing fragment is never passed in, so it can never leak into
    output; a takeover says only that something differed. [spec 5.4, DEFERRED-2]
    """
    region = (
        f"{MARKER_BEGIN} ... {MARKER_END}"
        if operation.kind == "marker-block"
        else _dotted(operation.key_path)
    )
    lines = [operation.label, region, _fragment(operation)]
    if result.takeover:
        lines.append(HARNESS_SETTING_TAKEOVER_NOTICE if operation.harness_owned
                     else TAKEOVER_NOTICE)
    return "\n".join(lines) + "\n"


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


def _parse_json_object(source: str) -> dict[str, Any]:
    if not source.strip():
        return {}
    try:
        document = json.loads(source)
    except json.JSONDecodeError as error:
        raise PlanningError(f"file is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise PlanningError("file is not a JSON object")
    return document


def _render_json(document: dict[str, Any]) -> str:
    rendered = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    _parse_json_object(rendered)
    return rendered


def _render_toml(document: Any) -> str:
    rendered = tomlkit.dumps(document)
    try:
        tomlkit.parse(rendered)
    except ParseError as error:  # pragma: no cover - a tomlkit bug, not user input
        raise PlanningError(f"rendered TOML does not parse: {error}") from error
    return rendered
