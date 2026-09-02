"""Detached proposal review-worktree CLI plumbing."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli

PROPOSAL_KEY = "a" * 64
PROPOSAL_ID = f"sha256:{PROPOSAL_KEY}"


def _inspection() -> contracts.PlaybillProposalInspection:
    return contracts.PlaybillProposalInspection(
        proposal={"admission": {"proposal_id": PROPOSAL_ID}},
        accepted_coordinate=contracts.PlaybillAcceptedCoordinate(
            git_oid="1" * 40,
            semantic_root="sha256:" + "2" * 64,
            generation_root="sha256:" + "3" * 64,
            compiler_digest="sha256:" + "4" * 64,
        ),
        workspace_advertisement={
            "status": "updated",
            "workspace_path": "workspace",
            "advertised_refs": [f"refs/remotes/playbill/proposals/{PROPOSAL_KEY}"],
        },
    )


def test_review_open_resolves_canonical_id_and_close_stays_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Path, str]] = []

    class StubClient:
        def inspect_playbill_proposal(
            self, instance_id: str, proposal_id: str
        ) -> contracts.PlaybillProposalInspection:
            assert instance_id == "inst_review"
            assert proposal_id == "short-id"
            return _inspection()

    opened = tmp_path / ".playbill" / "review" / PROPOSAL_KEY

    def open_worktree(*, workspace_path: Path, proposal_id: str) -> Path:
        calls.append(("open", workspace_path, proposal_id))
        return opened

    def close_worktree(*, workspace_path: Path, proposal_id: str) -> Path:
        calls.append(("close", workspace_path, proposal_id))
        return opened

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.open_proposal_review_worktree",
        open_worktree,
    )
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.close_proposal_review_worktree",
        close_worktree,
    )
    common = [
        "--server-url",
        "https://review.example.test",
        "--instance-id",
        "inst_review",
        "playbill",
        "review",
    ]
    opened_result = CliRunner().invoke(
        cli,
        [*common, "open", "short-id", "--workspace-root", str(tmp_path), "--json"],
    )
    closed_result = CliRunner().invoke(
        cli,
        [*common, "close", PROPOSAL_ID, "--workspace-root", str(tmp_path), "--json"],
    )

    assert opened_result.exit_code == 0, opened_result.output
    assert closed_result.exit_code == 0, closed_result.output
    assert calls == [
        ("open", tmp_path, PROPOSAL_ID),
        ("close", tmp_path, PROPOSAL_ID),
    ]
    assert '"detached": true' in opened_result.output
    assert '"closed": true' in closed_result.output
