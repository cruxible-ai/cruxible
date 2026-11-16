"""A claims terminal's journal records, with their per-item parts in run-free bodies.

A claims terminal journals a ``prepared`` record before its sink and a
resolving record after. Inline, both restated every item: the child receipts
in each record, and in the prepared one the request's items, changes and
extraction receipts, the preparation's per-item paths and unchanged children,
and an item-to-Capture map; the delivered record repeated the receipt's
children. None of those parts names the run, so each is stored as one
content-addressed body cited by digest, and two records citing the same list
share one object. The record keeps what is specific to this run inline (its
verdict, rung, operation key, the request's run header).

:func:`split_terminal_egress_payload` is the writer's step;
:func:`join_terminal_egress_payload` restores the exact inline payload, so a
reader sees one shape whichever was journaled (records written before the
split are inline and pass through unchanged).

The item-to-Capture map (``evidence``) is mostly a restatement: an item whose
cited Capture is its extraction receipt's ``value.capture_digest`` needs no
entry. Only the items whose Capture is not their receipt's (a Subject item,
which carries no receipt) are journaled, as ``evidence_overrides``; the join
restores the whole map.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, cast

from cruxible_client.contracts.canonical import CanonicalValue, canonical_bytes
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.errors import CasError
from cruxible_core.exhaust.payloads import UnavailablePayload

TERMINAL_CHILDREN_V1 = "playbill-terminal-children-v1"
TERMINAL_REQUEST_ITEMS_V1 = "playbill-terminal-request-items-v1"
TERMINAL_PREPARED_ITEMS_V1 = "playbill-terminal-prepared-items-v1"
TERMINAL_RECEIPT_CHILDREN_V1 = "playbill-terminal-receipt-children-v1"

#: The request fields that grow with the terminal's items.
_REQUEST_ITEM_FIELDS = ("items", "claims_changes", "claims_evidence")
#: The preparation fields that grow with the terminal's items.
_PREPARED_ITEM_FIELDS = ("item_paths", "unchanged")


class TerminalPayloadError(ValueError):
    """A split terminal record cites a body that does not restore it."""


def splits_terminal_egress(payload: object) -> bool:
    """Whether a ``terminal_egress`` payload is journaled split: a claims terminal's."""

    return isinstance(payload, Mapping) and payload.get("kind") == "claims"


def _cited_capture(receipt: object) -> object:
    value = receipt.get("value") if isinstance(receipt, Mapping) else None
    return value.get("capture_digest") if isinstance(value, Mapping) else None


def split_terminal_egress_payload(
    payload: Mapping[str, Any],
    *,
    store: Callable[[bytes], str],
) -> dict[str, Any]:
    """Move a claims terminal record's per-item parts into bodies; return the record.

    ``store`` lands canonical bytes and returns their digest. A payload that is
    not a claims terminal's, or is already split, is returned as it is.
    """

    if not splits_terminal_egress(payload) or "children_digest" in payload:
        return dict(payload)
    split = dict(payload)
    children = split.pop("children", None)
    if isinstance(children, list):
        split["children_digest"] = store(
            canonical_bytes({"tag": TERMINAL_CHILDREN_V1, "children": children})
        )
    request = split.get("request")
    if isinstance(request, Mapping) and all(field in request for field in _REQUEST_ITEM_FIELDS):
        moved = _REQUEST_ITEM_FIELDS
        header = {key: value for key, value in request.items() if key not in moved}
        header["items_digest"] = store(
            canonical_bytes(
                {"tag": TERMINAL_REQUEST_ITEMS_V1, **{field: request[field] for field in moved}}
            )
        )
        split["request"] = header
    prepared = split.get("prepared")
    if isinstance(prepared, Mapping) and "item_paths" in prepared:
        header = {key: value for key, value in prepared.items() if key not in _PREPARED_ITEM_FIELDS}
        header["items_digest"] = store(
            canonical_bytes(
                {
                    "tag": TERMINAL_PREPARED_ITEMS_V1,
                    **{
                        field: prepared[field]
                        for field in _PREPARED_ITEM_FIELDS
                        if field in prepared
                    },
                }
            )
        )
        split["prepared"] = header
    evidence = split.pop("evidence", None)
    if isinstance(evidence, Mapping):
        receipts = request.get("claims_evidence") if isinstance(request, Mapping) else None
        receipts = receipts if isinstance(receipts, Mapping) else {}
        split["evidence_overrides"] = {
            key: value
            for key, value in evidence.items()
            if _cited_capture(receipts.get(key)) != value
        }
        # The join restores an entry for exactly the items the map held.
        split["evidence_items"] = sorted(evidence, key=lambda key: str(key).encode("utf-8"))
    receipt = split.get("receipt")
    if isinstance(receipt, Mapping) and isinstance(receipt.get("children"), list):
        header = {key: value for key, value in receipt.items() if key != "children"}
        header["children_digest"] = store(
            canonical_bytes({"tag": TERMINAL_RECEIPT_CHILDREN_V1, "children": receipt["children"]})
        )
        split["receipt"] = header
    return split


