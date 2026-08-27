"""PB-E CLI approval keeps private signing material entirely client-side."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_client.contracts.attestations import ApprovalAttestation
from cruxible_client.contracts.documents import DocumentAuthority, DocumentLifecycle, DocumentShell
from cruxible_core.cli.main import cli
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.service.documents import (
    service_propose_playbill_document,
    service_store_playbill_body,
    service_submit_playbill_approval,
)
from cruxible_core.playbill.service.review import service_prepare_playbill_approval
from tests.test_service.test_playbill_documents import TIMESTAMP, _instance


def test_cli_approval_signs_exact_challenge_without_transmitting_key(
    tmp_path: Path,
    monkeypatch,
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
            approval_roles=("owner",),
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

    class StubClient:
        def prepare_playbill_approval(
            self,
            instance_id: str,
            selected_proposal_id: str,
            *,
            signer_id: str,
            include_body: bool,
        ) -> contracts.PlaybillApprovalChallenge:
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
            return contracts.PlaybillApprovalChallenge.model_validate(
                challenge.model_dump(mode="json")
            )

        def submit_playbill_approval(
            self,
            instance_id: str,
            selected_proposal_id: str,
            *,
            attestation: dict[str, Any],
        ) -> contracts.PlaybillApprovalReceipt:
            assert instance_id == "inst_cli"
            submitted.append(attestation)
            receipt = service_submit_playbill_approval(
                instance,
                proposal_id=selected_proposal_id,
                attestation=ApprovalAttestation.model_validate(attestation),
                authenticated_submitter="reviewer",
            )
            return contracts.PlaybillApprovalReceipt.model_validate(receipt.model_dump(mode="json"))

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
            "playbill",
            "proposal",
            "approve",
            proposal_id,
            "--signer-id",
            "reviewer",
            "--key",
            str(reviewer.private_key_path),
            "--yes",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(submitted) == 1
    assert submitted[0]["payload_digest"] == proposal.candidate.candidate_digest
    assert submitted[0]["signing_semantic_root"] == (
        proposal.candidate.candidate.parent_semantic_root
    )
    serialized = json.dumps(submitted[0])
    assert str(reviewer.private_key_path) not in serialized
    assert "private_key" not in serialized


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
                approval_roles=("owner",),
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
        def prepare_playbill_approval(
            self,
            _instance_id: str,
            proposal_id: str,
            *,
            signer_id: str,
            include_body: bool,
        ) -> contracts.PlaybillApprovalChallenge:
            challenge = service_prepare_playbill_approval(
                instance,
                proposal_id=proposal_id,
                signer_id=signer_id,
                access=BodyAccessContext(
                    principal_id=signer_id,
                    can_read_body=include_body,
                ),
            )
            return contracts.PlaybillApprovalChallenge.model_validate(
                challenge.model_dump(mode="json")
            )

        def submit_playbill_approval(self, *args: Any, **kwargs: Any) -> Any:
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
            "playbill",
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
