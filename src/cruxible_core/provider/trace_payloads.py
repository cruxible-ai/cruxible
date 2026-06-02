"""Payload retention for provider execution traces."""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, Field

from cruxible_core.primitives import canonical_json, json_type_name

TracePayloadRetention = Literal["full", "preview", "metadata"]

DEFAULT_TRACE_PAYLOAD_INLINE_BYTES = 32 * 1024
_PREVIEW_KEY = "_cruxible_payload_preview"
_OMITTED_KEY = "_cruxible_payload_omitted"
_MAX_PREVIEW_DEPTH = 3
_MAX_PREVIEW_ITEMS = 8
_MAX_PREVIEW_STRING_CHARS = 256


class TracePayloadMetadata(BaseModel):
    """Metadata describing a persisted trace payload field."""

    retention: TracePayloadRetention = "preview"
    stored_inline: bool
    inline: bool
    byte_count: int
    sha256: str
    truncated: bool
    preview: dict[str, Any] = Field(default_factory=dict)


def retain_payload(
    payload: dict[str, Any],
    *,
    retention: TracePayloadRetention = "preview",
    inline_byte_limit: int = DEFAULT_TRACE_PAYLOAD_INLINE_BYTES,
) -> tuple[dict[str, Any], TracePayloadMetadata]:
    """Build the persisted payload field representation and metadata."""
    if retention not in ("full", "preview", "metadata"):
        raise ValueError(f"Unsupported trace payload retention: {retention}")
    payload_json = canonical_json(payload)
    payload_bytes = payload_json.encode("utf-8")
    byte_count = len(payload_bytes)
    sha256 = f"sha256:{hashlib.sha256(payload_bytes).hexdigest()}"

    if retention == "full":
        return (
            payload,
            TracePayloadMetadata(
                retention=retention,
                stored_inline=True,
                inline=True,
                byte_count=byte_count,
                sha256=sha256,
                truncated=False,
                preview={"retained_count": len(payload)},
            ),
        )

    if retention == "preview":
        preview_payload = _preview_dict(payload, depth=0)
        preview_metadata = dict(preview_payload[_PREVIEW_KEY])
        stored_inline = byte_count <= inline_byte_limit
        return (
            payload if stored_inline else preview_payload,
            TracePayloadMetadata(
                retention=retention,
                stored_inline=stored_inline,
                inline=stored_inline,
                byte_count=byte_count,
                sha256=sha256,
                truncated=not stored_inline,
                preview=preview_metadata,
            ),
        )

    omitted = {
        _OMITTED_KEY: {
            "retention": retention,
            "sha256": sha256,
            "byte_count": byte_count,
        }
    }
    return (
        omitted,
        TracePayloadMetadata(
            retention=retention,
            stored_inline=False,
            inline=False,
            byte_count=byte_count,
            sha256=sha256,
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
        retained = [
            _preview_value(item, depth=depth + 1)
            for item in value[:_MAX_PREVIEW_ITEMS]
        ]
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
    preview = {
        key: _preview_value(value[key], depth=depth + 1)
        for key in retained_keys
    }
    preview[_PREVIEW_KEY] = {
        "item_count": len(value),
        "retained_keys": retained_keys,
        "retained_count": len(retained_keys),
        "omitted_count": max(0, len(keys) - len(retained_keys)),
    }
    return preview