def _body(read_body: Callable[[str], bytes], digest: object, *, tag: str) -> dict[str, Any]:
    if not isinstance(digest, str):
        raise TerminalPayloadError(f"a split terminal record names no {tag} body")
    content = read_body(digest)
    value = json.loads(content)
    if not isinstance(value, dict) or value.get("tag") != tag or canonical_bytes(value) != content:
        raise TerminalPayloadError(f"a split terminal record's {tag} body is not canonical")
    value.pop("tag")
    return value


def join_terminal_egress_payload(
    payload: object,
    read_body: Callable[[str], bytes],
) -> object:
    """The exact inline payload of a ``terminal_egress`` record, either shape.

    ``read_body`` returns the verified CAS bytes of a digest. An inline record
    (any terminal kind, or one written before the split) is returned as is.
    """

    if not isinstance(payload, Mapping) or not _is_split(payload):
        return payload
    joined = dict(payload)
    if "children_digest" in joined:
        joined["children"] = _body(
            read_body, joined.pop("children_digest"), tag=TERMINAL_CHILDREN_V1
        )["children"]
    request = joined.get("request")
    if isinstance(request, Mapping) and "items_digest" in request:
        restored = {key: value for key, value in request.items() if key != "items_digest"}
        restored.update(_body(read_body, request["items_digest"], tag=TERMINAL_REQUEST_ITEMS_V1))
        joined["request"] = restored
    prepared = joined.get("prepared")
    if isinstance(prepared, Mapping) and "items_digest" in prepared:
        restored = {key: value for key, value in prepared.items() if key != "items_digest"}
        restored.update(_body(read_body, prepared["items_digest"], tag=TERMINAL_PREPARED_ITEMS_V1))
        joined["prepared"] = restored
    if "evidence_overrides" in joined:
        overrides = joined.pop("evidence_overrides")
        listed = joined.pop("evidence_items", None)
        request = joined.get("request")
        receipts = request.get("claims_evidence") if isinstance(request, Mapping) else None
        if not isinstance(overrides, Mapping) or not isinstance(listed, list):
            raise TerminalPayloadError("a split terminal record's evidence is malformed")
        receipts = receipts if isinstance(receipts, Mapping) else {}
        evidence: dict[str, Any] = {}
        for key in listed:
            value = overrides[key] if key in overrides else _cited_capture(receipts.get(key))
            if not isinstance(value, str):
                raise TerminalPayloadError(
                    "a split terminal record's evidence names no Capture for an item"
                )
            evidence[key] = value
        joined["evidence"] = evidence
    receipt = joined.get("receipt")
    if isinstance(receipt, Mapping) and "children_digest" in receipt:
        restored = {key: value for key, value in receipt.items() if key != "children_digest"}
        restored["children"] = _body(
            read_body, receipt["children_digest"], tag=TERMINAL_RECEIPT_CHILDREN_V1
        )["children"]
        joined["receipt"] = restored
    return joined


def join_terminal_egress_read(
    payload: CanonicalValue | UnavailablePayload,
    bodies: Any,
    *,
    access: BodyAccessContext,
) -> CanonicalValue | UnavailablePayload:
    """Join a ``terminal_egress`` record's read payload; a lost body is unavailable.

    The bodies a split record cites are exhaust, like the record's own payload:
    one lost to a crash or damage makes the record's payload unavailable, the
    same typed answer ``read_payload`` gives for a lost payload.
    """

    return _join_read(join_terminal_egress_payload, payload, bodies, access=access)


def _join_read(
    join: Callable[[object, Callable[[str], bytes]], object],
    payload: CanonicalValue | UnavailablePayload,
    bodies: Any,
    *,
    access: BodyAccessContext,
) -> CanonicalValue | UnavailablePayload:
    if isinstance(payload, UnavailablePayload):
        return payload
    missing: list[str] = []

    def read(digest: str) -> bytes:
        try:
            return bytes(bodies.read(digest, access=access))
        except (CasError, OSError):
            missing.append(digest)
            raise

    try:
        return cast(CanonicalValue, join(payload, read))
    except (CasError, OSError):
        return UnavailablePayload(missing[-1])


#: A derived Source request's value, run-free, cited by its journal record.
DERIVED_SOURCE_REQUEST_BODY_V1 = "playbill-derived-source-request-body-v1"


def split_derived_source_request(
    payload: Mapping[str, Any], *, store: Callable[[bytes], str]
) -> dict[str, Any]:
    """Move a ``source_request_derived`` record's request value into a run-free body.

    The request (a fetch's coordinates, a component list) grows with what the
    call asks for and is a function of the run's inputs; the record keeps its
    run binding and ``request_digest`` inline.
    """

    if "request" not in payload:
        return dict(payload)
    split = {key: value for key, value in payload.items() if key != "request"}
    split["request_body_digest"] = store(
        canonical_bytes({"tag": DERIVED_SOURCE_REQUEST_BODY_V1, "request": payload["request"]})
    )
    return split


