"""MCP seed tools reuse the deterministic planner and submit one group only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cruxible_client import contracts
from cruxible_core.mcp import handlers

EXAMPLE = Path(__file__).resolve().parents[2] / "benchmarks/playbill_taubench/seed-example"


def _coordinate() -> contracts.PlaybillAcceptedCoordinate:
    return contracts.PlaybillAcceptedCoordinate(
        git_oid="1" * 40,
        semantic_root="sha256:" + "2" * 64,
        generation_root="sha256:" + "3" * 64,
        compiler_digest="sha256:" + "4" * 64,
    )


def test_seed_plan_is_offline_and_matches_the_core_planner(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    repository = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(repository))

    result = handlers.handle_playbill_seed_plan(
        bundle_path="benchmarks/playbill_taubench/seed-example",
        proposal_name="mcp-example",
    )

    assert len(result.plan.groups) == 3
    assert result.plan_digest in result.rendered[0]
    assert result.plan.group_ids[0] == "claims"


def test_seed_apply_submits_exactly_one_content_addressed_group(
    monkeypatch,  # type: ignore[no-untyped-def]
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    subject_dir = workspace / "bundle/subjects"
    subject_dir.mkdir(parents=True)
    subject_dir.joinpath("wi-101.json").write_bytes(
        EXAMPLE.joinpath("subjects/wi-101.json").read_bytes()
    )
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    calls: list[tuple[str, str]] = []

    class StubClient:
        def playbill_whoami(self, instance_id: str) -> contracts.PlaybillWhoAmI:
            return contracts.PlaybillWhoAmI(
                actor_id="owner",
                credential_label="owner",
                actor_id_source="runtime_credential_label",
                credential_permission_mode="governed_write",
                principal_registration_status="active",
                active_principal_ids=["owner"],
                coordinate=_coordinate(),
            )

        def list_playbill_proposals(
            self, instance_id: str, *, status: str | None = None
        ) -> contracts.PlaybillProposalList:
            return contracts.PlaybillProposalList(
                coordinate=_coordinate(),
                status_filter="open",
                entries=[],
            )

        def propose_playbill_subject(
            self,
            instance_id: str,
            *,
            shell: dict[str, Any],
            proposal_name: str,
        ) -> contracts.PlaybillProposalInspection:
            target_ref = f"refs/proposals/owner/{proposal_name}"
            calls.append((shell["identity"]["name"], target_ref))
            return contracts.PlaybillProposalInspection(
                proposal={
                    "admission": {
                        "proposal_id": "proposal-1",
                        "target_ref": target_ref,
                    }
                },
                accepted_coordinate=_coordinate(),
            )

    monkeypatch.setattr(handlers, "_get_client", lambda: StubClient())

    result = handlers.handle_playbill_seed_apply(
        "inst_test",
        bundle_path="bundle",
        proposal_name="mcp-example",
        group_id=None,
    )

    assert result.group_id == "subject:project.work_item/wi-101"
    assert result.proposal_id == "proposal-1"
    assert result.next_group_id is None
    assert len(calls) == 1
    assert calls[0][1] == result.target_ref
