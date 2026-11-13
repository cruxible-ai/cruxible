"""Test worlds read Captures through their CaptureKind's layout.

A Procedure stores only the thin ``extract`` node (a record set and header
fields of the kind's layout); the kind declares the record set's path, key, row
and field rules. A world that used to spell an extract out in full writes the
same declaration once on the kind (:func:`record_set`, :func:`with_layout`) and
names it from the node (:func:`thin_node`) or the authored step
(:func:`thin_step`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cruxible_client.contracts.capture_layouts import CaptureLayout
from cruxible_client.contracts.procedures.layout_extract import LayoutExtractNode


def _dumped(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, Mapping):
        return {key: _dumped(item) for key, item in value.items()}
    return value


def _row(row: Any) -> Any:
    """A row as a layout states it: ``name@N`` text stays text, a ref stays a ref."""

    return row if isinstance(row, str) else _dumped(row)


def record_set(
    *,
    at: str,
    row: Any,
    fields: Mapping[str, Any],
    key: Mapping[str, Any] | None = None,
    count_at: str | None = None,
) -> dict[str, Any]:
    """One layout record set: what a full extract spelled as records/row/fields/key/count."""

    declared: dict[str, Any] = {"at": at, "row": _row(row), "fields": _dumped(dict(fields))}
    if key:
        declared["key"] = _dumped(dict(key))
    if count_at is not None:
        declared["count_at"] = count_at
    return declared


def layout(
    records: Mapping[str, Mapping[str, Any]], header: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    declared: dict[str, Any] = {"records": {name: dict(item) for name, item in records.items()}}
    if header:
        declared["header"] = _dumped(dict(header))
    return declared


def with_layout(
    kind: Any,
    records: Mapping[str, Mapping[str, Any]],
    header: Mapping[str, Any] | None = None,
) -> Any:
    """``kind`` (a stored CaptureKind) declaring these record sets and header fields."""

    return kind.model_copy(update={"layout": CaptureLayout.model_validate(layout(records, header))})


def thin_node(
    *,
    node_id: str,
    capture: str,
    records: str,
    as_: str | None = None,
    header: tuple[str, ...] = (),
    key: Mapping[str, str] | None = None,
    duplicates: str = "refuse_key",
    only_keys: object = None,
    next: str | None = None,
) -> LayoutExtractNode:
    """The stored thin node naming record set ``records`` (and ``header`` fields)."""

    data: dict[str, Any] = {
        "kind": "extract",
        "node_id": node_id,
        "capture": capture,
        "records": records,
        "header": tuple(sorted(header)),
        "key": {part: {"type": spelled} for part, spelled in (key or {}).items()},
        "duplicates": duplicates,
        "as": as_ or node_id,
        "next": next,
    }
    if only_keys is not None:
        data["only_keys"] = only_keys
    return LayoutExtractNode.model_validate(data)


def thin_step(
    extract: str,
    *,
    capture: Any,
    records: str,
    header: tuple[str, ...] = (),
    key: Mapping[str, str] | None = None,
    **options: Any,
) -> dict[str, Any]:
    """The authored thin step (``extract: <id>``) naming record set ``records``."""

    step: dict[str, Any] = {"extract": extract, "capture": capture, "records": records}
    if header:
        step["header"] = sorted(header)
    if key:
        step["key"] = {part: {"type": spelled} for part, spelled in key.items()}
    step.update(options)
    return step


__all__ = ["grown_kind_members", "layout", "record_set", "thin_node", "thin_step", "with_layout"]


def _plain(value: Any) -> Any:
    """A declaration as its stored JSON reads back (for an equality check)."""

    from cruxible_client.contracts.capture_layouts import LayoutRecordSet
    from cruxible_client.contracts.procedures.extract import ExtractField

    model = LayoutRecordSet if isinstance(value, Mapping) and "fields" in value else ExtractField
    return model.model_validate(_dumped(value)).model_dump(mode="json")


def grown_kind_members(
    instance: Any,
    name: str,
    records: Mapping[str, Mapping[str, Any]],
    header: Mapping[str, Any] | None = None,
) -> dict[str, bytes]:
    """The accepted CaptureKind ``name``'s successor that adds ``records`` (and header
    fields) to its layout, as candidate-tree members (a layout may only grow)."""

    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.captures import (
        capture_contract_digest,
        capture_contract_path,
        parse_capture_contract,
        render_capture_contract,
    )

    path = capture_contract_path(name)
    accepted = parse_capture_contract(
        instance.tree_at(instance.accepted_coordinate().git_oid)[path], path=path
    )
    current = (
        {"records": {}} if accepted.layout is None else accepted.layout.model_dump(mode="json")
    )
    if all(current.get("records", {}).get(k) == _plain(v) for k, v in records.items()) and (
        not header or all(current.get("header", {}).get(k) == _plain(v) for k, v in header.items())
    ):
        return {}
    grown = {
        **current,
        "records": {**current.get("records", {}), **{k: dict(v) for k, v in records.items()}},
    }
    if header:
        grown["header"] = {**current.get("header", {}), **_dumped(dict(header))}
    successor = accepted.model_copy(
        update={
            "layout": CaptureLayout.model_validate(grown),
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=capture_contract_digest(accepted).tagged
            ),
        }
    )
    return {path: render_capture_contract(successor)}
