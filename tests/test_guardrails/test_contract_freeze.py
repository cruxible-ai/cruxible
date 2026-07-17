"""Contract-freeze guardrails: pin the 0.2 public surface so drift fails CI.

The client model shapes are pinned by tests/test_client/test_contract_snapshot.py;
these tests pin everything around them — the HTTP surface, the envelope
conventions, the provenance vocabulary, and the deliberately exempt receipt
shape — per docs/dev/api-consistency-pass-0.2.md.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from tests.support.http_surface import (
    generate_http_surface_manifest,
    generate_openapi_spec,
    load_http_surface_snapshot,
)

from cruxible_client import contracts
from cruxible_core.graph.provenance import (
    CANONICAL_SOURCE_REFS,
    SOURCE_REF_ADD_RELATIONSHIP,
    SOURCE_REF_BATCH_DIRECT_WRITE,
)
from cruxible_core.receipt.types import Receipt

REPO_ROOT = Path(__file__).resolve().parents[2]
SURFACE_SNAPSHOT_PATH = REPO_ROOT / "tests/goldens/http_surface/http_surface_snapshot.json"

ENVELOPE_FIELDS = {"total", "limit", "offset", "truncated"}
ERROR_ENVELOPE_FIELDS = {
    "error_code",
    "error_type",
    "message",
    "errors",
    "context",
    "mutation_receipt_id",
}
STANDARD_ERROR_STATUSES = {"400", "401", "403", "404", "409", "422", "500"}
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}

# Composite documents and inputs that legitimately carry list fields without
# being list endpoints (convention 1 exemption + write-side inputs).
ENVELOPE_EXEMPT_MODELS = {
    "BatchDirectWritePayload",  # write-side input payload
    "QueryIncludeResult",  # nested side-context list inside a query row
    "QueryGraphIncludeResult",  # the same side-context list under graph layout
}


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


def test_openapi_query_tool_result_items_are_typed() -> None:
    spec = generate_openapi_spec()
    items_schema = spec["components"]["schemas"]["QueryToolResult"]["properties"]["items"]
    item_variants = items_schema["items"].get("anyOf") or items_schema["items"].get("oneOf")
    assert item_variants is not None

    item_refs = {_component_ref_name(variant) for variant in item_variants}
    assert {"QueryEntityItem", "QueryPathItem"}.issubset(item_refs)
    assert "QueryRelationshipItem" in item_refs
    assert "QueryProjectedItem" in item_refs


def test_openapi_stats_route_declares_status_counts() -> None:
    spec = generate_openapi_spec()
    schema = spec["paths"]["/api/v1/{instance_id}/stats"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert _component_ref_name(schema) == "StatsResult"

    status_counts_schema = spec["components"]["schemas"]["StatsResult"]["properties"][
        "status_counts"
    ]
    assert status_counts_schema["type"] == "object"
    assert status_counts_schema["additionalProperties"]["type"] == "object"


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


def test_openapi_receipt_route_declares_stable_receipt_shape() -> None:
    spec = generate_openapi_spec()
    schema = spec["paths"]["/api/v1/{instance_id}/receipts/{receipt_id}"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]
    assert _component_ref_name(schema) == "Receipt"

    receipt_schema = spec["components"]["schemas"]["Receipt"]
    assert {"receipt_id", "nodes", "edges", "results"}.issubset(receipt_schema["properties"])
    assert _component_ref_name(receipt_schema["properties"]["nodes"]["items"]) == "ReceiptNode"
    assert _component_ref_name(receipt_schema["properties"]["edges"]["items"]) == "EvidenceEdge"


def test_openapi_view_description_documents_forwarded_params() -> None:
    spec = generate_openapi_spec()
    description = spec["paths"]["/api/v1/{instance_id}/views/{query_name}"]["get"]["description"]

    assert "Non-reserved query-string keys are forwarded" in description
    assert "describe_query" in description
    assert "/api/v1/{instance_id}/queries/{query_name}" in description


def test_every_list_contract_carries_the_envelope() -> None:
    """Any contract model exposing a list `items` field must speak the envelope."""
    offenders: list[str] = []
    for name, model in inspect.getmembers(contracts, inspect.isclass):
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            continue
        if name in ENVELOPE_EXEMPT_MODELS:
            continue
        fields = model.model_fields
        if "items" not in fields:
            continue
        missing = ENVELOPE_FIELDS - set(fields)
        if missing:
            offenders.append(f"{name} missing {sorted(missing)}")
    assert offenders == [], (
        "List-shaped contract models must carry the standard envelope "
        f"(items/total/limit/offset/truncated): {offenders}"
    )


def _component_ref_name(schema: dict[str, Any]) -> str | None:
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref:
        return None
    return ref.rsplit("/", 1)[-1]


def test_provenance_source_refs_are_operation_vocabulary() -> None:
    """source_ref values are snake_case operation names, never surface spellings."""
    assert SOURCE_REF_ADD_RELATIONSHIP == "add_relationship"
    assert SOURCE_REF_BATCH_DIRECT_WRITE == "batch_direct_write"
    for ref in CANONICAL_SOURCE_REFS:
        assert ref == ref.lower()
        assert "-" not in ref and " " not in ref and not ref.startswith("cruxible_")


def test_receipt_shape_is_pinned_as_exempt() -> None:
    """Receipts are audit documents, deliberately exempt from the envelope rename.

    `results` (not `items`) is the receipt's own stable vocabulary; see the
    Deferred section of docs/dev/api-consistency-pass-0.2.md. Changing this set
    is a breaking change to persisted artifacts and requires its own decision.

    Deliberate 0.2 envelope decision (wi-governance-actor-context-normalization,
    Robert-approved): `actor_context` was promoted to a first-class receipt field
    so token-derived actor identity is usable by governance without digging into
    node detail. Additive + defaults None (older receipts still load).
    """
    assert sorted(Receipt.model_fields.keys()) == [
        "actor_context",
        "committed",
        "created_at",
        "duration_ms",
        "edges",
        "execution_options",
        "head_snapshot_id",
        "nodes",
        "operation_type",
        "parameters",
        "query_name",
        "receipt_id",
        "results",
        "workflow_mode",
    ]


def test_runtime_write_defaults_use_canonical_source_refs() -> None:
    """Every default provenance ref in the runtime API is a canonical constant."""
    from cruxible_core.runtime import api

    for fn_name in ("batch_direct_write",):
        params = inspect.signature(getattr(api, fn_name)).parameters
        default = params["provenance_source_ref"].default
        assert default in CANONICAL_SOURCE_REFS, (
            f"api.{fn_name} defaults provenance_source_ref={default!r}, "
            "which is not in the canonical vocabulary"
        )


def test_no_retired_source_ref_spellings_in_source() -> None:
    """Retired surface-spelled refs must not reappear in provenance contexts.

    The cruxible_-prefixed tool names stay legitimate as permission/tool
    identifiers; only source_ref assignments are scanned.
    """
    pattern = re.compile(
        r"source_ref[^\n=]*=\s*"
        r"\"(cruxible_add_relationship|cruxible_batch_direct_write"
        r"|add-relationship|batch-direct-write)\""
    )
    offenders: list[str] = []
    for path in (REPO_ROOT / "src").rglob("*.py"):
        for match in pattern.finditer(path.read_text()):
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0)}")
    assert offenders == [], f"Retired provenance spellings found: {offenders}"
