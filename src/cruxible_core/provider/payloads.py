"""Provider payload helpers for common Cruxible contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from cruxible_core.graph.evidence import (
    EvidenceRef,
    evidence_ref_payload,
    merge_evidence_refs,
)


class ParsedTabularBundle(BaseModel):
    """Validated helper for the ``cruxible.ParsedTabularBundle`` contract."""

    artifact: dict[str, Any] = Field(default_factory=dict)
    tables: dict[str, list[dict[str, Any]]]
    files: Any = Field(default_factory=dict)
    diagnostics: Any = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")
    _table_metadata: dict[str, dict[str, Any]] = PrivateAttr(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ParsedTabularBundle:
        """Normalize a parsed tabular provider payload."""
        raw_tables = payload.get("tables")
        if not isinstance(raw_tables, Mapping):
            raise ValueError("Expected input.tables to be a mapping")

        artifact = payload.get("artifact", {})
        if not isinstance(artifact, Mapping):
            raise ValueError("Expected input.artifact to be a mapping")

        tables: dict[str, list[dict[str, Any]]] = {}
        table_metadata: dict[str, dict[str, Any]] = {}
        for table_name, table_payload in raw_tables.items():
            rows, metadata = _normalize_table_payload(str(table_name), table_payload)
            table_name = str(table_name)
            tables[table_name] = rows
            table_metadata[table_name] = metadata

        bundle = cls(
            artifact=dict(artifact),
            tables=tables,
            files=payload.get("files", {}),
            diagnostics=payload.get("diagnostics", {}),
        )
        bundle._table_metadata = table_metadata
        return bundle

    def require_table(self, name: str) -> list[dict[str, Any]]:
        """Return parsed rows for ``name`` or raise a clear provider error."""
        if name not in self.tables:
            raise ValueError(f"Expected parsed table '{name}'")
        return [dict(row) for row in self.tables[name]]

    def optional_table(self, name: str) -> list[dict[str, Any]]:
        """Return parsed rows for ``name`` or an empty list when absent."""
        if name not in self.tables:
            return []
        return [dict(row) for row in self.tables[name]]

    def table_names(self) -> list[str]:
        """Return table names in deterministic order."""
        return sorted(self.tables)

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-compatible provider payload."""
        return {
            "artifact": dict(self.artifact),
            "tables": {
                table_name: _table_payload(table_name, rows, self._table_metadata)
                for table_name, rows in self.tables.items()
            },
            "files": self.files,
            "diagnostics": self.diagnostics,
        }


class JsonItems(BaseModel):
    """Validated helper for the ``cruxible.JsonItems`` contract."""

    items: list[dict[str, Any]]

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], key: str = "items") -> JsonItems:
        """Normalize a list-of-objects provider payload."""
        value = payload.get(key)
        if not isinstance(value, list):
            raise ValueError(f"Expected '{key}' to be a list of objects")
        return cls(items=_coerce_rows(value, f"'{key}'"))

    def to_payload(self, key: str = "items") -> dict[str, Any]:
        """Return the provider payload shape, preserving row order."""
        return {key: [dict(item) for item in self.items]}


def evidence_ref(source: str, source_record_id: str, **extra: Any) -> dict[str, Any]:
    """Build a generic evidence reference payload."""
    return evidence_ref_payload({"source": source, "source_record_id": source_record_id, **extra})


def _normalize_table_payload(
    table_name: str,
    table_payload: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if isinstance(table_payload, list):
        return _coerce_rows(table_payload, f"parsed table '{table_name}'"), {}
    if not isinstance(table_payload, Mapping):
        raise ValueError(f"Expected parsed table '{table_name}' to be a table object")
    table = dict(table_payload)
    rows = table.get("rows")
    if not isinstance(rows, list):
        raise ValueError(f"Expected parsed table '{table_name}' to contain rows")
    metadata = {key: value for key, value in table.items() if key != "rows"}
    return _coerce_rows(rows, f"parsed table '{table_name}' rows"), metadata


def _table_payload(
    table_name: str,
    rows: list[dict[str, Any]],
    metadata_by_table: Mapping[str, Mapping[str, Any]],
) -> Any:
    metadata = dict(metadata_by_table.get(table_name, {}))
    if not metadata:
        return [dict(row) for row in rows]
    return {**metadata, "rows": [dict(row) for row in rows]}


def _coerce_rows(rows: list[Any], label: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"Expected {label} entry {index} to be an object")
        result.append(dict(row))
    return result


__all__ = [
    "EvidenceRef",
    "JsonItems",
    "ParsedTabularBundle",
    "evidence_ref",
    "merge_evidence_refs",
]