def join_derived_source_request(payload: object, read_body: Callable[[str], bytes]) -> object:
    """The exact inline ``source_request_derived`` payload, either shape."""

    if not isinstance(payload, Mapping) or "request_body_digest" not in payload:
        return payload
    joined = {key: value for key, value in payload.items() if key != "request_body_digest"}
    joined["request"] = _body(
        read_body, payload["request_body_digest"], tag=DERIVED_SOURCE_REQUEST_BODY_V1
    )["request"]
    return joined


def join_derived_source_request_read(
    payload: CanonicalValue | UnavailablePayload,
    bodies: Any,
    *,
    access: BodyAccessContext,
) -> CanonicalValue | UnavailablePayload:
    """Join a ``source_request_derived`` read; a lost request body is unavailable."""

    return _join_read(join_derived_source_request, payload, bodies, access=access)


def _is_split(payload: Mapping[str, Any]) -> bool:
    request, prepared, receipt = (payload.get(key) for key in ("request", "prepared", "receipt"))
    return (
        "children_digest" in payload
        or "evidence_overrides" in payload
        or (isinstance(request, Mapping) and "items_digest" in request)
        or (isinstance(prepared, Mapping) and "items_digest" in prepared)
        or (isinstance(receipt, Mapping) and "children_digest" in receipt)
    )


#: One invocation's associated (field, Capture) pairs: run-free, cited by digest.
ASSOCIATION_MEMBERS_BODY_V1 = "playbill-source-capture-association-members-v1"


def split_source_capture_associations(
    associations: list[dict[str, Any]], *, store: Callable[[bytes], str]
) -> list[dict[str, Any]]:
    """A finalize record's associations, grouped per invocation with their pairs in a body.

    The occurrence path and invocation receipt are this run's binding and stay
    inline, one entry per invocation; the (field, Capture) pairs are a run-free
    body an unchanged run reproduces, so the record does not grow with the
    Captures a call fetched.
    """

    groups: dict[tuple[str, str], list[list[str]]] = {}
    for item in associations:
        key = (str(item["occurrence_path"]), str(item["invocation_receipt_digest"]))
        groups.setdefault(key, []).append([str(item["field"]), str(item["capture_digest"])])
    return [
        {
            "occurrence_path": path,
            "invocation_receipt_digest": receipt,
            "count": len(members),
            "members_digest": store(
                canonical_bytes({"tag": ASSOCIATION_MEMBERS_BODY_V1, "members": members})
            ),
        }
        for (path, receipt), members in sorted(
            groups.items(), key=lambda item: (item[0][0].encode("utf-8"), item[0][1])
        )
    ]


def join_source_capture_associations(
    final: object, read_body: Callable[[str], bytes]
) -> list[dict[str, Any]]:
    """A finalize record's associations as flat entries, either shape, in path/field order."""

    if not isinstance(final, Mapping):
        return []
    groups = final.get("source_capture_association_groups")
    if not isinstance(groups, list):
        flat = final.get("source_capture_associations", [])
        return list(flat) if isinstance(flat, list) else []
    joined: list[dict[str, Any]] = []
    for group in groups:
        body = _body(read_body, group["members_digest"], tag=ASSOCIATION_MEMBERS_BODY_V1)
        if len(body["members"]) != group["count"]:
            raise TerminalPayloadError("an association group's count differs from its body")
        joined.extend(
            {
                "tag": "playbill-procedure-source-capture-association-v1",
                "occurrence_path": group["occurrence_path"],
                "field": field,
                "invocation_receipt_digest": group["invocation_receipt_digest"],
                "capture_digest": capture,
            }
            for field, capture in body["members"]
        )
    return sorted(
        joined,
        key=lambda item: (item["occurrence_path"].encode("utf-8"), item["field"].encode("utf-8")),
    )


__all__ = [
    "ASSOCIATION_MEMBERS_BODY_V1",
    "join_source_capture_associations",
    "split_source_capture_associations",
    "DERIVED_SOURCE_REQUEST_BODY_V1",
    "join_derived_source_request",
    "join_derived_source_request_read",
    "split_derived_source_request",
    "TERMINAL_CHILDREN_V1",
    "TERMINAL_PREPARED_ITEMS_V1",
    "TERMINAL_RECEIPT_CHILDREN_V1",
    "TERMINAL_REQUEST_ITEMS_V1",
    "TerminalPayloadError",
    "join_terminal_egress_payload",
    "join_terminal_egress_read",
    "split_terminal_egress_payload",
    "splits_terminal_egress",
]
