"""Opaque continuation tokens for resumable bounded reads.

A continuation token binds a pagination cursor to the exact read it came from:
the instance, the active config (by digest), the monotonic ``read_revision``,
and a hash of the result-shaping filters. Replaying a token after ANY state
mutation (revision moved) or config change yields a typed
:class:`StaleContinuationError` (HTTP 409) telling the caller to restart;
structurally broken or re-bound tokens yield :class:`InvalidContinuationError`
(HTTP 422).

The token is deliberately opaque (base64url JSON) and carries no result data —
only the cursor. Freshness is proven by ``read_revision``, never by receipts:
receipts prove a computation happened, not that its inputs are still current.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from cruxible_core.errors import InvalidContinuationError, StaleContinuationError
from cruxible_core.primitives import canonical_json

ContinuationSurface = Literal["list", "query_catalog", "neighborhood"]

TOKEN_VERSION = 1


class ContinuationToken(BaseModel):
    """Decoded continuation token payload."""

    model_config = ConfigDict(extra="forbid")

    v: int
    surface: ContinuationSurface
    instance_key: str
    config_digest: str
    read_revision: int
    filter_hash: str
    cursor: dict[str, int]
    # Keyset high-water mark for resources whose backing table can grow
    # without bumping read_revision (receipts): string-valued components
    # (created_at / receipt_id) that page 2 resumes strictly older than.
    keyset: dict[str, str] | None = None


def compute_filter_hash(params: Mapping[str, Any]) -> str:
    """Deterministic hash of the result-shaping filters a token is bound to.

    Only parameters that change WHICH items a page contains belong here;
    serialization-only knobs (profile, field projection) and per-page budgets
    are deliberately excluded so they may vary between pages.
    """
    canonical = canonical_json(
        {key: value for key, value in params.items() if value is not None},
        default=str,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def mint_continuation_token(
    *,
    surface: ContinuationSurface,
    instance_key: str,
    config_digest: str,
    read_revision: int,
    filter_hash: str,
    cursor: Mapping[str, int],
    keyset: Mapping[str, str] | None = None,
) -> str:
    """Encode an opaque continuation token for a truncated, resumable read."""
    payload = ContinuationToken(
        v=TOKEN_VERSION,
        surface=surface,
        instance_key=instance_key,
        config_digest=config_digest,
        read_revision=read_revision,
        filter_hash=filter_hash,
        cursor=dict(cursor),
        keyset=dict(keyset) if keyset is not None else None,
    )
    # exclude_none keeps offset-only tokens byte-identical to pre-keyset ones.
    raw = canonical_json(payload.model_dump(mode="json", exclude_none=True)).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_continuation_token(raw: str) -> ContinuationToken:
    """Decode and structurally validate a continuation token.

    Raises :class:`InvalidContinuationError` (422) for anything that is not a
    well-formed token of the current version.
    """
    if not raw or not raw.strip():
        raise InvalidContinuationError("token is empty")
    padded = raw.strip() + "=" * (-len(raw.strip()) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(decoded)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidContinuationError("token is not base64url-encoded JSON") from exc
    if not isinstance(payload, dict):
        raise InvalidContinuationError("token payload must be a JSON object")
    try:
        token = ContinuationToken.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first.get("loc", ())) or "payload"
        raise InvalidContinuationError(f"token field '{location}': {first['msg']}") from exc
    if token.v != TOKEN_VERSION:
        raise InvalidContinuationError(
            f"token version {token.v} is not supported (expected {TOKEN_VERSION})"
        )
    return token


def validate_continuation_token(
    token: ContinuationToken,
    *,
    surface: ContinuationSurface,
    instance_key: str,
    config_digest: str,
    read_revision: int,
    filter_hash: str,
) -> None:
    """Check a decoded token against the current read context.

    Binding violations (wrong surface / instance / filters) raise
    :class:`InvalidContinuationError` (422); drift in state or config between
    pages raises :class:`StaleContinuationError` (409) — the caller restarts.
    """
    if token.surface != surface:
        raise InvalidContinuationError(
            f"token was minted for surface '{token.surface}', not '{surface}'"
        )
    if token.instance_key != instance_key:
        raise InvalidContinuationError("token was minted for a different instance")
    if token.filter_hash != filter_hash:
        raise InvalidContinuationError(
            "token was minted for a different filter set; repeat the original filters"
        )
    if token.config_digest != config_digest:
        raise StaleContinuationError(reason="config changed between pages")
    if token.read_revision != read_revision:
        raise StaleContinuationError(
            token_read_revision=token.read_revision,
            current_read_revision=read_revision,
        )


def cursor_int(token: ContinuationToken, key: str) -> int:
    """Read a non-negative integer cursor component, 422 on anything else."""
    value = token.cursor.get(key)
    if not isinstance(value, int) or value < 0:
        raise InvalidContinuationError(f"token cursor is missing a valid '{key}' component")
    return value


def keyset_str(token: ContinuationToken, key: str) -> str:
    """Read a non-empty string keyset component, 422 on anything else."""
    value = (token.keyset or {}).get(key)
    if not isinstance(value, str) or not value:
        raise InvalidContinuationError(f"token keyset is missing a valid '{key}' component")
    return value


__all__ = [
    "TOKEN_VERSION",
    "ContinuationSurface",
    "ContinuationToken",
    "compute_filter_hash",
    "cursor_int",
    "decode_continuation_token",
    "keyset_str",
    "mint_continuation_token",
    "validate_continuation_token",
]
