"""CLI curation list performs one explicit local scan."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_client.authoring.blocks import render_projection_opening
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.declared_blocks import (
    ProjectionBlockStampV1,
    ProjectionClaimBackingV1,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.cli.main import cli
from cruxible_core.service.playbill_curation import PlaybillCurationListRequestV1

COORDINATE = contracts.PlaybillAcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)


def test_cli_curation_list_scans_then_calls_one_route(monkeypatch: pytest.MonkeyPatch) -> None:
    observation = {
        "tag": "playbill-next-workspace-observation-v1",
        "source_observations": [],
    }
    calls: list[tuple[str, str, object, object]] = []

    class StubClient:
        def list_playbill_curation(
            self,
            instance_id: str,
            *,
            evaluation_time: str,
            access_profile: object,
            workspace_observation: object,
        ) -> contracts.PlaybillCurationListResult:
            calls.append((instance_id, evaluation_time, access_profile, workspace_observation))

            return contracts.PlaybillCurationListResult(
                coordinate=contracts.PlaybillAcceptedCoordinate(
                    git_oid="1" * 64,
                    semantic_root="sha256:" + "2" * 64,
                    generation_root="sha256:" + "3" * 64,
                    compiler_digest="sha256:" + "4" * 64,
                ),
                generation=3,
                evaluation_time=evaluation_time,
                operational_head_digest="sha256:" + "5" * 64,
                items=[],
                detector_coverage=[],
                observation_coverage={
                    "tag": "playbill-curation-observation-coverage-v1",
                    "source_count": 0,
                    "observed_block_count": 0,
                    "omitted_source_count": 0,
                    "omissions": [],
                },
                result_digest="sha256:" + "6" * 64,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.observe_playbill_next_workspace",
        lambda _root: observation,
    )
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.observe_playbill_next_workspace_with_coverage",
        lambda _client, _instance_id, _root, **_values: (observation, None),
    )
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://curation.example.test",
            "--instance-id",
            "inst",
            "playbill",
            "curation",
            "list",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "generation 3: 0 item(s)" in result.output
    assert len(calls) == 1
    assert calls[0][0] == "inst"
    assert isinstance(calls[0][2], dict)
    assert calls[0][2]["profile_id"] == "cli-curation"
    assert calls[0][3] == observation


def test_cli_curation_list_enriches_a_real_catalog_and_declared_block_for_text_and_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    playbill_dir = tmp_path / ".playbill"
    playbill_dir.mkdir()
    (playbill_dir / "sources.yaml").write_text(
        "tag: playbill-source-catalog-v1\n"
        "catalog_kind: portable\n"
        "entries:\n"
        "  - name: corpus.runbook\n"
        "    locator: runbook.md\n"
        "    document_id: runbook\n"
        "    document_kind: runbook\n"
        "    title: Runbook\n"
        "    media_type: text/markdown\n"
        "    governance_scope: [Document:runbook]\n",
        encoding="utf-8",
    )
    body = b"status: ready\n"
    stamp = ProjectionBlockStampV1(
        source_id="corpus.runbook",
        block_id="status",
        declared_generation=1,
        declared_coordinate=AcceptedCoordinate.model_validate(COORDINATE.model_dump(mode="json")),
        backing=(
            ProjectionClaimBackingV1(
                identity=ArtifactIdentity(kind="Claim", name="CLM-existing"),
                statement_digest="sha256:" + "7" * 64,
            ),
        ),
        body_digest="sha256:" + hashlib.sha256(body).hexdigest(),
    )
    (tmp_path / "runbook.md").write_bytes(
        render_projection_opening(stamp) + body + b"<!-- /playbill:block:status -->\n"
    )
    seen: list[dict[str, object]] = []

    class CatalogClient:
        def resolve_playbill_coverage(
            self, instance_id: str, **values: Any
        ) -> contracts.PlaybillCoverageResult:
            assert instance_id == "inst"
            (source,) = values["observations"]
            assert source["source"]["identity"] == "corpus.runbook"
            return contracts.PlaybillCoverageResult(
                coordinate=COORDINATE,
                result={
                    "tag": "playbill-coverage-result-v3",
                    "at": COORDINATE.model_dump(mode="json"),
                    "access_profile": {
                        "tag": "playbill-coverage-access-profile-v1",
                        "profile_id": "playbill.coverage.read",
                        "permitted_access_classes": ["instance", "public"],
                        "disclose_restricted_existence": True,
                    },
                    "spans": [
                        {
                            "tag": "playbill-coverage-span-result-v3",
                            "request": {"source": source["source"]},
                            "health": "complete",
                            "ambiguous_occurrence_count": 0,
                            "omitted_card_count": 0,
                            "cards": [],
                            "commitment_scan_proofs": [],
                            "citation_window_observations": [],
                        }
                    ],
                },
            )

        def list_playbill_curation(
            self,
            instance_id: str,
            *,
            evaluation_time: str,
            access_profile: object,
            workspace_observation: object,
        ) -> contracts.PlaybillCurationListResult:
            assert instance_id == "inst"
            assert isinstance(workspace_observation, dict)
            (source,) = workspace_observation["source_observations"]
            assert source["tag"] == "playbill-next-source-observation-v4"
            assert source["document_id"] == "runbook"
            assert source["scan_notes"] == []
            assert source["commitment_scan_proofs"] == []
            assert len(source["marker_summaries"]) == 1
            PlaybillCurationListRequestV1.model_validate(
                {
                    "evaluation_time": evaluation_time,
                    "access_profile": access_profile,
                    "workspace_observation": workspace_observation,
                }
            )
            seen.append(workspace_observation)
            return contracts.PlaybillCurationListResult(
                coordinate=COORDINATE,
                generation=1,
                evaluation_time=evaluation_time,
                operational_head_digest="sha256:" + "5" * 64,
                items=[],
                detector_coverage=[],
                observation_coverage={
                    "tag": "playbill-curation-observation-coverage-v1",
                    "source_count": 1,
                    "observed_block_count": 1,
                    "omitted_source_count": 0,
                    "omissions": [],
                },
                result_digest="sha256:" + "6" * 64,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: CatalogClient())
    base = [
        "--server-url",
        "https://curation.example.test",
        "--instance-id",
        "inst",
        "playbill",
        "curation",
        "list",
        "--workspace-root",
        str(tmp_path),
    ]

    text_result = CliRunner().invoke(cli, base)
    json_result = CliRunner().invoke(cli, [*base, "--json"])

    assert text_result.exit_code == 0, text_result.output
    assert "observed 1 declared block(s)" in text_result.output
    assert json_result.exit_code == 0, json_result.output
    payload = json.loads(json_result.output)
    assert payload["observation_coverage"]["observed_block_count"] == 1
    assert len(seen) == 2


@pytest.mark.parametrize(
    ("command", "expected_operation"),
    (
        (["overrule"], "overrule"),
        (
            [
                "accept-fixed",
                "--proposal-id",
                "sha256:" + "3" * 64,
                "--changeset-digest",
                "sha256:" + "4" * 64,
            ],
            "accept_fixed",
        ),
        (["suppress", "--scope", "pattern", "--until-generation", "9"], "suppress"),
    ),
)
def test_cli_curation_lifecycle_commands_delegate_once(
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    expected_operation: str,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class StubClient:
        def _action(
            self, operation: str, values: dict[str, object]
        ) -> contracts.PlaybillCurationActionResult:
            calls.append((operation, values))
            return contracts.PlaybillCurationActionResult(
                coordinate=contracts.PlaybillAcceptedCoordinate(
                    git_oid="1" * 64,
                    semantic_root="sha256:" + "2" * 64,
                    generation_root="sha256:" + "3" * 64,
                    compiler_digest="sha256:" + "4" * 64,
                ),
                generation=3,
                operational_head_digest="sha256:" + "5" * 64,
                item={"item_id": values["item_id"], "status": "resolved"},
            )

        def overrule_playbill_curation(
            self, _instance_id: str, **values: object
        ) -> contracts.PlaybillCurationActionResult:
            return self._action("overrule", values)

        def accept_fixed_playbill_curation(
            self, _instance_id: str, **values: object
        ) -> contracts.PlaybillCurationActionResult:
            return self._action("accept_fixed", values)

        def suppress_playbill_curation(
            self, _instance_id: str, **values: object
        ) -> contracts.PlaybillCurationActionResult:
            return self._action("suppress", values)

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://curation.example.test",
            "--instance-id",
            "inst",
            "playbill",
            "curation",
            *command,
            "sha256:" + "1" * 64,
            "--expected-latest-event-digest",
            "sha256:" + "2" * 64,
            "--reason",
            "operator-reviewed mechanical facts",
        ],
    )

    assert result.exit_code == 0, result.output
    assert [name for name, _values in calls] == [expected_operation]
