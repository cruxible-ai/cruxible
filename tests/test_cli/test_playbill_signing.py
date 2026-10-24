"""PB-E CLI approval keeps private signing material entirely client-side."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_client.contracts.attestations import ApprovalAttestation
from cruxible_client.contracts.documents import DocumentAuthority, DocumentLifecycle, DocumentShell
from cruxible_core.cli.main import cli
from cruxible_core.service.authoring.documents import (
    service_propose_playbill_document,
    service_store_playbill_body,
    service_submit_playbill_approval,
)
from cruxible_core.service.proposals.review import service_prepare_playbill_approval
from cruxible_core.storage.cas import BodyAccessContext
from tests.test_service.test_playbill_documents import TIMESTAMP, _instance


@pytest.mark.parametrize("hold", (False, True))
@pytest.mark.parametrize(
    "recompiled_status", (None, "pending_review", "ready_to_activate", "already_live")
)
@pytest.mark.parametrize("output_json", (False, True))
def test_cli_approval_signs_exact_challenge_without_transmitting_key(
    tmp_path: Path,
    monkeypatch,
    hold: bool,
    recompiled_status: str | None,
    output_json: bool,
) -> None:
    instance, owner, reviewer = _instance(tmp_path)
    body = service_store_playbill_body(instance, content=b"# Review me\n")
    shell = DocumentShell(
        identity="document:signing-test",
        document_kind="design",
        title="Signing test",
        media_type="text/markdown",
        body_digest=body.digest,
        authority=DocumentAuthority(
            required_tier="graph_write",
        ),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    proposal = service_propose_playbill_document(
        instance,
        shell=shell,
        actor_id="owner",
        proposal_name="cli-signing",
        timestamp=TIMESTAMP,
    ).proposal
    assert proposal.candidate is not None
    proposal_id = proposal.admission.proposal_id
    submitted: list[dict[str, Any]] = []
    holds: list[bool] = []
    recompiled = (
        None
        if recompiled_status is None
        else contracts.ProposalRecompiled(
            status=recompiled_status,
            source_proposal_id=proposal_id,
            **(
                {}
                if recompiled_status == "already_live"
                else {
                    "proposal_id": "sha256:" + "a" * 64,
                    "candidate_digest": "sha256:" + "b" * 64,
                }
            ),
            # Identical members after a pending recompilation are a landing, which
            # the CLI signs next (``test_cli_approval_signs_the_landed_candidate``).
            members_identical=recompiled_status != "pending_review",
            accepted_coordinate=contracts.AcceptedCoordinate.from_internal(
                instance.accepted_coordinate()
            ),
        )
    )

    class StubClient:
        def resolve_proposal_selector(
            self, instance_id: str, selector: str
        ) -> contracts.ProposalSelectorResult:
            return contracts.ProposalSelectorResult(
                selector=selector,
                proposal_id=selector,
            )

        def prepare_approval(
            self,
            instance_id: str,
            selected_proposal_id: str,
            *,
            signer_id: str,
            include_body: bool,
        ) -> contracts.ApprovalChallenge:
            assert instance_id == "inst_cli"
            challenge = service_prepare_playbill_approval(
                instance,
                proposal_id=selected_proposal_id,
                signer_id=signer_id,
                access=BodyAccessContext(
                    principal_id=signer_id,
                    can_read_body=include_body,
                ),
            )
            return contracts.ApprovalChallenge.model_validate(challenge.model_dump(mode="json"))

        def submit_approval(
            self,
            instance_id: str,
            selected_proposal_id: str,
            *,
            attestation: dict[str, Any],
            hold: bool = False,
        ) -> contracts.ApprovalReceipt | contracts.ProposalRecompiled:
            assert instance_id == "inst_cli"
            submitted.append(attestation)
            holds.append(hold)
            if recompiled is not None:
                return recompiled
            receipt = service_submit_playbill_approval(
                instance,
                proposal_id=selected_proposal_id,
                attestation=ApprovalAttestation.model_validate(attestation),
                authenticated_submitter="reviewer",
                activate_when_complete=not hold,
                may_activate=True,
            )
            return contracts.ApprovalReceipt.model_validate(receipt.model_dump(mode="json"))

    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_cli",
            "proposal",
            "approve",
            proposal_id,
            "--signer-id",
            "reviewer",
            "--key",
            str(reviewer.private_key_path),
            "--yes",
            *(("--hold",) if hold else ()),
            *(("--json",) if output_json else ()),
        ],
    )

    assert result.exit_code == 0, result.output
    assert holds == [hold]
    if recompiled is not None:
        if output_json:
            assert json.loads(result.stdout) == recompiled.model_dump(mode="json")
        elif recompiled_status == "already_live":
            # Nothing was proposed, so there is no successor to name.
            assert result.stdout.splitlines()[-1] == (
                f"{proposal_id} is already live at the head: every change it carried is "
                "accepted; closed as answered, nothing to review."
            )
            assert "None" not in result.stdout
        else:
            operation = "review" if recompiled_status == "pending_review" else "activate"
            assert result.stdout.splitlines()[-1] == (
                f"Next: cruxible proposal {operation} {recompiled.proposal_id}"
            )
        return
    if not output_json:
        # --yes prints one counts line before signing, not the approval summary.
        counts = result.stdout.splitlines()[0]
        assert counts.startswith(f"Signing {proposal.candidate.candidate_digest} ({proposal_id}): ")
        assert " change" in counts
        assert "Required approvals" not in result.stdout
        assert "Approved " in result.stdout
        return
    receipt = json.loads(result.stdout)
    # The reviewer's signature completes the candidate: it activates unless held.
    if hold:
        assert receipt["activation"] is None and receipt["not_activated"] == "held"
    else:
        assert receipt["not_activated"] is None
        assert receipt["activation"]["status"] == "accepted"
        assert receipt["activation"]["activated_by"] == "reviewer"
    assert len(submitted) == 1
    assert submitted[0]["payload_digest"] == proposal.candidate.candidate_digest
    assert submitted[0]["signing_semantic_root"] == (
        proposal.candidate.candidate.parent_semantic_root
    )
    serialized = json.dumps(submitted[0])
    assert str(reviewer.private_key_path) not in serialized
    assert "private_key" not in serialized


def test_cli_approval_signs_the_landed_candidate(tmp_path: Path, monkeypatch) -> None:
    """Nothing it touches changed since its base: one approve lands it and activates it.

    The daemon lands the same change set at the head as a new candidate; an
    approval signs exactly one candidate, so the CLI signs the landed one next.
    """

    from cruxible_core.service.proposals.proposals import _proposal_result
    from tests.core_support._support import client_material
    from tests.core_support._write_support import KIND, seed_write_surface
    from tests.test_ledger.test_activation import _sign
    from tests.test_service.test_write_verbs import _set, _write

    instance, _owner = seed_write_surface(tmp_path, self_approval=False)
    reviewer = client_material(tmp_path, instance)
    pending = _write(instance, _set(f"{KIND}/wi-1", "title", "behind"))
    moving = _write(instance, _set(f"{KIND}/wi-2", "title", "unrelated"))
    assert pending.proposal is not None and moving.proposal is not None
    source = _proposal_result(instance, pending.proposal.proposal_id)
    moved = _proposal_result(instance, moving.proposal.proposal_id)
    assert source.candidate is not None and moved.candidate is not None
    service_submit_playbill_approval(
        instance,
        proposal_id=moved.admission.proposal_id,
        attestation=_sign(
            reviewer,
            moved.candidate.candidate_digest,
            moved.candidate.candidate.parent_semantic_root,
        ).attestation,
        authenticated_submitter="reviewer",
        activate_when_complete=True,
        may_activate=True,
    )
    submitted: list[tuple[str, dict[str, Any]]] = []

    class StubClient:
        def resolve_proposal_selector(self, instance_id: str, selector: str):  # type: ignore[no-untyped-def]
            return contracts.ProposalSelectorResult(selector=selector, proposal_id=selector)

        def prepare_approval(self, instance_id, selected, *, signer_id, include_body=False):  # type: ignore[no-untyped-def]
            challenge = service_prepare_playbill_approval(
                instance,
                proposal_id=selected,
                signer_id=signer_id,
                access=BodyAccessContext(principal_id=signer_id, can_read_body=include_body),
            )
            return contracts.ApprovalChallenge.model_validate(challenge.model_dump(mode="json"))

        def submit_approval(self, instance_id, selected, *, attestation, hold=False):  # type: ignore[no-untyped-def]
            submitted.append((selected, attestation))
            result = service_submit_playbill_approval(
                instance,
                proposal_id=selected,
                attestation=ApprovalAttestation.model_validate(attestation),
                authenticated_submitter="reviewer",
                activate_when_complete=not hold,
                may_activate=True,
            )
            if isinstance(result, (contracts.ProposalRecompiled, contracts.ActivationReceipt)):
                return result
            return contracts.ApprovalReceipt.model_validate(result.model_dump(mode="json"))

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    head_root = instance.accepted_coordinate().semantic_root
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_cli",
            "proposal",
            "approve",
            source.admission.proposal_id,
            "--signer-id",
            "reviewer",
            "--key",
            str(reviewer.private_key_path),
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    ((first_id, first), (landed_id, landed)) = submitted
    assert first_id == source.admission.proposal_id
    assert first["payload_digest"] == source.candidate.candidate_digest
    assert landed_id != first_id and landed["payload_digest"] != first["payload_digest"]
    # The landed candidate sits on the head the approval met.
    assert landed["signing_semantic_root"] == head_root
    assert f"Landed at the head as {landed_id}" in result.stdout
    assert "Activated at" in result.stdout


def test_cli_missing_signer_never_falls_back_to_daemon(
    tmp_path: Path,
    monkeypatch,
) -> None:
    instance, _owner, _reviewer = _instance(tmp_path)
    body = service_store_playbill_body(instance, content=b"# Missing key\n")
    proposal = service_propose_playbill_document(
        instance,
        shell=DocumentShell(
            identity="document:missing-key",
            document_kind="design",
            title="Missing key",
            media_type="text/markdown",
            body_digest=body.digest,
            authority=DocumentAuthority(
                required_tier="graph_write",
            ),
            governance_scope=("project:playbill",),
            lifecycle=DocumentLifecycle(revision=1),
        ),
        actor_id="owner",
        proposal_name="missing-key",
        timestamp=TIMESTAMP,
    ).proposal
    submitted = False

    class StubClient:
        def resolve_proposal_selector(
            self, instance_id: str, selector: str
        ) -> contracts.ProposalSelectorResult:
            return contracts.ProposalSelectorResult(
                selector=selector,
                proposal_id=selector,
            )

        def prepare_approval(
            self,
            _instance_id: str,
            proposal_id: str,
            *,
            signer_id: str,
            include_body: bool,
        ) -> contracts.ApprovalChallenge:
            challenge = service_prepare_playbill_approval(
                instance,
                proposal_id=proposal_id,
                signer_id=signer_id,
                access=BodyAccessContext(
                    principal_id=signer_id,
                    can_read_body=include_body,
                ),
            )
            return contracts.ApprovalChallenge.model_validate(challenge.model_dump(mode="json"))

        def submit_approval(self, *args: Any, **kwargs: Any) -> Any:
            nonlocal submitted
            submitted = True

    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    missing = tmp_path / "missing-private-key"
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_cli",
            "proposal",
            "approve",
            proposal.admission.proposal_id,
            "--signer-id",
            "owner",
            "--key",
            str(missing),
            "--yes",
        ],
    )

    # The review challenge may be fetched, but a missing client key cannot
    # silently turn into a daemon/bearer signature or an attestation submit.
    assert result.exit_code != 0
    assert submitted is False
    assert instance.accepted_history()[-1].sequence == 0


def test_cli_approval_without_a_terminal_refuses_before_contacting_the_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: (_ for _ in ()).throw(AssertionError("no prompt, no daemon call")),
    )
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://playbill.invalid",
            "--instance-id",
            "inst_cli",
            "proposal",
            "approve",
            "PRP-0123456789ab",
            "--signer-id",
            "reviewer",
        ],
    )

    assert result.exit_code == 2
    assert "Aborted" not in result.output
    assert (
        "review it with `cruxible proposal review PRP-0123456789ab`, then rerun with --yes"
        in result.stderr
    )
