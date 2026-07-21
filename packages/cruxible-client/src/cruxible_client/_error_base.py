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


class InvalidContinuationError(CoreError):
    """A continuation token is malformed or bound to a different read."""

    error_code = "invalid_continuation"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid continuation token: {reason}. Restart the read from the start.")


class StaleContinuationError(CoreError):
    """A continuation token was minted at a different read revision or config."""

    error_code = "stale_continuation"

    def __init__(
        self,
        *,
        token_read_revision: int | None = None,
        current_read_revision: int | None = None,
        reason: str | None = None,
    ) -> None:
        self.token_read_revision = token_read_revision
        self.current_read_revision = current_read_revision
        self.reason = reason or "state changed between pages"
        detail = self.reason
        if token_read_revision is not None and current_read_revision is not None:
            detail += (
                f" (token read_revision={token_read_revision}, "
                f"current read_revision={current_read_revision})"
            )
        super().__init__(f"Stale continuation token: {detail}. Restart the read from the start.")
