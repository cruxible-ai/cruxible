"""The declared-block CLI is a client-only, canonically parameterized adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    BlockDetachResult,
    BlockSyncItem,
    BlockSyncResult,
)
from cruxible_client.contracts.change_control import StateCoordinate
from cruxible_client.contracts.declared_blocks import (
    ProjectionBlockStampV1,
    ProjectionClaimBacking,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.cli.main import cli


def _stamp() -> ProjectionBlockStampV1:
    return ProjectionBlockStampV1(
        source_id="corpus.runbook",
        block_id="summary",
        declared_generation=4,
        declared_coordinate=AcceptedCoordinate(
            git_oid="1" * 64,
            semantic_root="sha256:" + "2" * 64,
            generation_root="sha256:" + "3" * 64,
            compiler_digest="sha256:" + "4" * 64,
        ),
        backing=(
            ProjectionClaimBacking(
                identity=ArtifactIdentity(kind="Claim", name="CLM-existing"),
                statement_digest="sha256:" + "5" * 64,
            ),
        ),
        body_digest="sha256:" + "6" * 64,
    )


def test_cli_repin_passes_explicit_backings_and_canonical_query_parameters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []

    def repin(client: object, instance_id: str, **values: Any) -> ProjectionBlockStampV1:
        assert instance_id == "inst_projection"
        calls.append(values)
        return _stamp()

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: object())
    monkeypatch.setattr("cruxible_core.cli.commands.playbill.repin_projection_block", repin)

    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://projection.example.test",
            "--instance-id",
            "inst_projection",
            "block",
            "repin",
            "corpus.runbook",
            "summary",
            "--claim",
            "CLM-existing",
            "--query",
            "project.items",
            "--params",
            '{"status":"ready"}',
            "--workspace-root",
            str(tmp_path),
            "--evaluation-time",
            "2026-08-25T12:00:00Z",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["claims"] == ("CLM-existing",)
    assert calls[0]["queries"] == [("project.items", {"status": "ready"})]
    assert calls[0]["evaluation_time"] == datetime(2026, 8, 25, 12, tzinfo=UTC)
    assert '"declared_generation": 4' in result.output


@pytest.mark.parametrize("parameters", ['{"status": "ready"}', "[]", '{"x":1.5}'])
def test_cli_repin_refuses_noncanonical_query_parameter_objects(parameters: str) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "block",
            "repin",
            "corpus.runbook",
            "summary",
            "--query",
            "project.items",
            "--params",
            parameters,
        ],
    )

    assert result.exit_code == 1
    assert "not canonical JSON" in result.output


def test_cli_repin_rejects_query_parameter_count_mismatch() -> None:
    result = CliRunner().invoke(
        cli,
        [
            "block",
            "repin",
            "corpus.runbook",
            "summary",
            "--params",
            "{}",
        ],
    )

    assert result.exit_code == 1
    assert "once for each --query" in result.output


def test_cli_sync_passes_local_edit_and_path_controls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []

    def sync(client: object, instance_id: str, **values: Any) -> BlockSyncResult:
        assert instance_id == "inst_projection"
        calls.append(values)
        return BlockSyncResult(
            items=(
                BlockSyncItem(
                    path="corpus/runbook.md",
                    source_id="corpus.runbook",
                    block_id="summary",
                    outcome="unchanged",
                ),
            ),
            changed_file_count=0,
            would_change=False,
            has_refusals=False,
        )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: object())
    monkeypatch.setattr("cruxible_core.cli.commands.playbill.sync_projection_blocks", sync)
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://projection.example.test",
            "--instance-id",
            "inst_projection",
            "block",
            "sync",
            "corpus/runbook.md",
            "--workspace-root",
            str(tmp_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    (converging,) = calls
    # Without --check, sync converges rendered blocks at the caller's instant.
    assert isinstance(converging.pop("converge_at"), datetime)
    assert converging == {
        "workspace": str(tmp_path),
        "paths": ("corpus/runbook.md",),
        "all_files": False,
    }
    checked = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://projection.example.test",
            "--instance-id",
            "inst_projection",
            "block",
            "sync",
            "--all",
            "--check",
            "--workspace-root",
            str(tmp_path),
        ],
    )
    assert checked.exit_code == 0, checked.output
    assert calls[-1]["converge_at"] is None


def test_cli_sync_takes_no_detach_flag_and_detach_is_its_own_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, object]] = []

    def detach(client, instance_id, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs)
        return BlockDetachResult(
            status="would_detach",
            sync=BlockSyncResult(
                items=(
                    BlockSyncItem(
                        path="corpus/runbook.md",
                        source_id="corpus.runbook",
                        block_id="summary",
                        outcome="would_detach",
                    ),
                ),
                changed_file_count=0,
                would_change=True,
                has_refusals=False,
            ),
            coordinate=StateCoordinate.of("workspace_pages", {}),
        )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: object())
    monkeypatch.setattr("cruxible_core.cli.commands.playbill.detach_projection_pages", detach)
    base = [
        "--server-url",
        "https://projection.example.test",
        "--instance-id",
        "inst_projection",
        "block",
    ]
    refused = CliRunner().invoke(cli, [*base, "sync", "--all", "--detach", "x.md"])
    assert refused.exit_code == 2, refused.output

    invoked = CliRunner().invoke(
        cli,
        [*base, "detach", "corpus/runbook.md", "--workspace-root", str(tmp_path), "--dry-run"],
    )

    assert invoked.exit_code == 0, invoked.output
    assert "would_detach" in invoked.output
    assert "nothing was written" in invoked.output
    (call,) = calls
    assert call["dry_run"] is True and call["at"] is None
    assert call["pages"] == ((Path.cwd() / "corpus/runbook.md").resolve(),)


def test_block_sync_repair_lines_are_cli_commands_in_the_files_vocabulary() -> None:
    from cruxible_client.contracts.repairs import RepairOperation
    from cruxible_core.cli.commands.playbill import _block_repair_line

    repin = RepairOperation(
        operation="cruxible.block.repin",
        arguments={"source_id": "corpus.runbook", "block_id": "summary", "claim": ["CLM-1"]},
    )
    assert _block_repair_line(repin) == (
        "cruxible block repin corpus.runbook summary --claim CLM-1"
    )
    cleared = RepairOperation(
        operation="cruxible.block.repin",
        arguments={"source_id": "corpus.runbook", "block_id": "summary", "clear_claims": True},
    )
    assert _block_repair_line(cleared) == (
        "cruxible block repin corpus.runbook summary --clear-claims"
    )
    ambiguous = RepairOperation(
        operation="cruxible.block.repin",
        arguments={
            "source_id": "corpus.runbook",
            "block_id": "summary",
            "backing_candidates": ["sha256:aa", "sha256:bb"],
        },
    )
    assert _block_repair_line(ambiguous) == (
        "cruxible block repin corpus.runbook summary --backing DIGEST"
        "  (DIGEST: one of sha256:aa, sha256:bb)"
    )
    assert "source_id" not in _block_repair_line(repin)
    sweep = RepairOperation(operation="cruxible.block.sync", arguments={"all": True})
    assert _block_repair_line(sweep) == "cruxible block sync --all"
    detach = RepairOperation(
        operation="cruxible.block.detach", arguments={"paths": ["corpus/runbook.md"]}
    )
    assert _block_repair_line(detach) == "cruxible block detach corpus/runbook.md"
