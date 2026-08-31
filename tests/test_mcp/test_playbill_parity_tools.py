"""MCP parity tools own derivation and reuse existing daemon operations."""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args

import pytest

from cruxible_client import contracts
from cruxible_client.authoring.examples import AuthoringExampleName, authoring_example
from cruxible_client.authoring.inputs import ClaimInput
from cruxible_core.mcp import handlers
from cruxible_core.mcp.tool_prompts import tool_description
from tests.test_playbill._claim_type_support import claim_type_input_example


def _coordinate() -> contracts.PlaybillAcceptedCoordinate:
    return contracts.PlaybillAcceptedCoordinate(
        git_oid="1" * 40,
        semantic_root="sha256:" + "2" * 64,
        generation_root="sha256:" + "3" * 64,
        compiler_digest="sha256:" + "4" * 64,
    )


def test_examples_are_the_model_factories_not_copied_literals() -> None:
    result = handlers.handle_playbill_authoring_example("claim-flow-a")

    assert result.payload == authoring_example("claim-flow-a")
    assert result.name == "claim-flow-a"


def test_governed_query_example_has_mcp_client_factory_parity() -> None:
    result = handlers.handle_playbill_authoring_example("query-claims-by-type")

    assert result.payload == authoring_example("query-claims-by-type")
    assert result.payload.kind == "query_definition"
    assert result.payload.query_definition.artifact_format == "playbill-query-definition-v1"
    assert result.name == "query-claims-by-type"


def test_public_example_vocabulary_exactly_matches_authoring_input_examples() -> None:
    assert get_args(contracts.PlaybillAuthoringExampleName) == get_args(AuthoringExampleName)


def test_claim_type_uses_typed_proposal_input_not_a_coordinator_example() -> None:
    assert "claim-type" not in get_args(contracts.PlaybillAuthoringExampleName)
    assert "ClaimType" not in tool_description("cruxible_playbill_authoring_example")
    assert "ClaimTypeInputV1" in tool_description("cruxible_playbill_propose_claim_type")


def test_attestation_door_example_hints_have_mcp_client_parity() -> None:
    claim_id = "CLM-" + "a" * 32
    capture_digest = "sha256:" + "b" * 64
    result = handlers.handle_playbill_authoring_example(
        "claim-cite-supporting-evidence",
        claim_id=claim_id,
        capture_digest=capture_digest,
    )
    assert result.payload == authoring_example(
        "claim-cite-supporting-evidence",
        claim_id=claim_id,
        capture_digest=capture_digest,
    )
    with pytest.raises(ValueError, match="require claim_id and capture_digest"):
        authoring_example("claim-cite-supporting-evidence")
    with pytest.raises(ValueError, match="require claim_id and capture_digest"):
        handlers.handle_playbill_authoring_example(
            "claim-cite-supporting-evidence",
            claim_id=claim_id,
        )
    with pytest.raises(ValueError, match="require claim_id and capture_digest"):
        handlers.handle_playbill_authoring_example(
            "claim-cite-supporting-evidence",
            capture_digest=capture_digest,
        )
    with pytest.raises(ValueError, match="apply only"):
        handlers.handle_playbill_authoring_example(
            "claim-flow-a",
            claim_id=claim_id,
            capture_digest=capture_digest,
        )


