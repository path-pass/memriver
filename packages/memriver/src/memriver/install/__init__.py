"""``memriver install``: plan, render, and apply harness configuration edits.

This package never imports ``memriver_core`` (enforced by
tests/test_architecture.py): it edits harness config files, it does not touch
the memory store.
"""

from __future__ import annotations

from .editors import (
    MARKER_BEGIN,
    MARKER_END,
    TAKEOVER_NOTICE,
    EditOperation,
    EditorKind,
    EditResult,
    PlanningError,
    Snapshot,
    Target,
    apply_edit,
    hook_array_identity_merge,
    json_object_merge,
    marker_block,
    render_change_summary,
    toml_roundtrip,
)

__all__ = [
    "MARKER_BEGIN",
    "MARKER_END",
    "TAKEOVER_NOTICE",
    "EditOperation",
    "EditResult",
    "EditorKind",
    "PlanningError",
    "Snapshot",
    "Target",
    "apply_edit",
    "hook_array_identity_merge",
    "json_object_merge",
    "marker_block",
    "render_change_summary",
    "toml_roundtrip",
]
