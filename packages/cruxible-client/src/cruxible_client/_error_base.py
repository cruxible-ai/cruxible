"""Dependency-free exception types shared by Cruxible Core and its client."""

from __future__ import annotations


class CoreError(Exception):
    """Base exception for all local and reconstructed Cruxible errors."""

    def __init__(self, message: str, *, mutation_receipt_id: str | None = None) -> None:
        self.mutation_receipt_id = mutation_receipt_id
        super().__init__(message)

    def _receipt_suffix(self) -> str:
        if self.mutation_receipt_id:
            return f" (receipt: {self.mutation_receipt_id})"
        return ""

    def __str__(self) -> str:
        return super().__str__() + self._receipt_suffix()


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
