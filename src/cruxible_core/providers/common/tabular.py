"""Common providers for source-grounded tabular artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from cruxible_core.canonical_json import canonical_json
from cruxible_core.provider.types import ProviderContext

_SUPPORTED_EXTENSIONS = {".csv", ".json", ".jsonl", ".ndjson", ".xlsx", ".xls"}
_METADATA_FIELDS = {
    "_row_id",
    "_row_hash",
    "_source_file",
    "_source_format",
    "_source_row",
    "_source_sheet",
}


@dataclass(frozen=True)
class _ParsedTable:
    name: str
    source_file: str
    source_format: str
    source_sha256: str
    columns: list[str]
    rows: list[dict[str, Any]]
    source_sheet: str | None = None


def load_tabular_artifact_bundle(
    input_payload: dict[str, Any],
    context: ProviderContext,
) -> dict[str, Any]:
    """Parse a file or directory artifact into generic, provenance-rich tables.

    This provider deliberately does not map rows into domain entities. It only
    handles source parsing, mechanical header normalization, row hashes, and
    per-row provenance so kit-specific providers can consume clean records.
    """
    root = _require_artifact_path(context, "load_tabular_artifact_bundle")
    options = _TabularOptions.from_payload(input_payload)
    files = _discover_files(root, options.extensions)
    tables: dict[str, dict[str, Any]] = {}
    diagnostics: list[dict[str, Any]] = []

    for file_path in files:
        for parsed in _parse_file(file_path, root=root, options=options):
            table = tables.setdefault(
                parsed.name,
                {
                    "columns": [],
                    "rows": [],
                    "sources": [],
                    "row_count": 0,
                },
            )
            table["columns"] = _merge_columns(table["columns"], parsed.columns)
            table["rows"].extend(parsed.rows)
            table["row_count"] = len(table["rows"])
            table["sources"].append(
                {
                    "file": parsed.source_file,
                    "sheet": parsed.source_sheet,
                    "format": parsed.source_format,
                    "sha256": parsed.source_sha256,
                    "row_count": len(parsed.rows),
                }
            )

    for expected in options.expected_tables:
        if expected not in tables:
            diagnostics.append(
                {
                    "level": "warning",
                    "code": "missing_expected_table",
                    "table": expected,
                    "message": f"Expected table '{expected}' was not found",
                }
            )

    return {
        "artifact": _artifact_summary(context),
        "tables": tables,
        "files": [
            {
                "path": _relative_file_name(file_path, root),
                "format": file_path.suffix.lower().lstrip("."),
                "sha256": _sha256_file(file_path),
            }
            for file_path in files
        ],
        "diagnostics": diagnostics,
    }


def source_diff(
    input_payload: dict[str, Any],
    _context: ProviderContext,
) -> dict[str, Any]:
    """Diff two parsed table payloads and return added/changed/removed rows."""
    previous_tables = _extract_tables(input_payload, "previous")
    current_tables = _extract_tables(input_payload, "current")
    key_fields = _coerce_key_fields(input_payload.get("key_fields"))
    include_unchanged = bool(input_payload.get("include_unchanged", False))

    table_names = sorted(set(previous_tables) | set(current_tables))
    diff_tables: dict[str, dict[str, Any]] = {}
    summary = {
        "tables": len(table_names),
        "added": 0,
        "changed": 0,
        "removed": 0,
        "unchanged": 0,
    }

    for table_name in table_names:
        keys = _keys_for_table(table_name, key_fields)
        previous_rows = _table_rows(previous_tables.get(table_name))
        current_rows = _table_rows(current_tables.get(table_name))
        previous_index = _index_rows(table_name, previous_rows, keys)
        current_index = _index_rows(table_name, current_rows, keys)

        added: list[dict[str, Any]] = []
        changed: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []
        unchanged: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []

        for key in sorted(current_index):
            current = current_index[key]
            previous = previous_index.get(key)
            if previous is None:
                added.append(current)
                continue
            if _content_hash(previous) == _content_hash(current):
                if include_unchanged:
                    unchanged.append(current)
                continue
            changed.append(
                {
                    "key": _key_payload(keys, current),
                    "before": previous,
                    "after": current,
                    "changed_fields": _changed_fields(previous, current),
                }
            )

        for key in sorted(set(previous_index) - set(current_index)):
            removed.append(previous_index[key])

        if len(previous_index) != len(previous_rows):
            diagnostics.append(_duplicate_diagnostic(table_name, "previous", previous_rows, keys))
        if len(current_index) != len(current_rows):
            diagnostics.append(_duplicate_diagnostic(table_name, "current", current_rows, keys))

        diff_tables[table_name] = {
            "key_fields": keys,
            "added": added,
            "changed": changed,
            "removed": removed,
            "unchanged": unchanged if include_unchanged else [],
            "counts": {
                "added": len(added),
                "changed": len(changed),
                "removed": len(removed),
                "unchanged": len(unchanged)
                if include_unchanged
                else len(set(previous_index) & set(current_index)) - len(changed),
            },
            "diagnostics": diagnostics,
        }
        summary["added"] += len(added)
        summary["changed"] += len(changed)
        summary["removed"] += len(removed)
        summary["unchanged"] += diff_tables[table_name]["counts"]["unchanged"]

    return {"tables": diff_tables, "summary": summary}


@dataclass(frozen=True)
class _TabularOptions:
    expected_tables: tuple[str, ...]
    table_names: dict[str, str]
    extensions: frozenset[str]
    normalize_headers: bool

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> _TabularOptions:
        expected_tables = tuple(
            _normalize_table_name(item)
            for item in _string_list(payload.get("expected_tables", []), "expected_tables")
        )
        table_names = {
            str(key): _normalize_table_name(str(value))
            for key, value in _string_map(payload.get("table_names", {}), "table_names").items()
        }
        extensions = frozenset(
            _normalize_extension(item)
            for item in _string_list(
                payload.get("extensions", sorted(_SUPPORTED_EXTENSIONS)),
                "extensions",
            )
        )
        unsupported = sorted(extensions - _SUPPORTED_EXTENSIONS)
        if unsupported:
            raise ValueError(f"Unsupported tabular extension(s): {', '.join(unsupported)}")
        normalize_headers = bool(payload.get("normalize_headers", True))
        return cls(
            expected_tables=expected_tables,
            table_names=table_names,
            extensions=extensions,
            normalize_headers=normalize_headers,
        )


def _require_artifact_path(context: ProviderContext, provider_name: str) -> Path:
    if context.artifact is None or context.artifact.local_path is None:
        raise ValueError(f"{provider_name} requires a local file or directory artifact")
    path = Path(context.artifact.local_path)
    if not path.exists():
        raise ValueError(f"{provider_name} artifact path does not exist: {path}")
    return path


def _discover_files(root: Path, extensions: frozenset[str]) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() in extensions else []
    return sorted(
        path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in extensions
    )


def _parse_file(file_path: Path, *, root: Path, options: _TabularOptions) -> list[_ParsedTable]:
    extension = file_path.suffix.lower()
    relative_name = _relative_file_name(file_path, root)
    source_sha256 = _sha256_file(file_path)

    if extension == ".csv":
        frame = pl.read_csv(file_path, infer_schema=False)
        return [
            _table_from_frame(
                frame,
                table_name=_table_name_for(file_path, root, options),
                source_file=relative_name,
                source_format="csv",
                source_sha256=source_sha256,
                source_row_start=2,
                source_sheet=None,
                normalize_headers=options.normalize_headers,
            )
        ]
    if extension in {".jsonl", ".ndjson"}:
        rows = _read_json_lines(file_path)
        frame = pl.DataFrame(rows) if rows else pl.DataFrame()
        return [
            _table_from_frame(
                frame,
                table_name=_table_name_for(file_path, root, options),
                source_file=relative_name,
                source_format=extension.lstrip("."),
                source_sha256=source_sha256,
                source_row_start=1,
                source_sheet=None,
                normalize_headers=options.normalize_headers,
            )
        ]
    if extension == ".json":
        rows = _read_json_rows(file_path)
        frame = pl.DataFrame(rows) if rows else pl.DataFrame()
        return [
            _table_from_frame(
                frame,
                table_name=_table_name_for(file_path, root, options),
                source_file=relative_name,
                source_format="json",
                source_sha256=source_sha256,
                source_row_start=1,
                source_sheet=None,
                normalize_headers=options.normalize_headers,
            )
        ]
    if extension in {".xlsx", ".xls"}:
        return _parse_excel_file(
            file_path,
            root=root,
            options=options,
            source_sha256=source_sha256,
        )
    raise ValueError(f"Unsupported tabular file extension: {extension}")


def _parse_excel_file(
    file_path: Path,
    *,
    root: Path,
    options: _TabularOptions,
    source_sha256: str,
) -> list[_ParsedTable]:
    try:
        raw = pl.read_excel(file_path, sheet_id=0)
    except Exception as exc:  # pragma: no cover - depends on optional engine availability
        raise ValueError(
            "Reading Excel artifacts requires Polars Excel support in the server runtime"
        ) from exc

    relative_name = _relative_file_name(file_path, root)
    if isinstance(raw, pl.DataFrame):
        raw_tables = {"Sheet1": raw}
    elif isinstance(raw, dict):
        raw_tables = {str(name): frame for name, frame in raw.items()}
    else:  # pragma: no cover - defensive against upstream API changes
        raise ValueError("Polars returned an unsupported Excel payload")

    parsed: list[_ParsedTable] = []
    for sheet_name, frame in sorted(raw_tables.items()):
        parsed.append(
            _table_from_frame(
                frame,
                table_name=_table_name_for(file_path, root, options, sheet_name=sheet_name),
                source_file=relative_name,
                source_format=file_path.suffix.lower().lstrip("."),
                source_sha256=source_sha256,
                source_row_start=2,
                source_sheet=sheet_name,
                normalize_headers=options.normalize_headers,
            )
        )
    return parsed


def _table_from_frame(
    frame: pl.DataFrame,
    *,
    table_name: str,
    source_file: str,
    source_format: str,
    source_sha256: str,
    source_row_start: int,
    source_sheet: str | None,
    normalize_headers: bool,
) -> _ParsedTable:
    columns = [
        _normalize_column_name(column) if normalize_headers else column
        for column in frame.columns
    ]
    unique_columns = _deduplicate_columns(columns)
    if unique_columns != frame.columns:
        frame = frame.rename(dict(zip(frame.columns, unique_columns, strict=True)))

    row_dicts = frame.to_dicts()
    rows: list[dict[str, Any]] = []
    for index, raw_row in enumerate(row_dicts, start=source_row_start):
        row = {key: _json_safe_value(value) for key, value in raw_row.items()}
        row_hash = _row_hash(row)
        row_id_parts = [source_file]
        if source_sheet:
            row_id_parts.append(source_sheet)
        row_id_parts.append(str(index))
        rows.append(
            {
                "_row_id": ":".join(row_id_parts),
                "_row_hash": row_hash,
                "_source_file": source_file,
                "_source_format": source_format,
                "_source_row": index,
                **({"_source_sheet": source_sheet} if source_sheet else {}),
                **row,
            }
        )

    return _ParsedTable(
        name=table_name,
        source_file=source_file,
        source_format=source_format,
        source_sha256=source_sha256,
        source_sheet=source_sheet,
        columns=unique_columns,
        rows=rows,
    )


def _read_json_lines(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        value = json.loads(stripped)
        if not isinstance(value, dict):
            raise ValueError(f"{path} line {line_number} is not a JSON object")
        rows.append(value)
    return rows


def _read_json_rows(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text())
    if isinstance(value, list):
        rows = value
    elif isinstance(value, dict):
        for key in ("rows", "items", "data"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                rows = candidate
                break
        else:
            rows = [value]
    else:
        raise ValueError(f"{path} must contain a JSON object or array")

    if not all(isinstance(item, dict) for item in rows):
        raise ValueError(f"{path} contains non-object rows")
    return list(rows)


def _table_name_for(
    file_path: Path,
    root: Path,
    options: _TabularOptions,
    *,
    sheet_name: str | None = None,
) -> str:
    relative = _relative_file_name(file_path, root)
    candidates = [
        relative if sheet_name is None else f"{relative}#{sheet_name}",
        file_path.name if sheet_name is None else f"{file_path.name}#{sheet_name}",
        file_path.stem if sheet_name is None else f"{file_path.stem}#{sheet_name}",
        file_path.stem,
    ]
    for candidate in candidates:
        if candidate in options.table_names:
            return options.table_names[candidate]

    base = _normalize_table_name(file_path.stem)
    if sheet_name is None:
        return base
    return f"{base}__{_normalize_table_name(sheet_name)}"


def _normalize_column_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower())
    normalized = normalized.strip("_")
    return normalized or "column"


def _normalize_table_name(value: str) -> str:
    return _normalize_column_name(value)


def _normalize_extension(value: str) -> str:
    extension = value.lower().strip()
    if not extension.startswith("."):
        extension = f".{extension}"
    return extension


def _deduplicate_columns(columns: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result: list[str] = []
    for column in columns:
        count = seen.get(column, 0)
        seen[column] = count + 1
        result.append(column if count == 0 else f"{column}_{count + 1}")
    return result


def _merge_columns(current: list[str], new: list[str]) -> list[str]:
    result = list(current)
    for column in new:
        if column not in result:
            result.append(column)
    return result


def _relative_file_name(path: Path, root: Path) -> str:
    if root.is_file():
        return path.name
    return path.relative_to(root).as_posix()


def _sha256_file(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _row_hash(row: dict[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(row).encode()).hexdigest()}"


def _canonical_json(value: Any) -> str:
    return canonical_json(value)


def _json_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _artifact_summary(context: ProviderContext) -> dict[str, Any] | None:
    if context.artifact is None:
        return None
    return {
        "name": context.artifact.name,
        "kind": context.artifact.kind,
        "uri": context.artifact.uri,
        "sha256": context.artifact.sha256,
        "metadata": context.artifact.metadata,
    }


def _string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must be a list of strings")
    return value


def _string_map(value: Any, field_name: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError(f"{field_name} must be a string map")
    return value


def _extract_tables(payload: dict[str, Any], name: str) -> dict[str, Any]:
    direct_key = f"{name}_tables"
    direct = payload.get(direct_key)
    if isinstance(direct, dict):
        return direct

    nested = payload.get(name)
    if isinstance(nested, dict):
        tables = nested.get("tables")
        if isinstance(tables, dict):
            return tables

    raise ValueError(f"source_diff requires '{direct_key}' or '{name}.tables'")


def _coerce_key_fields(value: Any) -> dict[str, list[str]]:
    if value is None:
        return {}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return {"*": value}
    if isinstance(value, dict):
        result: dict[str, list[str]] = {}
        for table_name, fields in value.items():
            if not isinstance(table_name, str):
                raise ValueError("key_fields table names must be strings")
            if not isinstance(fields, list) or not all(isinstance(item, str) for item in fields):
                raise ValueError("key_fields values must be lists of strings")
            result[_normalize_table_name(table_name)] = fields
        return result
    raise ValueError("key_fields must be a list of strings or a table-to-fields map")


def _keys_for_table(table_name: str, key_fields: dict[str, list[str]]) -> list[str]:
    normalized = _normalize_table_name(table_name)
    if normalized in key_fields:
        return key_fields[normalized]
    if table_name in key_fields:
        return key_fields[table_name]
    if "*" in key_fields:
        return key_fields["*"]
    return ["_source_file", "_source_sheet", "_source_row"]


def _table_rows(table: Any) -> list[dict[str, Any]]:
    if table is None:
        return []
    if isinstance(table, dict):
        rows = table.get("rows", [])
    else:
        rows = table
    if not isinstance(rows, list) or not all(isinstance(item, dict) for item in rows):
        raise ValueError("table rows must be a list of objects")
    return rows


def _index_rows(
    table_name: str,
    rows: list[dict[str, Any]],
    keys: list[str],
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = _row_key(row, keys)
        if key in indexed:
            continue
        indexed[key] = row
    return indexed


def _row_key(row: dict[str, Any], keys: list[str]) -> str:
    payload = {key: row.get(key) for key in keys}
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _key_payload(keys: list[str], row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in keys}


def _content_hash(row: dict[str, Any]) -> str:
    existing = row.get("_row_hash")
    if isinstance(existing, str) and existing:
        return existing
    content = {key: value for key, value in row.items() if key not in _METADATA_FIELDS}
    return _row_hash(content)


def _changed_fields(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    previous_content = {
        key: value for key, value in previous.items() if key not in _METADATA_FIELDS
    }
    current_content = {key: value for key, value in current.items() if key not in _METADATA_FIELDS}
    fields = sorted(set(previous_content) | set(current_content))
    return [field for field in fields if previous_content.get(field) != current_content.get(field)]


def _duplicate_diagnostic(
    table_name: str,
    side: str,
    rows: list[dict[str, Any]],
    keys: list[str],
) -> dict[str, Any]:
    return {
        "level": "warning",
        "code": "duplicate_keys",
        "table": table_name,
        "side": side,
        "key_fields": keys,
        "duplicates": len(rows) - len({_row_key(row, keys) for row in rows}),
        "message": f"{side} table '{table_name}' contains duplicate keys",
    }
