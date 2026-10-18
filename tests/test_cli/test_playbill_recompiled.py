"""Recompiled proposals name the next operation on their new ID."""

import json

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_client.contracts.recompilation import RecompiledMemberChange
from cruxible_core.cli.main import cli
from tests.test_cli.test_playbill_and_activate import COORDINATE, PROPOSAL_ID


@pytest.mark.parametrize("status", ("pending_review", "ready_to_activate"))
@pytest.mark.parametrize("output_json", (False, True))
def test_activate_recompiled_next_step(monkeypatch, status, output_json):
    receipt = contracts.ProposalRecompiled(
        status=status,
        source_proposal_id=PROPOSAL_ID,
        proposal_id="sha256:" + "a" * 64,
        candidate_digest="sha256:" + "b" * 64,
        members_identical=False,
        changed_members=(RecompiledMemberChange(path="claims/changed.json", change="replaced"),),
        changed_members_truncated=True,
        accepted_coordinate=COORDINATE,
    )

    class Client:
        def resolve_proposal_selector(self, instance_id, selector):
            return contracts.ProposalSelectorResult(selector=selector, proposal_id=selector)

        def activate_proposal(self, instance_id, proposal_id):
            return receipt

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", Client)
    result = CliRunner().invoke(cli, [
        "--server-url", "https://example.invalid", "--instance-id", "inst_cli",
        "proposal", "activate", PROPOSAL_ID, *(("--json",) if output_json else ()),
    ])
    assert result.exit_code == 0, result.output
    if output_json:
        assert json.loads(result.stdout) == receipt.model_dump(mode="json")
    else:
        assert "replaced: claims/changed.json" in result.stdout
        assert "Changed member paths truncated" in result.stdout
        operation = "review" if status == "pending_review" else "activate"
        assert result.stdout.splitlines()[-1] == (
            f"Next: cruxible proposal {operation} {receipt.proposal_id}"
        )
