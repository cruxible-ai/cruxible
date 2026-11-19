"""One problem an acceptance law finds: a dotted code and a plain message.

The capability, CaptureKind layout and provider registration laws return a
tuple of these, in the style of the Contract law: an empty tuple accepts, each
problem names one thing that must change.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LawProblem:
    code: str
    message: str


__all__ = ["LawProblem"]
