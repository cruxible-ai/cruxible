"""The MCP write tools: declared parameters, the default profile, and their requests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.write import (
    FileEvidence,
    RetireRequest,
    SetChange,
    SetRequest,
    WriteRequest,
)
from cruxible_core.errors import DataValidationError
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server


def _object_properties(schema: dict[str, Any], defs: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    stack = [schema]
    while stack:
        option = stack.pop()
        if "$ref" in option:
            option = defs[option["$ref"].rsplit("/", 1)[1]]
        stack.extend(option.get("anyOf", []))
        stack.extend(option.get("oneOf", []))
        if option.get("type") == "array" and isinstance(option.get("items"), dict):
            stack.append(option["items"])
        if option.get("type") == "object":
            found.append(option)
    return found


@pytest.mark.parametrize(
    ("tool", "required", "declared"),
    [
        (
            "cruxible_set",
            ["subject", "field", "value", "because"],
            {
                "instance_id",
                "subject",
                "field",
                "value",
                "because",
                "evidence",
                "role",
                "contend",
                "expect",
                "dry_run",
                "accept",
                "at",
            },
        ),
        (
            "cruxible_retire",
            ["target", "because"],
            {"instance_id", "target", "because", "reason", "expect", "dry_run", "accept", "at"},
        ),
        (
            "cruxible_write",
            ["changes", "because"],
            {"instance_id", "changes", "because", "subject", "dry_run", "accept", "at"},
        ),
    ],
)
def test_write_tools_are_default_governed_write_tools_with_declared_parameters(
    monkeypatch: pytest.MonkeyPatch, tool: str, required: list[str], declared: set[str]
) -> None:
    monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
    tools = {item.name: item for item in asyncio.run(create_server().list_tools())}

    schema = tools[tool].inputSchema
    defs = schema.get("$defs", {})
    assert sorted(schema["required"]) == sorted(required)
    assert set(schema["properties"]) == declared
    for name, prop in schema["properties"].items():
        for option in _object_properties(prop, defs):
            assert option.get("properties"), name
            assert option.get("additionalProperties") is not True, name
    assert tools[tool].outputSchema is not None


def _capture(monkeypatch: pytest.MonkeyPatch, name: str) -> list[Any]:
    captured: list[Any] = []

    def local(instance_id: str, *, request: Any) -> Any:
        assert instance_id == "inst_write"
        captured.append(request)
        return "outcome"

    monkeypatch.setattr(handlers.playbill_api, name, local)
    monkeypatch.setattr(
        handlers, "_dispatch_remote_or_local", lambda _remote, local, **_kw: local()
    )
    return captured


def test_handlers_build_typed_requests_for_the_mcp_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sets = _capture(monkeypatch, "playbill_set")
    retires = _capture(monkeypatch, "playbill_retire")
    writes = _capture(monkeypatch, "playbill_write")

    handlers.handle_playbill_set(
        "inst_write",
        subject="dev.item/a",
        field="status",
        value="done",
        because="Shipped.",
        dry_run=True,
        at="0123456789ab",
        expect="ready",
        evidence={"kind": "capture", "capture": "CAP-0123456789ab"},
    )
    handlers.handle_playbill_retire(
        "inst_write",
        target={"subject": "dev.item/a", "field": "status"},
        because="Gone.",
        expect=["done", "ready"],
    )
    handlers.handle_playbill_write(
        "inst_write",
        changes=[
            {
                "op": "add",
                "subject": "dev.item/a",
                "field": "governs",
                "value": "dev.item/b",
                "expect_absent": True,
            },
            {
                "op": "set",
                "field": "status",
                "value": "done",
                "evidence": {"kind": "contract", "contract": "repo.reports"},
            },
        ],
        because="Linked.",
        subject="dev.item/a",
    )

    (set_request,) = sets
    assert isinstance(set_request, SetRequest)
    assert set_request.surface == "mcp" and set_request.dry_run and set_request.at == "0123456789ab"
    assert set_request.expect == "ready"
    assert set_request.evidence.capture == "CAP-0123456789ab"  # type: ignore[union-attr]
    (retire_request,) = retires
    assert isinstance(retire_request, RetireRequest) and retire_request.surface == "mcp"
    assert retire_request.expect == ("done", "ready")
    (write_request,) = writes
    assert isinstance(write_request, WriteRequest)
    assert write_request.changes[0].op == "add"
    assert write_request.changes[0].expect_absent  # type: ignore[union-attr]
    assert write_request.subject == "dev.item/a"
    assert write_request.changes[1].subject is None  # type: ignore[union-attr]
    assert write_request.changes[1].evidence.contract == "repo.reports"  # type: ignore[union-attr]


def test_a_malformed_write_names_the_json_path_and_an_example(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(monkeypatch, "playbill_write")
    with pytest.raises(DataValidationError) as caught:
        handlers.handle_playbill_write(
            "inst_write",
            changes=[{"op": "set", "subject": "no-slash", "field": "status", "value": "x"}],
            because="x",
        )
    assert "example:" in str(caught.value)
    assert any(error.startswith("$.changes.0.set.subject") for error in caught.value.errors)


def test_file_evidence_is_read_in_the_mcp_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / ".cruxible").mkdir(parents=True)
    (workspace / "notes.md").write_text("# Notes\n\nStatus: done\n", encoding="utf-8")
    (workspace / ".cruxible" / "sources.yaml").write_text(
        """\
tag: playbill-source-catalog-v1
catalog_kind: portable
entries:
  - name: repo.notes
    locator: notes.md
    document_id: notes
    document_kind: note
    title: Notes
    media_type: text/markdown
    compiler_profile: document-v1
    required_tier: governed_write
    governance_scope: [Document:notes]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    sets = _capture(monkeypatch, "playbill_set")
    writes = _capture(monkeypatch, "playbill_write")
    evidence = {"kind": "file", "file": "notes.md#Status: done"}

    handlers.handle_playbill_set(
        "inst_write",
        subject="dev.item/a",
        field="status",
        value="done",
        because="The notes say so.",
        evidence=evidence,
    )
    handlers.handle_playbill_write(
        "inst_write",
        changes=[
            {
                "op": "set",
                "subject": "dev.item/a",
                "field": "status",
                "value": "done",
                "evidence": evidence,
            }
        ],
        because="The notes say so.",
    )

    observed = sets[0].evidence
    assert isinstance(observed, FileEvidence) and observed.observation is not None
    assert observed.observation.source_id == "repo.notes"
    change = writes[0].changes[0]
    assert isinstance(change, SetChange) and isinstance(change.evidence, FileEvidence)
    assert change.evidence.observation == observed.observation


def test_write_tool_outputs_declare_each_warning_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MODE", "governed_write")
    tools = {item.name: item for item in asyncio.run(create_server().list_tools())}
    for name in ("cruxible_set", "cruxible_retire", "cruxible_write"):
        schema = tools[name].outputSchema
        assert schema is not None
        defs = schema.get("$defs", {})
        verdict = defs["VerdictNotSupportedWarning"]
        newer = defs["NewerCaptureNotCitableWarning"]
        assert verdict["properties"]["code"]["const"] == "cruxible.write.verdict_not_supported"
        assert newer["properties"]["code"]["const"] == "cruxible.write.newer_capture_not_citable"
        assert "verdict" in verdict["required"] and "capture" not in verdict["properties"]
        assert "capture" in newer["required"] and "verdict" not in newer["properties"]
