from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ContentPolicy(Protocol):
    """Content-acceptance port consumed by the memory and maintenance services.

    Binding semantics: ``check`` raises the stable core
    ``ContentRejected`` error without echoing rejected content. The Protocol
    itself does not import the error taxonomy; the concrete implementation
    raises the documented error.
    """

    def check(self, text: str, max_chars: int) -> None: ...


@dataclass(frozen=True)
class Verdict:
    """Why a text may not be stored.

    ``category`` is a short ascii label the refusal names, for example
    "instruction"; "unavailable" means the classifier could not decide. ``detail``
    is set for "unavailable" only: a fixed reason such as "timeout", never words
    from the checked text or from a model's answer.
    """

    category: str
    detail: str = ""


class ContentClassifier(Protocol):
    """Optional check of a write's new text, asked before the write transaction.

    ``classify`` returns None when the text may be stored and a Verdict when it
    may not. An implementation that cannot decide returns
    ``Verdict("unavailable", detail=...)``: the write is refused, never let
    through. ``changed_by`` names the caller so an implementation can apply its
    own per-source switches; core holds no policy about sources. An exception
    raised here is a bug and reaches the caller unchanged.
    """

    def classify(self, text: str, *, changed_by: str) -> Verdict | None: ...
