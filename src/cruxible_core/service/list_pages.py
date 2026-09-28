"""Bounded list pages with opaque continuation cursors.

The proposal, policy and curation lists answer one page at a time, like
``next``: a request carries ``limit`` and an optional ``cursor``, and a cut
answer carries ``truncated`` and the ``next_cursor`` that continues it. A cursor
pins the list it belongs to, the accepted coordinate its first page was read
at, the selection that page answered, a snapshot digest of the whole listing
that page was cut from, and the last row it carried. A cursor minted for
anything else is refused rather than silently restarting the list, and a cursor
whose listing has since changed (a proposal withdrawn or admitted, a curation
item appended or resolved) is refused as stale: a caller gets either the
original listing or a clear restart, never a silently shifted walk.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.primitives import canonical_json

T = TypeVar("T")


class PlaybillListCursorMismatch(PlaybillFormatError):
    """A list cursor that does not continue the list this request reads."""

    error_code = "playbill.list.cursor_mismatch"


class PlaybillListCursorStale(PlaybillListCursorMismatch):
    """A list cursor whose listing changed after its first page."""

    error_code = "playbill.list.cursor_stale"


@dataclass(frozen=True)
class ListContinuation:
    """What a decoded cursor pins."""

    coordinate: dict[str, Any]
    snapshot: str
    last_key: tuple[str, ...]


def _mismatch(list_name: str, detail: str) -> PlaybillListCursorMismatch:
    return PlaybillListCursorMismatch(
        f"{PlaybillListCursorMismatch.error_code}: {detail}; "
        f"list the {list_name} again without a cursor"
    )


def _digest(body: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(dict(body)).encode("utf-8")).hexdigest()


def list_snapshot(rows: Sequence[Any]) -> str:
    """Digest of a whole listing; any change to its rows changes it."""
    return _digest({"rows": list(rows)})


def encode_list_cursor(
    *,
    list_name: str,
    coordinate: Mapping[str, Any],
    selection: Mapping[str, Any],
    snapshot: str,
    last_key: Sequence[str],
) -> str:
    body = {
        "list": list_name,
        "coordinate": dict(coordinate),
        "selection": dict(selection),
        "snapshot": snapshot,
        "last_key": list(last_key),
    }
    return base64.urlsafe_b64encode(
        canonical_json({**body, "digest": _digest(body)}).encode("utf-8")
    ).decode("ascii")


def decode_list_cursor(
    cursor: str,
    *,
    list_name: str,
    selection: Mapping[str, Any],
) -> ListContinuation:
    """Decode a cursor, refusing one minted for another list or selection."""

    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except (UnicodeError, ValueError, binascii.Error) as exc:
        raise _mismatch(list_name, "the cursor is not a list cursor") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "list",
        "coordinate",
        "selection",
        "snapshot",
        "last_key",
        "digest",
    }:
        raise _mismatch(list_name, "the cursor is not a list cursor")
    digest = payload.pop("digest")
    last_key = payload["last_key"]
    if (
        digest != _digest(payload)
        or not isinstance(payload["coordinate"], dict)
        or not isinstance(payload["snapshot"], str)
        or not isinstance(last_key, list)
        or not all(isinstance(part, str) for part in last_key)
    ):
        raise _mismatch(list_name, "the cursor is malformed")
    if payload["list"] != list_name:
        raise _mismatch(list_name, f"the cursor continues the {payload['list']} list")
    if payload["selection"] != json.loads(canonical_json(dict(selection))):
        raise _mismatch(list_name, "the cursor was minted for a different selection")
    return ListContinuation(
        coordinate=payload["coordinate"],
        snapshot=payload["snapshot"],
        last_key=tuple(last_key),
    )


def page_after_boundary(
    rows: Sequence[T],
    *,
    keys: Sequence[tuple[str, ...]],
    snapshot: str,
    continuation: ListContinuation | None,
    limit: int,
    list_name: str,
) -> tuple[tuple[T, ...], bool]:
    """One page of ``rows`` after the cursor's last row.

    ``snapshot`` digests the listing as it stands now; a continuation minted
    over a different listing is refused as stale before any row is served.
    """

    start = 0
    if continuation is not None:
        if continuation.snapshot != snapshot:
            raise PlaybillListCursorStale(
                f"{PlaybillListCursorStale.error_code}: the {list_name} listing changed since "
                f"the cursor's first page; list the {list_name} again without a cursor"
            )
        try:
            start = list(keys).index(continuation.last_key) + 1
        except ValueError as exc:
            raise _mismatch(list_name, "the cursor's last row is absent from the list") from exc
    page = tuple(rows[start : start + limit])
    return page, start + len(page) < len(rows)


__all__ = [
    "ListContinuation",
    "PlaybillListCursorMismatch",
    "PlaybillListCursorStale",
    "decode_list_cursor",
    "encode_list_cursor",
    "list_snapshot",
    "page_after_boundary",
]
