"""Core's built-in workspace reads: check the bytes core read for one file.

Core performs the read (``documents/workspace_file.py``): the workspace binding,
the allowed roots, the symlink walk, the no-follow open, the containment
re-check, the size budget and the ``source-read-receipt``. What reaches this
module is the outcome of one file's read -- bytes, base64-encoded, with their
declared length and digest (the handoff) -- and its job is to refuse bytes whose
encoding, length or digest disagrees with what core declared. It returns an
empty output: core commits the bytes it read as the call's capture field
(``workspace.file``) or as one member of its list (``workspace.files``), so the
call's value is exactly the base kit's result, ``{capture}`` or ``{captures}``.

The module is pure, and a guard test holds it to that: it imports no ``os``,
``socket`` or ``io`` and never names ``open``. It reads no file, contacts no
endpoint, reads no clock and consults no secret; its output is a function of
its input. That purity is what lets core run it in-process, inside the daemon,
with no process boundary or lease (``providers/builtin_runtime.py``). Its
behaviour is pinned by ``tests/fixtures/workspace_file_parity.json``
(tests/test_providers/test_builtin_workspace_file.py).
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Mapping
from typing import Any

from cruxible_core.providers.provider_runtime_contract import (
    PROVIDER_RUNTIME_PROTOCOL,
    ProviderRuntimeRefusal,
    ProviderRuntimeResultEnvelope,
    ProviderRuntimeRunContext,
    ProviderRuntimeTrace,
)

OPERATIONS: tuple[str, ...] = ("workspace.file", "workspace.files")
"""The operations the adapter serves: one file per call, for a one-path or a glob read."""
CONTENT_ENCODING = "base64"
"""The only content encoding the handoff admits: RFC 4648 section 4, padded, no line breaks."""

INPUT_FIELDS: tuple[str, ...] = (
    "logical_source",
    "commitment_digest",
    "content_encoding",
    "bytes",
    "byte_length",
    "bytes_digest",
)
"""The closed set of handoff fields. Anything else refuses rather than being ignored."""

# The package's ``cruxible_provider_runtime.canonical.SHA256_RE``, matched the same way.
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def decode_declared_bytes(payload: Mapping[str, Any]) -> bytes | None:
    """Decode the handoff's payload, or ``None`` when it is not a base64 string.

    Strict: the alphabet is RFC 4648 section 4 with padding, and a stray
    character (a line break included) is not a payload.
    """

    if payload.get("content_encoding") != CONTENT_ENCODING:
        return None
    encoded = payload.get("bytes")
    if not isinstance(encoded, str):
        return None
    try:
        return base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError):
        return None


class WorkspaceFile:
    """Check the bytes core read from one workspace file; return an empty output."""

    #: The operations this entrypoint serves, as a runtime child reads them.
    operations = OPERATIONS

    def __call__(self, context: ProviderRuntimeRunContext) -> ProviderRuntimeResultEnvelope:
        payload = context.input
        refusal = _validate_shape(payload)
        if refusal is not None:
            return _refused(context, refusal)

        data = decode_declared_bytes(payload)
        if data is None:
            return _refused(
                context,
                _refusal(
                    "invalid_parameter",
                    "bytes is not a base64 payload under the declared content encoding",
                    field="bytes",
                    content_encoding=CONTENT_ENCODING,
                ),
            )

        declared_length = payload["byte_length"]
        if len(data) != declared_length:
            return _refused(
                context,
                _refusal(
                    "mismatched_lengths",
                    "the declared byte_length does not match the decoded payload",
                    declared=declared_length,
                    decoded=len(data),
                ),
            )

        computed_digest = "sha256:" + hashlib.sha256(data).hexdigest()
        declared_digest = payload["bytes_digest"]
        if computed_digest != declared_digest:
            return _refused(
                context,
                _refusal(
                    "provider_declined",
                    "the declared bytes_digest does not match the decoded payload; the "
                    "built-in will not answer for bytes whose identity disagrees with the "
                    "read receipt",
                    declared=declared_digest,
                    computed=computed_digest,
                ),
            )

        return ProviderRuntimeResultEnvelope(
            protocol_version=PROVIDER_RUNTIME_PROTOCOL,
            run_id=context.run_id,
            status="ok",
            output={},
            trace=ProviderRuntimeTrace(metrics={"byte_length": float(len(data))}),
        )


def _refusal(code: str, message: str, **detail: Any) -> ProviderRuntimeRefusal:
    return ProviderRuntimeRefusal(code=code, message=message, detail=detail)


def _refused(
    context: ProviderRuntimeRunContext, refusal: ProviderRuntimeRefusal
) -> ProviderRuntimeResultEnvelope:
    return ProviderRuntimeResultEnvelope(
        protocol_version=PROVIDER_RUNTIME_PROTOCOL,
        run_id=context.run_id,
        status="refused",
        refusal=refusal,
    )


def _validate_shape(payload: Mapping[str, Any]) -> ProviderRuntimeRefusal | None:
    """Check the handoff's shape, field by field, failing closed on any surprise."""

    unknown = sorted(set(payload) - set(INPUT_FIELDS))
    if unknown:
        return _refusal(
            "invalid_parameter",
            "the handoff carries fields it does not declare",
            unknown=unknown,
            declared=list(INPUT_FIELDS),
        )
    missing = [name for name in INPUT_FIELDS if name not in payload]
    if missing:
        return _refusal(
            "invalid_parameter", "the handoff is missing required fields", missing=missing
        )

    logical_source = payload["logical_source"]
    if (
        not isinstance(logical_source, str)
        or not logical_source
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in logical_source)
    ):
        return _refusal(
            "invalid_parameter",
            "logical_source must be a non-empty string without control characters",
            field="logical_source",
        )
    for field in ("commitment_digest", "bytes_digest"):
        value = payload[field]
        if not isinstance(value, str) or not _SHA256_RE.match(value):
            return _refusal(
                "invalid_parameter",
                f"{field} must be spelled sha256:<64 lowercase hex>",
                field=field,
            )
    if payload["content_encoding"] != CONTENT_ENCODING:
        return _refusal(
            "invalid_parameter",
            f"content_encoding must be {CONTENT_ENCODING!r}",
            field="content_encoding",
            declared=payload["content_encoding"],
            supported=[CONTENT_ENCODING],
        )
    if not isinstance(payload["bytes"], str):
        return _refusal("invalid_parameter", "bytes must be a string", field="bytes")
    length = payload["byte_length"]
    if not isinstance(length, int) or isinstance(length, bool) or length < 0:
        return _refusal(
            "invalid_parameter",
            "byte_length must be a non-negative integer",
            field="byte_length",
        )
    return None


__all__ = [
    "CONTENT_ENCODING",
    "INPUT_FIELDS",
    "OPERATIONS",
    "WorkspaceFile",
    "decode_declared_bytes",
]
