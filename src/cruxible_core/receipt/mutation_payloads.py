"""Payload retention for governed mutation receipts.

Mirrors the provider trace payload retention machinery
(:mod:`cruxible_core.provider.trace_payloads`, introduced for execution
traces) for mutation payloads. The *mutation payload* is the structured input
that produced a mutation receipt -- the ``parameters`` dict carried by the
root ``mutation`` node of the receipt DAG.

Every retention mode stamps a content-addressed ``payload_digest`` and a
``byte_count`` onto the mutation node. The digest is the core value: it lets a
stale on-disk payload be mechanically matched against a committed receipt
(hash equality), catches replay drift, and shares its ``sha256:``-prefixed
identity shape with the existing ``apply_digest`` pattern.

Modes (option "b": metadata means metadata, preview means preview):

* ``metadata`` -- digest + byte_count ONLY. The body is NEVER carried,
  regardless of size; it is always replaced by a compact omitted marker
  carrying the digest + byte_count.
* ``preview`` -- digest + byte_count + a bounded structural preview ONLY,
  regardless of size. The raw full body is NEVER carried.
* ``full``    -- digest + byte_count + the complete payload body retained
  inline. (The mutation payload already lives inline on the receipt, so this
  is a clean inline reuse -- no separate content-addressed body store is
  required, mirroring how trace ``full`` retention keeps its body inline.)

The digest and byte_count are always computed over the ORIGINAL full canonical
payload (before any reduction), so replay-drift detection still works.

``full`` retention accepts an optional ``max_inline_bytes`` ceiling. Retention
that is unbounded by construction is a liability wherever it is applied by
policy rather than by operator choice -- the refusal retention floor is exactly
that case. Above the ceiling the body is replaced by the Stage D convention
(:data:`cruxible_core.procedure.types.MAX_PROCEDURE_EVIDENCE_BYTES`): digest +
byte_count + a bounded UTF-8 head + an explicit truncation marker. Never a
silent truncation, never an unbounded retention.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, Field

from cruxible_core.primitives import canonical_json, json_type_name

MutationPayloadRetention = Literal["full", "preview", "metadata"]

MAX_RETAINED_PAYLOAD_BYTES = 256 * 1024
"""Ceiling on the canonical JSON bytes any ONE payload may retain inline.

Matches the Stage D artifact cap (``MAX_PROCEDURE_EVIDENCE_BYTES``) so the two
"how much of a body does a receipt keep" answers in the system are the same
number. Applies only where ``full`` retention is imposed by policy rather than
chosen by the operator -- today that is the refusal retention floor, whose
payload is attacker-influenceable in size and which no configured mode can
shrink.
"""

RETAINED_PAYLOAD_HEAD_BYTES = 4096
"""Bounded UTF-8 head retained for a payload that exceeds the ceiling.

