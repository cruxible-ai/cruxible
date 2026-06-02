"""Contract-freeze tests for the public cruxible-client surface."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from cruxible_client import contracts
from tests.support.client_contracts import (
    compare_contract_manifests,
    generate_contract_manifest,
    load_contract_snapshot,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = REPO_ROOT / "tests/goldens/cruxible_client/contracts_snapshot.json"
CLIENT_PATH = REPO_ROOT / "packages/cruxible-client/src/cruxible_client/http_client.py"


def test_client_contract_snapshot_is_current() -> None:
    snapshot = load_contract_snapshot(SNAPSHOT_PATH)
    current = generate_contract_manifest()

    if current == snapshot:
        return

    report = compare_contract_manifests(snapshot, current)
    details = [*report.breaking, *report.compatible]
    detail_text = "\n".join(f"- {item}" for item in details[:25])
    pytest.fail(
        "cruxible-client contract snapshot drifted. Run "
        "`uv run python scripts/update_client_contract_snapshot.py` and review "
        "`tests/goldens/cruxible_client/contracts_snapshot.json`."
        + (f"\n\nDetected changes:\n{detail_text}" if detail_text else "")
    )


def test_client_methods_parse_contract_return_models() -> None:
    tree = ast.parse(CLIENT_PATH.read_text(encoding="utf-8"))
    client_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "CruxibleClient"
    )
    violations: list[str] = []

    for method in client_class.body:
        if not isinstance(method, ast.FunctionDef):
            continue
        contract_name = _contract_return_name(method)
        if contract_name is None:
            continue
        returns = [node for node in ast.walk(method) if isinstance(node, ast.Return)]
        if not returns:
            violations.append(f"{method.name}: missing return")
            continue
        for return_node in returns:
            if not _returns_parse_model(return_node, contract_name):
                violations.append(
                    f"{method.name}: must return _parse_model(..., contracts.{contract_name})"
                )

    assert violations == []


def _manifest(
    *,
    aliases: dict[str, Any] | None = None,
    models: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "manifest_version": 1,
        "literal_aliases": aliases or {},
        "models": models or {},
    }


def _model(**fields: dict[str, Any]) -> dict[str, Any]:
    required_fields = [
        field_name for field_name, field in fields.items() if field.get("required", False)
    ]
    return {
        "fields": fields,
        "json_schema": {},
        "required_fields": required_fields,
    }


def _field(schema: dict[str, Any], *, required: bool = False) -> dict[str, Any]:
    return {
        "has_default": not required,
        "has_default_factory": False,
        "json_name": "value",
        "required": required,
        "schema": schema,
    }


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (
            _manifest(models={"Result": _model(value=_field({"type": "string"}))}),
            _manifest(models={}),
            "Removed model Result",
        ),
        (
            _manifest(models={"Result": _model(value=_field({"type": "string"}))}),
            _manifest(models={"Result": _model()}),
            "Removed field Result.value",
        ),
        (
            _manifest(models={"Result": _model(value=_field({"type": "string"}))}),
            _manifest(models={"Result": _model(value=_field({"type": "string"}, required=True))}),
            "Field became required Result.value",
        ),
        (
            _manifest(models={"Result": _model()}),
            _manifest(models={"Result": _model(value=_field({"type": "string"}, required=True))}),
            "Added required field Result.value",
        ),
        (
            _manifest(aliases={"Mode": {"values": ["run", "apply"]}}),
            _manifest(aliases={"Mode": {"values": ["run"]}}),
            "Removed Literal value(s) from Mode",
        ),
        (
            _manifest(
                models={
                    "Result": _model(
                        value=_field({"anyOf": [{"type": "string"}, {"type": "null"}]})
                    )
                }
            ),
            _manifest(models={"Result": _model(value=_field({"type": "string"}))}),
            "Narrowed field type Result.value",
        ),
        (
            _manifest(models={"Result": _model(value=_field({"type": "string"}))}),
            _manifest(models={"Result": _model(value=_field({"type": "integer"}))}),
            "Narrowed field type Result.value",
        ),
        (
            _manifest(models={"Result": _model(mode=_field({"enum": ["run", "apply"]}))}),
            _manifest(models={"Result": _model(mode=_field({"enum": ["run"]}))}),
            "Removed enum value(s) from Result.mode",
        ),
    ],
)
def test_contract_compatibility_reports_breaking_changes(
    old: dict[str, Any],
    new: dict[str, Any],
    expected: str,
) -> None:
    report = compare_contract_manifests(old, new)

    assert not report.is_compatible
    assert any(expected in item for item in report.breaking)


def test_contract_compatibility_allows_additive_changes() -> None:
    old = _manifest(
        aliases={"Mode": {"values": ["run"]}},
        models={
            "Result": _model(
                mode=_field({"enum": ["run"]}),
                score=_field({"type": "integer"}),
            )
        },
    )
    new = _manifest(
        aliases={"Mode": {"values": ["run", "apply"]}},
        models={
            "AddedResult": _model(value=_field({"type": "string"})),
            "Result": _model(
                detail=_field({"type": "string"}),
                mode=_field({"enum": ["run", "apply"]}),
                score=_field({"type": "number"}),
            ),
        },
    )

    report = compare_contract_manifests(old, new)

    assert report.breaking == ()
    assert report.is_compatible
    assert "Added model AddedResult" in report.compatible
    assert "Added optional field Result.detail" in report.compatible
    assert any("Added Literal value(s) to Mode" in item for item in report.compatible)
    assert any("Added enum value(s) to Result.mode" in item for item in report.compatible)


def _contract_return_name(method: ast.FunctionDef) -> str | None:
    annotation = method.returns
    if not isinstance(annotation, ast.Attribute):
        return None
    if not isinstance(annotation.value, ast.Name) or annotation.value.id != "contracts":
        return None
    value = getattr(contracts, annotation.attr)
    if not isinstance(value, type) or not issubclass(value, BaseModel):
        return None
    return annotation.attr


def _returns_parse_model(return_node: ast.Return, contract_name: str) -> bool:
    value = return_node.value
    if not isinstance(value, ast.Call):
        return False
    if not isinstance(value.func, ast.Attribute) or value.func.attr != "_parse_model":
        return False
    if not isinstance(value.func.value, ast.Name) or value.func.value.id != "self":
        return False
    if len(value.args) < 2:
        return False
    model_arg = value.args[1]
    return (
        isinstance(model_arg, ast.Attribute)
        and isinstance(model_arg.value, ast.Name)
        and model_arg.value.id == "contracts"
        and model_arg.attr == contract_name
    )
