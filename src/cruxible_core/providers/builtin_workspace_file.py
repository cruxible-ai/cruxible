"""``workspace.file`` as a core built-in: structure one authorized workspace file read.

Core performs the read (``documents/workspace_file.py``): the workspace binding,
the allowed roots, the symlink walk, the no-follow open, the containment
re-check, the size cap and the ``source-read-receipt``. What reaches this module
is the outcome of that read -- bytes, base64-encoded, with their declared length
and digest -- and its whole job is to turn those bytes into a structured capture
body.

It is a port of ``cruxible_provider_workspace.file`` (cruxible-providers
db085204, the package that is no longer published): the same field validation,
strict base64 decode, length and digest checks and text/bytes structuring, so a
built-in run and a run of the package produce the same result envelope for the
same input. A parity golden pins that (tests/test_providers/
test_builtin_workspace_file.py).

The module is pure, and a guard test holds it to that: it imports no ``os``,
``socket`` or ``io`` and never names ``open``. It reads no file, contacts no
endpoint, reads no clock and consults no secret; its output is a function of
its input. That purity is what lets core run it in-process, inside the daemon,
with no process boundary or lease (``providers/builtin_runtime.py``).
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Mapping
from typing import Any

from cruxible_core.providers.provider_runtime_contract import (
    PROVIDER_RUNTIME_PROTOCOL,
    ProviderRuntimeRefusalCodeV1,
    ProviderRuntimeRefusalV1,
    ProviderRuntimeResultEnvelopeV1,
    ProviderRuntimeRunContextV1,
    ProviderRuntimeTraceV1,
)

INTERFACE_ID = "workspace.file"
CONTENT_ENCODING = "base64"
"""The only content encoding the interface admits: RFC 4648 section 4, padded, no line breaks."""

INPUT_FIELDS: tuple[str, ...] = (
    "logical_source",
    "commitment_digest",
    "content_encoding",
    "bytes",
    "byte_length",
    "bytes_digest",
)
"""The closed set of run-input fields. Anything else refuses rather than being ignored."""

BYTE_SIZE_CEILINGS: tuple[tuple[str, int], ...] = (
    ("tiny", 4_096),
    ("small", 65_536),
    ("medium", 1_048_576),
)
"""Inclusive upper bounds per size class, in order; above the last is ``large``."""

# The package's ``cruxible_provider_runtime.canonical.SHA256_RE``, matched the same way.
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BOM = "\ufeff"


def decode_declared_bytes(payload: Mapping[str, Any]) -> bytes | None:
    """Decode the run input's payload, or ``None`` when it is not a base64 string.

    Shared by the classifier and the adapter so the two cannot disagree about
    what the bytes are. Strict: the alphabet is RFC 4648 section 4 with padding,
    and a stray character (a line break included) is not a payload.
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


def content_kind_class(data: bytes) -> str:
    """``text`` for strict UTF-8 without a NUL byte, ``binary`` otherwise."""

    if b"\x00" in data:
        return "binary"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "binary"
    return "text"


def byte_size_class(length: int) -> str:
    for class_id, ceiling in BYTE_SIZE_CEILINGS:
        if length <= ceiling:
            return class_id
    return "large"


def classify(payload: Mapping[str, Any]) -> str | None:
    """The bucket, measured from the decoded bytes and never from a declaration.

    ``None`` when the payload carries nothing decodable. The declared
    ``byte_length`` and ``bytes_digest`` are deliberately not consulted: the
    adapter checks them, and a bucket measured from a declaration would be a
    bucket the caller chose.
    """

    data = decode_declared_bytes(payload)
    if data is None:
        return None
    return f"content_kind={content_kind_class(data)};byte_size={byte_size_class(len(data))}"


