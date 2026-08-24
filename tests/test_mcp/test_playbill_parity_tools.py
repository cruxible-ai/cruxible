"""MCP parity tools own derivation and reuse existing daemon operations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cruxible_client import contracts
from cruxible_client.authoring.examples import authoring_example
from cruxible_client.authoring.inputs import ClaimInput
from cruxible_core.mcp import handlers
from cruxible_core.playbill.claim_type_inputs import claim_type_input_example


def _coordinate() -> contracts.PlaybillAcceptedCoordinate:
    return contracts.PlaybillAcceptedCoordinate(
        git_oid="1" * 40,
        semantic_root="sha256:" + "2" * 64,
        generation_root="sha256:" + "3" * 64,
        compiler_digest="sha256:" + "4" * 64,
    )


def test_examples_are_the_model_factories_not_copied_literals() -> None:
    result = handlers.handle_playbill_authoring_example("claim-flow-a")

    assert result.payload == authoring_example("claim-flow-a").model_dump(mode="json")
    assert result.name == "claim-flow-a"


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

    assert readmitted.source_proposal_id == "proposal-1"
    assert migrated.dependents == []
    assert calls == [
        "readmit:proposal-1",
        "migrate:playbill-claim-type-migration-request-v1",
    ]
