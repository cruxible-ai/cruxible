"""`--and-activate` never half-activates, and says why when it stops.

The convenience flag exists so one command replaces two. The thing it must not
do is turn a candidate that needs an approval into a partly-applied change: an
unactivated candidate is recoverable, a half-activated one is a mess. So the
flag activates only a candidate that needs nothing further, and every other
state returns the submit result plus a typed note naming the next step.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli

COORDINATE = contracts.PlaybillAcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)
INTENT_ID = "AIT-" + "5" * 32
PROPOSAL_ID = "sha256:" + "7" * 64

COMMON = [
    "--server-url",
    "https://authoring.example.test",
    "--instance-id",
    "inst_authoring",
    "playbill",
    "authoring",
]


class _SubmitClient:
    """Records every activation attempt so a test can assert there were none."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.activations: list[str] = []

    def submit_playbill_authoring_intent(
        self, instance_id: str, intent_id: str
    ) -> contracts.PlaybillAuthoringSubmitResult:
        intent: dict[str, object] = {"intent_id": intent_id}
        if self.state == "preflight_refused":
            intent["last_preflight"] = {
                "frontier": {
                    "diagnostics": [
                        {
                            "code": "playbill.claim.subject_unresolved",
                            "message": "The requested Subject\n does not exist.",
                        }
                    ],
                    "blocked_checks": [
                        {
                            "check": "claim_acceptance",
                            "blocked_by": ["subject_resolution"],
                            "reason": "The claim cannot be checked\n until its subject resolves.",
                        }
                    ],
                }
            }
        return contracts.PlaybillAuthoringSubmitResult(
            intent=intent,
            status=contracts.PlaybillCandidateStatus(
                state=self.state,
                proposal_id=PROPOSAL_ID,
                current_accepted_coordinate=COORDINATE,
            ),
        )

    def activate_playbill_proposal(self, instance_id: str, proposal_id: str, **_: Any) -> object:
        self.activations.append(proposal_id)
        raise AssertionError("this candidate must never be activated")


@pytest.mark.parametrize(
    ("state", "marker"),
    [
        ("awaiting_external_approval", "needs an external approval"),
        ("preflight_refused", "not ready_to_activate"),
        ("draft", "not ready_to_activate"),
    ],
)
def test_and_activate_stops_and_names_the_step_when_the_candidate_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    marker: str,
) -> None:
    """A candidate carrying approval requirements is never activated."""
    client = _SubmitClient(state)
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)

    result = CliRunner().invoke(cli, [*COMMON, "submit", INTENT_ID, "--and-activate", "--json"])

    assert result.exit_code == 0, result.output
    assert client.activations == []
    # The CLI prints a target banner before its JSON document.
    payload = json.loads(result.output[result.output.index("{") :])
    assert "activation" not in payload
    assert marker in payload["activation_note"]


def test_and_activate_names_the_approve_command_in_brief_when_it_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stopping is only useful if the caller is told what to run instead."""
    client = _SubmitClient("awaiting_external_approval")
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)

    result = CliRunner().invoke(cli, [*COMMON, "submit", INTENT_ID, "--and-activate", "--brief"])

    assert result.exit_code == 0, result.output
    assert client.activations == []
    assert f"cruxible playbill proposal approve {PROPOSAL_ID}" in result.output


def test_refused_brief_prints_the_typed_reason_on_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _SubmitClient("preflight_refused")
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)

    result = CliRunner().invoke(cli, [*COMMON, "submit", INTENT_ID, "--brief"])

    assert result.exit_code == 0, result.output
    assert (
        "reason: playbill.claim.subject_unresolved: The requested Subject does not exist."
        in result.output
    )
    assert (
        "blocked claim_acceptance by subject_resolution: "
        "The claim cannot be checked until its subject resolves."
    ) in result.output
    assert result.output.count("reason:") == 1


def test_and_activate_brief_prints_the_accepted_coordinate_and_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _SubmitClient("ready_to_activate")
    activation = contracts.PlaybillWorkspaceActivationResult(
        proposal_id=PROPOSAL_ID,
        activated_by="owner",
        status="accepted",
        accepted_coordinate=COORDINATE,
        workspace_advertisement={"status": "not_attached", "workspace_path": None},
        floor_refresh=contracts.PlaybillFloorRefreshResult(status="not_configured"),
    )
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.activate_with_workspace_refresh",
        lambda *_args, **_kwargs: activation,
    )

    result = CliRunner().invoke(
        cli,
        [*COMMON, "submit", INTENT_ID, "--and-activate", "--brief"],
    )

    assert result.exit_code == 0, result.output
    assert f"coordinate: {COORDINATE.git_oid}" in result.output
    assert "receipt: playbill-activation-receipt-v1" in result.output
