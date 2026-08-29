from __future__ import annotations

import re

MAX_BODY_CHARS = 8000

_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("private key block", re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----")),
    ("Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("credential assignment",
     re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{12,}['\"]")),
]


class GateError(ValueError):
    pass


def check_content(body: str) -> None:
    if len(body) > MAX_BODY_CHARS:
        raise GateError(f"content too large ({len(body)} > {MAX_BODY_CHARS} chars); "
                        "store a summary or pointer instead")
    for label, pat in _SECRET_PATTERNS:
        if pat.search(body):
            raise GateError(f"content rejected: looks like a secret ({label}). "
                            "Store a pointer (e.g. 'token is in 1Password item X') instead.")
