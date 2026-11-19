"""Dependency-free exception types shared by Cruxible Core and its client."""

from __future__ import annotations


class CoreError(Exception):
    """Base exception for all local and reconstructed Cruxible errors."""


def printable(value: str) -> str:
    """Render caller-supplied prose so it cannot forge a line of daemon output.

    Operator prose (a decommission reason, a refusal detail) is echoed back to
    a terminal. A newline followed by ``Error: ...`` reads as the daemon's own
    line, so every non-printable character is rendered as its escape instead.
    Printable text, including non-ASCII, is returned unchanged.
    """

    return "".join(
        character if character.isprintable() else character.encode("unicode_escape").decode("ascii")
        for character in value
    )
