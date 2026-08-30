from __future__ import annotations

from typing import Protocol


class ContentPolicy(Protocol):
    """Content-acceptance port consumed by the application facade.

    Binding semantics: ``check`` raises the stable application
    ``ContentRejected`` error without echoing rejected content. The Protocol
    itself does not import application errors; the concrete implementation
    raises the documented error.
    """

    def check(self, text: str, max_chars: int) -> None: ...
