"""The declared-block CLI is a client-only, canonically parameterized adapter."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    PlaybillBlockSyncItemV1,
    PlaybillBlockSyncResultV1,
)
from cruxible_client.contracts.declared_blocks import (
    ProjectionBlockStampV1,
    ProjectionClaimBackingV1,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.cli.main import cli
from cruxible_core.deprecation import BLOCK_SYNC_DISCARD_LOCAL_FLAG


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
            ProjectionClaimBackingV1(
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
            "playbill",
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
            "playbill",
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
            "playbill",
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

    def sync(client: object, instance_id: str, **values: Any) -> PlaybillBlockSyncResultV1:
        assert instance_id == "inst_projection"
        calls.append(values)
        return PlaybillBlockSyncResultV1(
            items=(
                PlaybillBlockSyncItemV1(
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
            "playbill",
            "block",
            "sync",
            "corpus/runbook.md",
            "--accept-local",
            "corpus/runbook.md",
            "--workspace-root",
            str(tmp_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "workspace": str(tmp_path),
            "paths": ("corpus/runbook.md",),
            "all_sources": False,
            "check": False,
            "detach_paths": (),
            "accept_local_paths": ("corpus/runbook.md",),
        }
    ]


def test_cli_sync_accepts_the_deprecated_discard_local_spelling_with_a_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The flag is renamed, not removed: one release of structured warning."""

    calls: list[dict[str, Any]] = []

    def sync(client: object, instance_id: str, **values: Any) -> PlaybillBlockSyncResultV1:
        calls.append(values)
        return PlaybillBlockSyncResultV1(
            items=(),
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
            "playbill",
            "block",
            "sync",
            "corpus/runbook.md",
            "--discard-local",
            "corpus/runbook.md",
            "--workspace-root",
            str(tmp_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "workspace": str(tmp_path),
            "paths": ("corpus/runbook.md",),
            "all_sources": False,
            "check": False,
            "detach_paths": (),
            "accept_local_paths": ("corpus/runbook.md",),
        }
    ]
    assert (
        json.loads(
            next(
                line for line in result.output.splitlines() if line.startswith("Deprecation: ")
            ).removeprefix("Deprecation: ")
        )
        == BLOCK_SYNC_DISCARD_LOCAL_FLAG.as_dict()
    )


def test_cli_sync_check_exits_nonzero_when_safe_bytes_would_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = PlaybillBlockSyncResultV1(
        items=(
            PlaybillBlockSyncItemV1(
                path="corpus/runbook.md",
                source_id="corpus.runbook",
                block_id="summary",
                outcome="would_sync",
            ),
        ),
        changed_file_count=0,
        would_change=True,
        has_refusals=False,
    )
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: object())
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.sync_projection_blocks",
        lambda *_args, **_kwargs: result,
    )

    invoked = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://projection.example.test",
            "--instance-id",
            "inst_projection",
            "playbill",
            "block",
            "sync",
            "--all",
            "--check",
        ],
    )

    assert invoked.exit_code == 1
    assert "would_sync" in invoked.output
