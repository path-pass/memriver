"""Display helpers the umbrella's management surfaces share, and the git-root finder.

Project identity and every directory rule live in memriver_core; nothing here
reads or writes the store. ``find_git_root`` places the Cursor/Kiro static
instruction file at the nearest git root; ``visible`` neutralises invisible
characters before a store- or path-derived string is printed.
"""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path

# What the management surfaces (the project commands, doctor) neutralise
# before printing a store- or path-derived string: a directory or file name the
# user can hand-edit to carry a newline (forging a second output line) or an
# ANSI escape (a raw terminal control sequence). Categorised rather than
# enumerated, because a code-point list keeps missing things a real name
# carries: Cc is the C0/C1 controls, Cf the format controls (U+202E
# RIGHT-TO-LEFT OVERRIDE reorders the line a terminal draws without being a
# control character), Zl/Zp the line and paragraph separators U+2028/U+2029,
# and Cs the lone surrogates that a filename no codec accepts arrives as --
# those turn back into their original raw byte the moment stdout, which uses
# surrogateescape on a terminal, encodes them.
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def find_git_root(start: Path) -> Path | None:
    """The nearest ``.git`` root at or above ``start``, or ``None`` outside a repo."""
    cur = start.resolve()
    for p in [cur, *cur.parents]:
        if (p / ".git").exists():
            return p
    return None


def visible(text: str) -> str:
    """``text`` with every invisible character replaced by one space, one for one.

    Unlike ``single_line`` this never collapses or strips ordinary spaces: a
    management surface shows a path as the user spelled it, so a root named
    ``two  spaces`` stays recognisable. The agent-facing header keeps
    ``single_line`` and its length cap.
    """
    return "".join(
        " " if unicodedata.category(char) in _INVISIBLE_CATEGORIES else char
        for char in text
    )


def count_child_git_markers(directory: Path) -> int | None:
    """Direct children holding a ``.git`` entry of any kind; ``None`` when unlistable."""
    try:
        with os.scandir(directory) as children:
            return sum(1 for child in children
                       if child.is_dir(follow_symlinks=False)
                       and os.path.lexists(os.path.join(child.path, ".git")))
    except OSError:
        return None
