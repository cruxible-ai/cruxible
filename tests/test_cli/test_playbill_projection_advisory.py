"""Client-local projection evidence for proposal review."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli

COORDINATE = contracts.PlaybillAcceptedCoordinate(
    git_oid="1" * 40,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)


def test_cli_review_sends_bounded_coordinate_bound_projection_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projection = {
        "tag": "playbill-projection-coverage-observation-v1",
        "coordinate": COORDINATE.model_dump(mode="json"),
        "complete_kinds": ["Procedure"],
        "bindings": [],
    }

    class StubClient:
        def search_playbill(self, instance_id: str, *, mode: str) -> SimpleNamespace:
            assert instance_id == "inst_review"
            assert mode == "orient"
            return SimpleNamespace(coordinate=COORDINATE)

        def review_playbill_proposal(
            self,
            instance_id: str,
            proposal_id: str,
            **values: object,
        ) -> contracts.PlaybillProposalReview:
            assert instance_id == "inst_review"
            assert proposal_id == "sha256:" + "a" * 64
            observation = values["workspace_observation"]
            assert isinstance(observation, dict)
            assert "source_observations" not in observation
            assert observation["tag"] == "playbill-review-workspace-observation-v1"
            assert observation["projection_coverage"] == projection
            return contracts.PlaybillProposalReview(
                proposal_id=proposal_id,
                candidate={},
                candidate_digest="sha256:" + "b" * 64,
                parent_semantic_root="sha256:" + "c" * 64,
                settlement_base=COORDINATE,
                base_oid=COORDINATE.git_oid,
                complete_members=[],
                members=[],
                governance={},
                provenance={},
                attestation_coverage={},
                documents=[],
                redactions=[],
                projection_advisory=contracts.PlaybillProjectionAdvisory(
                    unprojected_count=1,
                    artifact_identities=["Procedure:release-guard"],
                    message=(
                        "1 changed artifact has no projection coverage; "
                        "reviewers will see raw JSON only"
                    ),
                ),
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.observe_playbill_next_workspace",
        lambda _workspace: {
            "tag": "playbill-next-workspace-observation-v1",
            "source_observations": [{"source_id": "not-yet-enriched"}],
        },
    )
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.observe_playbill_projection_coverage",
        lambda _workspace, *, coordinate: projection,
    )

    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://review.example.test",
            "--instance-id",
            "inst_review",
            "playbill",
            "proposal",
            "review",
            "sha256:" + "a" * 64,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert '"unprojected_count": 1' in result.output
    assert "reviewers will see raw JSON only" in result.output
