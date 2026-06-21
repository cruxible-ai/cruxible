"""CLI coverage for project-state ReviewRequest submission."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.cli.main import cli
from cruxible_core.service import (
    EntityWriteInput,
    RelationshipWriteInput,
    service_add_entity_inputs,
    service_add_relationship_inputs,
)

KIT_CONFIG = Path(__file__).resolve().parents[2] / "kits" / "project-state" / "config.yaml"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _run_in(directory: Path, runner: CliRunner, args: list[str]) -> Any:
    original = os.getcwd()
    try:
        os.chdir(directory)
        return runner.invoke(cli, args)
    finally:
        os.chdir(original)


def _project_state_instance(tmp_path: Path) -> CruxibleInstance:
    shutil.copy(KIT_CONFIG, tmp_path / "config.yaml")
    return CruxibleInstance.init(tmp_path, "config.yaml")


def _seed_project_state_review_context(instance: CruxibleInstance) -> None:
    service_add_entity_inputs(
        instance,
        [
            EntityWriteInput(
                entity_type="WorkItem",
                entity_id="wi-review-submit-command",
                properties={
                    "work_item_id": "wi-review-submit-command",
                    "title": "Review submit command",
                    "type": "feature",
                    "status": "active",
                    "priority": "high",
                },
            ),
            EntityWriteInput(
                entity_type="ReleaseLine",
                entity_id="rel-post-0.2",
                properties={
                    "release_line_id": "rel-post-0.2",
                    "name": "post-0.2",
                    "status": "active",
                },
            ),
            EntityWriteInput(
                entity_type="Milestone",
                entity_id="ms-client-api",
                properties={
                    "milestone_id": "ms-client-api",
                    "title": "client-api",
                    "status": "active",
                },
            ),
        ],
    )
    service_add_relationship_inputs(
        instance,
        [
            RelationshipWriteInput(
                from_type="WorkItem",
                from_id="wi-review-submit-command",
                relationship_type="work_item_in_release",
                to_type="ReleaseLine",
                to_id="rel-post-0.2",
            ),
            RelationshipWriteInput(
                from_type="WorkItem",
                from_id="wi-review-submit-command",
                relationship_type="work_item_in_milestone",
                to_type="Milestone",
                to_id="ms-client-api",
            ),
        ],
        source="test",
        source_ref="test",
    )


def test_review_submit_applies_locally_with_release_and_milestone_context(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    instance = _project_state_instance(tmp_path)
    _seed_project_state_review_context(instance)

    result = _run_in(
        instance.root,
        runner,
        [
            "review",
            "submit",
            "wi-review-submit-command",
            "--review-request-id",
            "rr-review-submit-command",
            "--change-repo",
            "cruxible-ai/cruxible-core",
            "--change-base",
            "266d1ab5d4f1ce2b5988eba639fda9bc3f1dab12",
            "--change-head",
            "abc1234567890defabc1234567890defabc12345",
            "--requested-at",
            "2026-06-21",
            "--summary",
            "Implemented review submit command.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "ReviewRequest rr-review-submit-command submitted." in result.output
    assert "Receipt: RCP-" in result.output

    graph = instance.load_graph()
    review = graph.get_entity("ReviewRequest", "rr-review-submit-command")
    assert review is not None
    assert review.properties["status"] == "requested"
    assert review.properties["change_head"] == "abc1234567890defabc1234567890defabc12345"
    assert (
        graph.get_relationship(
            "ReviewRequest",
            "rr-review-submit-command",
            "WorkItem",
            "wi-review-submit-command",
            "review_request_for_work_item",
        )
        is not None
    )
    assert (
        graph.get_relationship(
            "ReviewRequest",
            "rr-review-submit-command",
            "ReleaseLine",
            "rel-post-0.2",
            "review_request_in_release",
        )
        is not None
    )
    assert (
        graph.get_relationship(
            "ReviewRequest",
            "rr-review-submit-command",
            "Milestone",
            "ms-client-api",
            "review_request_in_milestone",
        )
        is not None
    )


def test_review_submit_server_mode_builds_batch_payload(
    monkeypatch,
    runner: CliRunner,
) -> None:
    captured: dict[str, Any] = {}

    class StubClient:
        def get_entity(self, instance_id, entity_type, entity_id):
            captured.setdefault("get_entity_calls", []).append(
                (instance_id, entity_type, entity_id)
            )
            return contracts.GetEntityResult(
                found=entity_type == "WorkItem",
                entity_type=entity_type,
                entity_id=entity_id,
            )

        def list(self, instance_id, *, resource_type, relationship_type, limit, offset):
            captured.setdefault("list_calls", []).append(
                (instance_id, resource_type, relationship_type, limit, offset)
            )
            all_items = {
                "work_item_in_release": [
                    {
                        "from_type": "WorkItem",
                        "from_id": "wi-review-submit-command",
                        "to_type": "ReleaseLine",
                        "to_id": "rel-post-0.2",
                        "relationship_type": "work_item_in_release",
                    },
                    {
                        "from_type": "WorkItem",
                        "from_id": "wi-other",
                        "to_type": "ReleaseLine",
                        "to_id": "rel-other",
                        "relationship_type": "work_item_in_release",
                    },
                ],
                "work_item_in_milestone": [
                    {
                        "from_type": "WorkItem",
                        "from_id": "wi-review-submit-command",
                        "to_type": "Milestone",
                        "to_id": "ms-client-api",
                        "relationship_type": "work_item_in_milestone",
                    }
                ],
                "milestone_in_release": [
                    {
                        "from_type": "Milestone",
                        "from_id": "ms-client-api",
                        "to_type": "ReleaseLine",
                        "to_id": "rel-post-0.2",
                        "relationship_type": "milestone_in_release",
                    }
                ],
            }[relationship_type]
            return contracts.ListResult(
                items=all_items[offset : offset + limit],
                total=len(all_items),
                limit=limit,
                offset=offset,
            )

        def batch_direct_write(self, instance_id, payload, *, dry_run=False):
            captured["instance_id"] = instance_id
            captured["payload"] = payload
            captured["dry_run"] = dry_run
            return contracts.BatchDirectWriteResult(
                dry_run=dry_run,
                valid=True,
                entities_added=1,
                relationships_added=3,
                receipt_id="RCP-review-submit",
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "review",
            "submit",
            "wi-review-submit-command",
            "--change-repo",
            "cruxible-ai/cruxible-core",
            "--change-base",
            "266d1ab5d4f1ce2b5988eba639fda9bc3f1dab12",
            "--change-head",
            "abc1234567890defabc1234567890defabc12345",
            "--requested-by",
            "codex",
            "--reviewer",
            "human",
            "--requested-at",
            "2026-06-21",
            "--summary",
            "Ready for review.",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "ReviewRequest rr-wi-review-submit-command-abc123456789 submitted." in result.output
    assert captured["instance_id"] == "inst_123"
    assert captured["dry_run"] is False
    payload = captured["payload"]
    assert isinstance(payload, contracts.BatchDirectWritePayload)
    assert payload.entities[0].entity_id == "rr-wi-review-submit-command-abc123456789"
    assert payload.entities[0].properties == {
        "review_request_id": "rr-wi-review-submit-command-abc123456789",
        "title": "Review wi-review-submit-command at abc123456789",
        "status": "requested",
        "summary": "Ready for review.",
        "change_repo": "cruxible-ai/cruxible-core",
        "change_base": "266d1ab5d4f1ce2b5988eba639fda9bc3f1dab12",
        "change_head": "abc1234567890defabc1234567890defabc12345",
        "requested_at": "2026-06-21",
        "requested_by": "codex",
        "reviewer": "human",
    }
    assert [
        (edge.relationship_type, edge.to_type, edge.to_id) for edge in payload.relationships
    ] == [
        ("review_request_for_work_item", "WorkItem", "wi-review-submit-command"),
        ("review_request_in_release", "ReleaseLine", "rel-post-0.2"),
        ("review_request_in_milestone", "Milestone", "ms-client-api"),
    ]


def test_review_submit_json_dry_run_reports_review_id_and_receipt(
    monkeypatch,
    runner: CliRunner,
) -> None:
    class StubClient:
        def get_entity(self, instance_id, entity_type, entity_id):
            return contracts.GetEntityResult(
                found=entity_type == "WorkItem",
                entity_type=entity_type,
                entity_id=entity_id,
            )

        def list(self, instance_id, *, resource_type, relationship_type, limit, offset):
            return contracts.ListResult(items=[], total=0, limit=limit, offset=offset)

        def batch_direct_write(self, instance_id, payload, *, dry_run=False):
            return contracts.BatchDirectWriteResult(
                dry_run=dry_run,
                valid=True,
                entities_added=1,
                relationships_added=1,
                receipt_id=None,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "review",
            "submit",
            "wi-review-submit-command",
            "--review-request-id",
            "rr-explicit",
            "--change-repo",
            "cruxible-ai/cruxible-core",
            "--change-base",
            "266d1ab5d4f1ce2b5988eba639fda9bc3f1dab12",
            "--change-head",
            "abc1234567890defabc1234567890defabc12345",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["review_request_id"] == "rr-explicit"
    assert payload["work_item_id"] == "wi-review-submit-command"
    assert payload["dry_run"] is True
    assert payload["receipt_id"] is None
    assert payload["batch_direct_write"]["relationships_added"] == 1


def test_review_submit_rejects_missing_work_item(monkeypatch, runner: CliRunner) -> None:
    class StubClient:
        def get_entity(self, instance_id, entity_type, entity_id):
            return contracts.GetEntityResult(
                found=False,
                entity_type=entity_type,
                entity_id=entity_id,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    result = runner.invoke(
        cli,
        [
            "--server-url",
            "http://server",
            "--instance-id",
            "inst_123",
            "review",
            "submit",
            "wi-missing",
            "--change-repo",
            "cruxible-ai/cruxible-core",
            "--change-base",
            "266d1ab5d4f1ce2b5988eba639fda9bc3f1dab12",
            "--change-head",
            "abc1234567890defabc1234567890defabc12345",
        ],
    )

    assert result.exit_code == 1
    assert "WorkItem wi-missing not found" in result.output
