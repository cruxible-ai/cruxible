"""CLI curation: list is a pure read, observe records one explicit local scan."""

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
    ProjectionClaimBacking,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.cli.main import cli
from cruxible_core.service.discovery.curation import PlaybillCurationObserveRequestV1

COORDINATE = contracts.AcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)


def _listed(**values: Any) -> contracts.CurationListResult:
    return contracts.CurationListResult(
        coordinate=COORDINATE,
        generation=3,
        operational_head_digest="sha256:" + "5" * 64,
        detection=contracts.CurationDetection(
            state="current",
            trigger="live",
            detected_through_generation=3,
            detected_at="2026-09-01T12:00:00Z",
        ),
        detector_coverage=[],
        result_digest="sha256:" + "6" * 64,
        **values,
    )


def test_cli_curation_list_is_one_read_that_prints_each_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    item = {
        "item_id": "sha256:" + "a" * 64,
        "pattern_kind": "playbill.curation.qualifier_crystallization.v1",
        "subject": {"kind": "ClaimType", "name": "project.work_item.status"},
        "status": "open",
        "latest_event_digest": "sha256:" + "b" * 64,
    }

    class StubClient:
        def list_curation(self, instance_id: str, **values: Any) -> contracts.CurationListResult:
            calls.append((instance_id, values))
            return _listed(
                items=[item],
                inactive_detectors=[
                    contracts.CurationInactiveDetector(
                        pattern_kind="playbill.curation.dead_vocabulary.v1",
                        reason="consumption_receipts_off",
                    )
                ],
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://curation.example.test",
            "--instance-id",
            "inst",
            "curation",
            "list",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "generation 3: 1 item(s); detection current" in result.output
    assert (
        f"{item['item_id']}  qualifier_crystallization.v1  ClaimType:project.work_item.status  "
        f"latest={item['latest_event_digest']}"
    ) in result.output
    assert "Inactive: playbill.curation.dead_vocabulary.v1 (consumption_receipts_off)" in (
        result.output
    )
    ((instance_id, values),) = calls
    assert instance_id == "inst"
    # A pure read: no clock, no workspace scan travels.
    assert set(values) == {"access_profile", "limit", "cursor"}
    assert values["access_profile"]["profile_id"] == "cli-curation"


def test_cli_curation_observe_scans_a_real_catalog_and_declared_block(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    playbill_dir = tmp_path / ".cruxible"
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
            ProjectionClaimBacking(
                identity=ArtifactIdentity(kind="Claim", name="CLM-existing"),
                statement_digest="sha256:" + "7" * 64,
            ),
        ),
        body_digest="sha256:" + hashlib.sha256(body).hexdigest(),
    )
    (tmp_path / "runbook.md").write_bytes(
        render_projection_opening(stamp) + body + b"<!-- /cruxible:block:status -->\n"
    )
    seen: list[dict[str, object]] = []

    class CatalogClient:
        def resolve_coverage(self, instance_id: str, **values: Any) -> contracts.CoverageResult:
            assert instance_id == "inst"
            (source,) = values["observations"]
            assert source["source"]["identity"] == "corpus.runbook"
            return contracts.CoverageResult(
                coordinate=COORDINATE,
                result={
                    "tag": "playbill-coverage-result-v3",
                    "at": COORDINATE.model_dump(mode="json"),
                    "access_profile": {
                        "tag": "playbill-coverage-access-profile-v1",
                        "profile_id": "cruxible.coverage.read",
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

        def observe_curation(
            self,
            instance_id: str,
            *,
            workspace_observation: object,
            dry_run: bool | None = None,
            at: str | None = None,
        ) -> contracts.CurationObserveResult:
            assert instance_id == "inst"
            assert isinstance(workspace_observation, dict)
            (source,) = workspace_observation["source_observations"]
            assert source["tag"] == "playbill-next-source-observation-v4"
            assert source["document_id"] == "runbook"
            assert source["scan_notes"] == []
            assert source["commitment_scan_proofs"] == []
            assert len(source["marker_summaries"]) == 1
            PlaybillCurationObserveRequestV1.model_validate(
                {"workspace_observation": workspace_observation, "dry_run": dry_run, "at": at}
            )
            seen.append(workspace_observation)
            return contracts.CurationObserveResult(
                coordinate=COORDINATE,
                generation=1,
                observation_coverage={
                    "tag": "playbill-curation-observation-coverage-v1",
                    "source_count": 1,
                    "observed_block_count": 1,
                    "omitted_source_count": 0,
                    "omissions": [],
                },
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: CatalogClient())
    base = [
        "--server-url",
        "https://curation.example.test",
        "--instance-id",
        "inst",
        "curation",
        "observe",
        "--workspace-root",
        str(tmp_path),
    ]

    text_result = CliRunner().invoke(cli, base)
    json_result = CliRunner().invoke(cli, [*base, "--json"])

    assert text_result.exit_code == 0, text_result.output
    assert "Recorded 1 declared block(s) at generation 1." in text_result.output
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
        (
            [
                "accept-fixed",
                "--generation",
                "4",
                "--attribution-ref",
                "ticket-9",
            ],
            "accept_fixed",
        ),
        (["suppress", "--scope", "lineage", "--until-generation", "9"], "suppress"),
        (["unsuppress"], "unsuppress"),
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
        ) -> contracts.CurationActionResult:
            calls.append((operation, values))
            return contracts.CurationActionResult(
                coordinate=contracts.AcceptedCoordinate(
                    git_oid="1" * 64,
                    semantic_root="sha256:" + "2" * 64,
                    generation_root="sha256:" + "3" * 64,
                    compiler_digest="sha256:" + "4" * 64,
                ),
                generation=3,
                operational_head_digest="sha256:" + "5" * 64,
                item={"item_id": values["item_id"], "status": "resolved"},
            )

        def overrule_curation(
            self, _instance_id: str, **values: object
        ) -> contracts.CurationActionResult:
            return self._action("overrule", values)

        def accept_fixed_curation(
            self, _instance_id: str, **values: object
        ) -> contracts.CurationActionResult:
            return self._action("accept_fixed", values)

        def suppress_curation(
            self, _instance_id: str, **values: object
        ) -> contracts.CurationActionResult:
            return self._action("suppress", values)

        def unsuppress_curation(
            self, _instance_id: str, **values: object
        ) -> contracts.CurationActionResult:
            return self._action("unsuppress", values)

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://curation.example.test",
            "--instance-id",
            "inst",
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


@pytest.mark.parametrize(
    ("command", "phrase"),
    (
        ("overrule", "permanently"),
        ("accept-fixed", "--generation"),
        ("suppress", "lineage"),
        ("unsuppress", "Lift a suppression"),
        ("observe", "block-churn"),
    ),
)
def test_curation_commands_carry_their_own_help(command: str, phrase: str) -> None:
    result = CliRunner().invoke(cli, ["curation", command, "--help"])
    assert result.exit_code == 0, result.output
    assert phrase in result.output