class WorkspaceFile:
    """Structure the bytes core read from one workspace file into a result envelope."""

    interface_id = INTERFACE_ID

    def __call__(self, context: ProviderRuntimeRunContextV1) -> ProviderRuntimeResultEnvelopeV1:
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
                    "the declared bytes_digest does not match the decoded payload; the adapter "
                    "will not structure bytes whose identity disagrees with the read receipt",
                    declared=declared_digest,
                    computed=computed_digest,
                ),
            )

        content = structure_bytes(data)
        metrics: dict[str, float] = {"byte_length": float(len(data))}
        if content["kind"] == "text":
            metrics["line_count"] = float(content["line_count"])
            metrics["character_count"] = float(content["character_count"])
        return ProviderRuntimeResultEnvelopeV1(
            protocol_version=PROVIDER_RUNTIME_PROTOCOL,
            run_id=context.run_id,
            status="ok",
            output={
                "input_bucket": context.input_bucket,
                "source": {
                    "logical_source": payload["logical_source"],
                    "commitment_digest": payload["commitment_digest"],
                    "bytes_digest": computed_digest,
                    "byte_length": len(data),
                },
                "content": content,
            },
            trace=ProviderRuntimeTraceV1(metrics=metrics),
        )


def _refusal(
    code: ProviderRuntimeRefusalCodeV1, message: str, **detail: Any
) -> ProviderRuntimeRefusalV1:
    return ProviderRuntimeRefusalV1(code=code, message=message, detail=detail)


def _refused(
    context: ProviderRuntimeRunContextV1, refusal: ProviderRuntimeRefusalV1
) -> ProviderRuntimeResultEnvelopeV1:
    return ProviderRuntimeResultEnvelopeV1(
        protocol_version=PROVIDER_RUNTIME_PROTOCOL,
        run_id=context.run_id,
        status="refused",
        refusal=refusal,
    )


def _validate_shape(payload: Mapping[str, Any]) -> ProviderRuntimeRefusalV1 | None:
    """Check the run input's shape, field by field, failing closed on any surprise."""

    unknown = sorted(set(payload) - set(INPUT_FIELDS))
    if unknown:
        return _refusal(
            "invalid_parameter",
            "the run input carries fields the interface does not declare",
            unknown=unknown,
            declared=list(INPUT_FIELDS),
        )
    missing = [name for name in INPUT_FIELDS if name not in payload]
    if missing:
        return _refusal(
            "invalid_parameter", "the run input is missing required fields", missing=missing
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


def structure_bytes(data: bytes) -> dict[str, Any]:
    """The capture body for ``data``: a text view when it is text, bytes otherwise."""

    if content_kind_class(data) != "text":
        return {
            "kind": "bytes",
            "encoding": "base64",
            "byte_length": len(data),
            "bytes": base64.b64encode(data).decode("ascii"),
        }
    text = data.decode("utf-8")
    lines = _lines(text)
    return {
        "kind": "text",
        "encoding": "utf-8",
        "bom": text.startswith(_BOM),
        "newline": _newline_style(text),
        "trailing_newline": text.endswith(("\n", "\r")),
        "line_count": len(lines),
        "character_count": len(text),
        "text": text,
        "lines": lines,
    }


def _newline_style(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    present = [name for name, count in (("lf", lf), ("crlf", crlf), ("cr", cr)) if count]
    if not present:
        return "none"
    if len(present) == 1:
        return present[0]
    return "mixed"


def _lines(text: str) -> list[str]:
    """Split at line feeds; drop one carriage return before each feed and the empty tail.

    Deliberately not ``str.splitlines``: that also splits on form feeds, vertical
    tabs and the Unicode separators, which a line-numbered citation into a source
    file does not expect. A lone carriage return is not a line break here either.
    """

    *terminated, tail = text.split("\n")
    lines = [part[:-1] if part.endswith("\r") else part for part in terminated]
    if tail:
        lines.append(tail)
    return lines


__all__ = [
    "BYTE_SIZE_CEILINGS",
    "CONTENT_ENCODING",
    "INPUT_FIELDS",
    "INTERFACE_ID",
    "WorkspaceFile",
    "byte_size_class",
    "classify",
    "content_kind_class",
    "decode_declared_bytes",
    "structure_bytes",
]