def test_publication_prepare_handler_preserves_advisory_warnings(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    warning = contracts.PlaybillPublicationPrepareWarning(
        tag="playbill-publication-prepare-warning-v1",
        code="playbill.authoring.publication_citation_anchor_collision",
        source_id="repo.work-items",
        citation_ids=["sha256:" + "8" * 64],
    )

    def stub(_instance_id: str, _intent_id: str, *, observation: object):
        assert observation is not None
        return contracts.PlaybillInsertionPrepareResult(
            tag="playbill-insertion-prepare-result-v2",
            outcome="prepared",
            intent={"intent_id": "AIT-" + "1" * 32},
            expectation={"state": "prepared"},
            preparation={"preparation_digest": "sha256:" + "7" * 64},
            warnings=[warning],
        )

    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_api.playbill_authoring_prepare_publication", stub
    )
    result = handlers.handle_playbill_authoring_prepare_publication(
        "inst",
        "AIT-" + "1" * 32,
        {
            "tag": "playbill-publication-source-observation-v2",
            "source_id": "repo.work-items",
            "content_base64": "",
            "content_digest": (
                "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            ),
            "byte_length": 0,
        },
    )

    assert result.warnings == [warning]


def test_flow_a_bind_reads_workspace_and_sends_only_the_lowered_payload(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "corpus/decision.md"
    source.parent.mkdir(parents=True)
    source.write_text("before\nThe decision is ready.\nafter\n", encoding="utf-8")
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    captured: dict[str, Any] = {}

    class StubClient:
        def compile_playbill_authoring(
            self,
            instance_id: str,
            *,
            payload: dict[str, Any],
            intent_id: str | None,
        ) -> contracts.PlaybillAuthoringPreflightResult:
            captured.update(payload)
            return contracts.PlaybillAuthoringPreflightResult(
                verdict="passed",
                certificate={},
                frontier={},
            )

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())
    payload = ClaimInput.model_validate(authoring_example("claim-flow-a").model_dump(mode="json"))

    result = handlers.handle_playbill_authoring_bind(
        "inst_test",
        source_path="corpus/decision.md",
        anchor="The decision is ready.",
        payload=payload,
        window_lines=None,
    )

    assert result.verdict == "passed"
    assert captured["source"]["tag"] == "playbill-working-selection-observation-v1"
    assert "source_content_digest" in captured["source"]["coordinate"]


def test_readmit_and_migration_delegate_to_existing_client_routes(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    calls: list[str] = []

    class StubClient:
        def readmit_playbill_proposal(
            self, instance_id: str, proposal_id: str
        ) -> contracts.PlaybillProposalReadmitResult:
            calls.append(f"readmit:{proposal_id}")
            inspection = contracts.PlaybillProposalInspection(
                proposal={}, accepted_coordinate=_coordinate()
            )
            return contracts.PlaybillProposalReadmitResult(
                tag="playbill-proposal-readmit-result-v1",
                source_proposal_id=proposal_id,
                operation_digest="sha256:" + "5" * 64,
                proposal=inspection,
            )

        def migrate_playbill_claim_type(
            self,
            instance_id: str,
            *,
            request: dict[str, Any],
        ) -> contracts.PlaybillClaimTypeMigrationResult:
            calls.append(f"migrate:{request['tag']}")
            return contracts.PlaybillClaimTypeMigrationResult(
                tag="playbill-claim-type-migration-result-v1",
                operation_digest="sha256:" + "6" * 64,
                dependents=[],
                proposal=contracts.PlaybillProposalInspection(
                    proposal={}, accepted_coordinate=_coordinate()
                ),
            )

        def retire_playbill_claim(
            self,
            instance_id: str,
            claim_id: str,
            *,
            request: dict[str, Any],
        ) -> contracts.PlaybillClaimRetireResponse:
            calls.append(f"retire:{claim_id}:{request['reason']}")
            return contracts.PlaybillClaimRetirePreflight(
                operation_digest="sha256:" + "7" * 64,
                coordinate=_coordinate(),
                root_identity={"kind": "Claim", "name": claim_id},
                root_predecessor_digest="sha256:" + "8" * 64,
                reason="was-wrong",
                effective_until=None,
                required_dependents=[],
                diagnostics=[],
                submit_ready=True,
            )

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())
    readmitted = handlers.handle_playbill_readmit_proposal("inst_test", "proposal-1")
    migrated = handlers.handle_playbill_migrate_claim_type(
        "inst_test",
        {
            "tag": "playbill-claim-type-migration-request-v1",
            "successor": claim_type_input_example().model_dump(mode="json"),
            "dependents": [],
        },
    )
    claim_id = "CLM-0123456789abcdef0123456789abcdef"
    retired = handlers.handle_playbill_retire_claim(
        "inst_test",
        claim_id,
        {
            "tag": "playbill-claim-retire-request-v1",
            "mode": "preflight",
            "reason": "was-wrong",
            "expected_coordinate": _coordinate().model_dump(mode="json"),
        },
    )

    assert readmitted.source_proposal_id == "proposal-1"
    assert migrated.dependents == []
    assert retired.submit_ready is True
    assert calls == [
        "readmit:proposal-1",
        "migrate:playbill-claim-type-migration-request-v1",
        f"retire:{claim_id}:was-wrong",
    ]
