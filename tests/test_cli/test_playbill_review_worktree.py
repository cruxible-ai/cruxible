"""Detached proposal review-worktree CLI plumbing."""

from __future__ import annotations

import shlex
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


def test_review_open_not_attached_names_a_runnable_local_socket_repair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class StubClient:
        def inspect_playbill_proposal(
            self, instance_id: str, proposal_id: str
        ) -> contracts.PlaybillProposalInspection:
            return _inspection().model_copy(
                update={
                    "workspace_advertisement": contracts.PlaybillWorkspaceAdvertisement(
                        status="not_attached",
                        workspace_path=None,
                    )
                }
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    server_socket = tmp_path / "daemon.sock"

    result = CliRunner().invoke(
        cli,
        [
            "--server-socket",
            str(server_socket),
            "--instance-id",
            "inst_review",
            "playbill",
            "review",
            "open",
            "short-id",
            "--workspace-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 1
    assert "review_workspace_not_attached" in result.output
    assert f"cruxible --server-socket {server_socket}" in result.output
    assert f"playbill host create --workspace {tmp_path}" in result.output
    assert "SOCKET" not in result.output and "WORKSPACE" not in result.output


def test_review_open_not_attached_url_branch_keeps_a_valid_local_repair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class StubClient:
        def inspect_playbill_proposal(
            self, instance_id: str, proposal_id: str
        ) -> contracts.PlaybillProposalInspection:
            return _inspection().model_copy(
                update={
                    "workspace_advertisement": contracts.PlaybillWorkspaceAdvertisement(
                        status="not_attached",
                        workspace_path=None,
                    )
                }
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    workspace = tmp_path / "review workspace"
    workspace.mkdir()
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://review.example.test",
            "--instance-id",
            "inst_review",
            "playbill",
            "review",
            "open",
            "short-id",
            "--workspace-root",
            str(workspace),
        ],
    )

    assert result.exit_code == 1
    repair = result.output.split("repair: ", maxsplit=1)[1].strip()
    assert shlex.split(repair) == [
        "cruxible",
        "playbill",
        "host",
        "create",
        "--workspace",
        str(workspace.resolve()),
    ]
