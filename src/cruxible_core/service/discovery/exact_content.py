"""Exact-content Claim values as text, for every read verb that shows a value.

An exact-content Claim's object is a digest of the bytes the Claim IS (a ruling,
a method law) and the span of them it states. ``get`` and ``query`` show that
value as its UTF-8 text, read from the body store the bytes were committed to,
so reading a ruling is one call rather than a Claim read, a Capture read and a
base64 decode. The digest stays beside the text as its proof.

When the value cannot be text, a typed ``PlaybillExactContentRefV1`` marker
stands in for it, and nothing raises:

- ``binary``: the bytes are not UTF-8 text (or hold a NUL byte);
- ``withheld``: the caller may not read bodies, which is the same boundary a
  Capture read or a Document body read enforces;
- ``unavailable``: the store no longer holds the bytes, or they fail verification.
"""

from __future__ import annotations

from cruxible_client.contracts.claims import ExactContentClaimObject
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.get_reads import PlaybillExactContentRefV1
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.storage.cas import BodyAccessContext

ExactContentValue = str | PlaybillExactContentRefV1


def _span_length(span: tuple[int, int] | None) -> int | None:
    return None if span is None else span[1] - span[0]


class ExactContentReader:
    """Reads exact-content values as text for one answer, each digest at most once."""

    def __init__(self, instance: PlaybillInstance, access: BodyAccessContext | None) -> None:
        self._instance = instance
        self._access = access if access is not None and access.can_read_body else None
        self._bytes: dict[str, bytes | None] = {}

    def _read(self, digest: str) -> bytes | None:
        if digest not in self._bytes:
            assert self._access is not None
            try:
                self._bytes[digest] = self._instance.body_store().read(digest, access=self._access)
            except (PlaybillError, OSError, ValueError):
                self._bytes[digest] = None
        return self._bytes[digest]

    def value(self, digest: str, span: tuple[int, int] | None = None) -> ExactContentValue:
        """The value's text, or the marker that says why it is shown by digest."""

        if self._access is None:
            return PlaybillExactContentRefV1(
                exact_content="withheld", content_digest=digest, length=_span_length(span)
            )
        content = self._read(digest)
        if content is None or (span is not None and span[1] > len(content)):
            return PlaybillExactContentRefV1(
                exact_content="unavailable", content_digest=digest, length=_span_length(span)
            )
        selected = content if span is None else content[span[0] : span[1]]
        if b"\x00" not in selected:
            try:
                return selected.decode("utf-8")
            except UnicodeDecodeError:
                pass
        return PlaybillExactContentRefV1(
            exact_content="binary", content_digest=digest, length=len(selected)
        )

    def of(self, obj: ExactContentClaimObject) -> ExactContentValue:
        """One accepted exact-content object's value."""

        span = None if obj.span is None else (obj.span.start_byte, obj.span.end_byte)
        return self.value(obj.content_digest, span)

    def text(self, digest: str, span: tuple[int, int] | None = None) -> str | None:
        """The value's text, or None when it has none to search."""

        value = self.value(digest, span)
        return value if isinstance(value, str) else None


__all__ = ["ExactContentReader", "ExactContentValue"]
