"""Authoring submit lands in the daemon and explains every stop."""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli

COORDINATE = contracts.AcceptedCoordinate(
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
    "authoring",
]


class _SubmitClient:
    """Records every activation attempt so a test can assert there were none."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.activations: list[str] = []

    def submit_authoring_intent(
        self, instance_id: str, intent_id: str, *, accept: str = "if_allowed"
    ) -> contracts.AuthoringSubmitResultRecord:
        self.accept = accept
        intent: dict[str, object] = {"intent_id": intent_id}
        if self.state == "preflight_refused":
            intent["last_preflight"] = {
                "frontier": {
                    "diagnostics": [
                        {
                            "code": "cruxible.claim.subject_unresolved",
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
        return contracts.AuthoringSubmitResultRecord(
            outcome=contracts.AuthoringProposed(
                proposal_id=PROPOSAL_ID,
                reason="independent_approval_required"
                if self.state == "awaiting_external_approval"
                else "not_ready_to_activate",
                message="not activated: the candidate needs an external approval"
                if self.state == "awaiting_external_approval"
                else f"not activated: the candidate is {self.state}, not ready_to_activate",
                next=f"cruxible proposal approve {PROPOSAL_ID}"
                if self.state == "awaiting_external_approval"
                else None,
            ),
            intent=intent,
            status=contracts.CandidateStatusRecord(
                state=self.state,
                proposal_id=PROPOSAL_ID,
                current_accepted_coordinate=COORDINATE,
            ),
        )

    def activate_proposal(self, instance_id: str, proposal_id: str, **_: Any) -> object:
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
def test_default_submit_stops_and_names_the_step_when_the_candidate_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    marker: str,
) -> None:
    """A candidate carrying approval requirements is never activated."""
    client = _SubmitClient(state)
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)

    result = CliRunner().invoke(cli, [*COMMON, "submit", "--intent-id", INTENT_ID])

    assert result.exit_code == 0, result.output
    assert client.activations == []
    # The CLI prints a target banner before its JSON document.
    payload = json.loads(result.output[result.output.index("{") :])
    assert "activation" not in payload
    assert marker in payload["outcome"]["message"]


def test_default_submit_names_the_approve_command_in_brief_when_it_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stopping is only useful if the caller is told what to run instead."""
    client = _SubmitClient("awaiting_external_approval")
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)

    result = CliRunner().invoke(cli, [*COMMON, "submit", "--intent-id", INTENT_ID, "--brief"])

    assert result.exit_code == 0, result.output
    assert client.activations == []
    assert f"cruxible proposal approve {PROPOSAL_ID}" in result.output


def test_refused_brief_prints_the_typed_reason_on_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _SubmitClient("preflight_refused")
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)

    result = CliRunner().invoke(cli, [*COMMON, "submit", "--intent-id", INTENT_ID, "--brief"])

    assert result.exit_code == 0, result.output
    assert (
        "reason: cruxible.claim.subject_unresolved: The requested Subject does not exist."
        in result.output
    )
    assert (
        "blocked claim_acceptance by subject_resolution: "
        "The claim cannot be checked until its subject resolves."
    ) in result.output
    assert result.output.count("reason:") == 1


def test_default_submit_brief_prints_the_accepted_coordinate_and_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _SubmitClient("ready_to_activate")

    def submit(
        instance_id: str, intent_id: str, *, accept: str
    ) -> contracts.AuthoringSubmitResultRecord:
        assert accept == "if_allowed"
        return contracts.AuthoringSubmitResultRecord(
            intent={"intent_id": intent_id},
            status=contracts.CandidateStatusRecord(
                state="accepted",
                proposal_id=PROPOSAL_ID,
                current_accepted_coordinate=COORDINATE,
                accepted_generation=COORDINATE,
            ),
            outcome=contracts.AuthoringLanded(
                proposal_id=PROPOSAL_ID, accepted_coordinate=COORDINATE
            ),
        )

    client.submit_authoring_intent = submit  # type: ignore[method-assign]
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    result = CliRunner().invoke(
        cli,
        [*COMMON, "submit", "--intent-id", INTENT_ID, "--brief"],
    )

    assert result.exit_code == 0, result.output
    # Activation is the daemon act alone: one call, no floor or block step.
    assert client.activations == []
    assert "outcome: accepted" in result.output
    assert f"coordinate: {COORDINATE.git_oid}" in result.output
    assert f"proposal: {PROPOSAL_ID}" in result.output


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("accepted", ["accepted (111111111111) by owner", "next: cruxible next"]),
        ("lost_cas", ["not accepted (lost_cas)", f"next: cruxible proposal readmit {PROPOSAL_ID}"]),
    ],
)
def test_proposal_activate_prints_the_outcome_and_next_step_as_text(
    monkeypatch: pytest.MonkeyPatch, status: str, expected: list[str]
) -> None:
    """`proposal activate` reads like a write: the outcome and the next step, not raw JSON."""

    receipt = contracts.ActivationReceipt(
        proposal_id=PROPOSAL_ID,
        activated_by="owner",
        status=status,  # type: ignore[arg-type]
        accepted_coordinate=COORDINATE if status == "accepted" else None,
        workspace_advertisement={"status": "not_attached", "workspace_path": None},
    )

    class _ActivateClient:
        def resolve_proposal_selector(self, instance_id: str, selector: str) -> object:
            from types import SimpleNamespace

            return SimpleNamespace(proposal_id=PROPOSAL_ID)

        def activate_proposal(self, instance_id: str, proposal_id: str) -> object:
            return receipt

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: _ActivateClient())
    base = [*COMMON[:-1], "proposal", "activate", PROPOSAL_ID]
    result = CliRunner().invoke(cli, base)
    assert result.exit_code == 0, result.output
    for line in expected:
        assert line in result.output
    assert "playbill-activation-receipt-v1" not in result.output

    as_json = CliRunner().invoke(cli, [*base, "--json"])
    assert json.loads(as_json.stdout)["status"] == status


def test_submit_no_accept_passes_the_explicit_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _SubmitClient("ready_to_activate")
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    result = CliRunner().invoke(cli, [*COMMON, "submit", "--intent-id", INTENT_ID, "--no-accept"])
    assert result.exit_code == 0, result.output
    assert client.accept == "never"
    assert client.activations == []