Mirrors ``PROCEDURE_EVIDENCE_HEAD_BYTES``: enough to identify what was proposed,
far short of retaining the proposal.
"""

_PREVIEW_KEY = "_cruxible_payload_preview"
_OMITTED_KEY = "_cruxible_payload_omitted"
_TRUNCATED_KEY = "_cruxible_payload_truncated"
_MAX_PREVIEW_DEPTH = 3
_MAX_PREVIEW_ITEMS = 8
_MAX_PREVIEW_STRING_CHARS = 256


class MutationPayloadMetadata(BaseModel):
    """Metadata describing a persisted mutation payload field.

    ``payload_digest`` is a ``sha256:``-prefixed content address of the
    canonical JSON encoding of the mutation payload, and ``byte_count`` is the
    length of that canonical encoding in bytes. Both are stamped for every
    retention mode.
    """

    retention: MutationPayloadRetention = "metadata"
    stored_inline: bool
    byte_count: int
    payload_digest: str
    truncated: bool
    preview: dict[str, Any] = Field(default_factory=dict)


def compute_payload_digest(payload: dict[str, Any]) -> tuple[str, int]:
    """Return ``(payload_digest, byte_count)`` for a mutation payload.

    The digest is the SHA-256 of the canonical JSON encoding, prefixed with
    ``sha256:`` to match the ``apply_digest`` / trace digest identity shape.
    The byte count is the length of that same canonical encoding, so digest and
    byte count always describe the same bytes.
    """
    payload_bytes = canonical_json(payload).encode("utf-8")
    digest = f"sha256:{hashlib.sha256(payload_bytes).hexdigest()}"
    return digest, len(payload_bytes)


def retain_mutation_payload(
    payload: dict[str, Any],
    *,
    retention: MutationPayloadRetention = "metadata",
    max_inline_bytes: int | None = None,
    head_byte_limit: int = RETAINED_PAYLOAD_HEAD_BYTES,
) -> tuple[dict[str, Any], MutationPayloadMetadata]:
    """Build the retained payload representation and its metadata.

    Returns ``(retained_payload, metadata)``. ``retained_payload`` is what
    should be persisted in place of the raw payload for this mode; it becomes
    the single canonical value carried by every persisted copy of the receipt.
    ``metadata`` always carries ``payload_digest`` and ``byte_count``, computed
    over the ORIGINAL full canonical payload regardless of the mode.

    Semantics (option "b"), applied regardless of payload size:

    * ``full``     -- the complete body, retained inline verbatim, unless
      ``max_inline_bytes`` is supplied and the canonical encoding exceeds it:
      then digest + byte_count + a bounded head + an explicit truncation
      marker. ``None`` (the default) keeps the historical unbounded behaviour
      for operator-configured ``full`` retention.
    * ``preview``  -- a bounded structural preview ONLY; never the full body.
    * ``metadata`` -- a compact omitted marker (digest + byte_count) ONLY;
      never any body.
    """
    if retention not in ("full", "preview", "metadata"):
        raise ValueError(f"Unsupported mutation payload retention: {retention}")

    # Digest + byte_count are always computed over the original full payload,
    # before any reduction, so replay-drift detection stays accurate.
    encoded = canonical_json(payload).encode("utf-8")
    digest = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    byte_count = len(encoded)

    if retention == "full":
        if max_inline_bytes is not None and byte_count > max_inline_bytes:
            truncated = {
                _TRUNCATED_KEY: {
                    "retention": retention,
                    "payload_digest": digest,
                    "byte_count": byte_count,
                    "max_retained_bytes": max_inline_bytes,
                    "truncated": True,
                    "head": encoded[:head_byte_limit].decode("utf-8", errors="replace"),
                }
            }
            return (
                truncated,
                MutationPayloadMetadata(
                    retention=retention,
                    stored_inline=False,
                    byte_count=byte_count,
                    payload_digest=digest,
                    truncated=True,
                    preview=dict(truncated[_TRUNCATED_KEY]),
                ),
            )
        return (
            payload,
            MutationPayloadMetadata(
                retention=retention,
                stored_inline=True,
                byte_count=byte_count,
                payload_digest=digest,
                truncated=False,
                preview={"retained_count": len(payload)},
            ),
        )

    if retention == "preview":
        preview_payload = _preview_dict(payload, depth=0)
        preview_metadata = dict(preview_payload[_PREVIEW_KEY])
        return (
            preview_payload,
            MutationPayloadMetadata(
                retention=retention,
                stored_inline=False,
                byte_count=byte_count,
                payload_digest=digest,
                truncated=True,
                preview=preview_metadata,
            ),
        )

    omitted = {
        _OMITTED_KEY: {
            "retention": retention,
            "payload_digest": digest,
            "byte_count": byte_count,
        }
    }
    return (
        omitted,
        MutationPayloadMetadata(
            retention=retention,
            stored_inline=False,
            byte_count=byte_count,
            payload_digest=digest,
            truncated=True,
            preview=dict(omitted[_OMITTED_KEY]),
        ),
    )


def _preview_value(value: Any, *, depth: int) -> Any:
    if depth >= _MAX_PREVIEW_DEPTH:
        summary: dict[str, Any] = {
            "_cruxible_type": json_type_name(value),
            "truncated": True,
        }
        if isinstance(value, dict):
            summary["item_count"] = len(value)
        elif isinstance(value, list):
            summary["item_count"] = len(value)
        elif isinstance(value, str):
            summary["char_count"] = len(value)
        return summary
    if isinstance(value, dict):
        return _preview_dict(value, depth=depth)
    if isinstance(value, list):
        retained = [_preview_value(item, depth=depth + 1) for item in value[:_MAX_PREVIEW_ITEMS]]
        return {
            "_cruxible_type": json_type_name(value),
            "item_count": len(value),
            "retained_count": len(retained),
            "omitted_count": max(0, len(value) - len(retained)),
            "items": retained,
        }
    if isinstance(value, str) and len(value) > _MAX_PREVIEW_STRING_CHARS:
        return {
            "_cruxible_type": json_type_name(value),
            "char_count": len(value),
            "truncated": True,
            "prefix": value[:_MAX_PREVIEW_STRING_CHARS],
        }
    return value


def _preview_dict(value: dict[str, Any], *, depth: int) -> dict[str, Any]:
    keys = sorted(value)
    retained_keys = keys[:_MAX_PREVIEW_ITEMS]
    preview = {key: _preview_value(value[key], depth=depth + 1) for key in retained_keys}
    preview[_PREVIEW_KEY] = {
        "item_count": len(value),
        "retained_keys": retained_keys,
        "retained_count": len(retained_keys),
        "omitted_count": max(0, len(keys) - len(retained_keys)),
    }
    return preview
