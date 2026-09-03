"""Contract-freeze guardrails for the Playbill-only HTTP surface."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.support.http_surface import (
    generate_http_surface_manifest,
    generate_openapi_spec,
    load_http_surface_snapshot,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SURFACE_SNAPSHOT_PATH = REPO_ROOT / "tests/goldens/http_surface/http_surface_snapshot.json"

ERROR_ENVELOPE_FIELDS = {
    "error_code",
    "error_type",
    "message",
    "errors",
    "context",
    "mutation_receipt_id",
    # P2-B5 U4: the structured repair carrier replaces prose repair strings.
    "repair",
}
# FastAPI renames a model to `<name>-Input`/`<name>-Output` when one model is
# reachable in both request and response position with a mode-dependent schema.
# Typing an authoring input union into a served response therefore renames every
# frozen component that union reaches, across the whole document. This pin names
# the only pair v1 accepts, so that regression fails here instead of silently
# moving the frozen schema names.
SPLIT_COMPONENT_NAMES = {
    "PredictionObservationSelectorV1-Input",
    "PredictionObservationSelectorV1-Output",
}
STANDARD_ERROR_STATUSES = {"400", "401", "403", "404", "409", "422", "500"}
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def test_http_surface_snapshot_is_current() -> None:
    snapshot = load_http_surface_snapshot(SURFACE_SNAPSHOT_PATH)
    current = generate_http_surface_manifest()

    if current == snapshot:
        return

    added = sorted(set(current) - set(snapshot))
    removed = sorted(set(snapshot) - set(current))
    changed = sorted(
        path for path in set(snapshot) & set(current) if snapshot[path] != current[path]
    )
    pytest.fail(
        "HTTP surface drifted from the frozen snapshot. Run "
        "`uv run python scripts/update_http_surface_snapshot.py` and review the diff.\n"
        f"Added paths: {added}\nRemoved paths: {removed}\nChanged paths: {changed}"
    )


def test_http_catalog_is_playbill_plus_generic_host_transport_only() -> None:
    paths = set(generate_http_surface_manifest())
    generic = {
        "/health",
        "/version",
        "/api/v1/runtime/instances",
        "/api/v1/server/info",
        "/api/v1/server/restart",
        "/api/v1/{instance_id}/runtime/bootstrap/claim",
        "/api/v1/{instance_id}/runtime/credentials",
        "/api/v1/{instance_id}/runtime/credentials/{credential_id}/revoke",
        "/api/v1/{instance_id}/runtime/credentials/{credential_id}/rotate",
    }
    assert paths - generic
    assert all(path in generic or "/playbill/" in path for path in paths)


def test_playbill_openapi_exposes_typed_coordinate_and_host_results() -> None:
    spec = generate_openapi_spec()
    host_schema = spec["paths"]["/api/v1/runtime/instances"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert _component_ref_name(host_schema) == "PlaybillHostResult"

    coordinate = spec["components"]["schemas"]["PlaybillAcceptedCoordinate"]
    assert set(coordinate["properties"]) == {
        "tag",
        "git_oid",
        "semantic_root",
        "generation_root",
        "compiler_digest",
    }
    assert set(coordinate["required"]) == {
        "git_oid",
        "semantic_root",
        "generation_root",
        "compiler_digest",
    }


def test_no_response_model_splits_a_frozen_component_name() -> None:
    spec = generate_openapi_spec()
    names = set(spec["components"]["schemas"])
    split = {name for name in names if name.endswith(("-Input", "-Output"))}

    assert split == SPLIT_COMPONENT_NAMES, (
        "a served response model dragged frozen request components into "
        "serialization mode; carry the served view object instead of the "
        f"authoring union. Unexpected: {sorted(split - SPLIT_COMPONENT_NAMES)}"
    )
    assert "ClaimType" in names


def test_openapi_routes_declare_standard_error_envelope() -> None:
    spec = generate_openapi_spec()
    error_schema = spec["components"]["schemas"]["ErrorResponse"]
    assert set(error_schema["properties"]) == ERROR_ENVELOPE_FIELDS
    assert "HTTPValidationError" not in spec["components"]["schemas"]

    offenders: list[str] = []
    for path, operations in spec["paths"].items():
        if not path.startswith("/api/v1/"):
            continue
        for method, operation in operations.items():
            if method not in HTTP_METHODS:
                continue
            responses = operation.get("responses", {})
            for status in STANDARD_ERROR_STATUSES:
                schema = (
                    responses.get(status, {})
                    .get("content", {})
                    .get("application/json", {})
                    .get("schema", {})
                )
                if _component_ref_name(schema) != "ErrorResponse":
                    offenders.append(f"{method.upper()} {path} missing {status}=ErrorResponse")

    assert offenders == []


def _component_ref_name(schema: dict[str, Any]) -> str | None:
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref:
        return None
    return ref.rsplit("/", 1)[-1]
