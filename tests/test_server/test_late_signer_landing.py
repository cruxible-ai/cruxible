"""A reviewer registered after a proposal's base approves it by landing it at the head.

Its signature is checked against the registry of the coordinate it signs at, the
candidate's parent, where it is absent; the approval challenge lands the proposal
at the head first, by the same path its author's readmit takes, and the late
reviewer signs the landed candidate. Driven over HTTP with a real principal
registered after the proposals were written, under independent approval.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client.authoring.examples import authoring_example
from cruxible_client.authoring.signing import LocalEd25519ApprovalSigner
from cruxible_client.contracts.attestations import ApprovalStatement
from cruxible_client.contracts.principals import PRINCIPAL_ID_HEADER
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.governance.keys import generate_client_principal_key
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.service.authoring.documents import (
    service_inspect_playbill_proposal,
    service_propose_playbill_document,
    service_store_playbill_body,
)
from tests.test_proposals.test_proposals import _shell
from tests.test_server.conftest import _playbill_http

LATE = {PRINCIPAL_ID_HEADER: "late"}


@pytest.fixture
def independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, str, Path]]:
    yield from _playbill_http(tmp_path, monkeypatch, require_independent_approval=True)


def _transport(http: TestClient) -> CruxibleClient:
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    return transport


def sign_and_submit(
    http: TestClient,
    instance_id: str,
    proposal_id: str,
    *,
    signer: str,
    key: Path,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    challenge = http.post(
        f"/api/v1/{instance_id}/proposals/{proposal_id}/approval-challenge",
        json={"signer_id": signer},
        headers=headers or {},
    )
    assert challenge.status_code == 200, challenge.text
    body = challenge.json()
    attestation = LocalEd25519ApprovalSigner.open(
        signer_id=signer,
        private_key_path=key,
        expected_public_key=body["signer_principal"]["public_key"],
        forbidden_roots=(),
    ).sign(ApprovalStatement.model_validate(body["statement"]))
    submitted = http.post(
        f"/api/v1/{instance_id}/proposals/{body['proposal_id']}/approvals",
        json={"attestation": attestation.model_dump(mode="json")},
        headers=headers or {},
    )
    assert submitted.status_code == 200, submitted.text
    return {"challenge_proposal_id": body["proposal_id"], **submitted.json()}


def register_late(
    http: TestClient, instance_id: str, tmp_path: Path, reviewer_key: Path, owner_key: Path
) -> Path:
    """Register principal ``late`` now: proposed by operator, signed by it and reviewer."""

    assert owner_key.is_file()
    late = generate_client_principal_key(
        tmp_path / f"late-custody-{instance_id}",
        principal_id="late",
        kind="ordinary",
        forbidden_roots=(),
    )
    proposed = playbill_api.playbill_propose_principal_change(
        instance_id, principal=late.principal, proposal_name="register-late"
    )
    proposal_id = proposed.proposal["admission"]["proposal_id"]
    # A principal change needs its proposer's signature and an independent one.
    sign_and_submit(http, instance_id, proposal_id, signer="operator", key=owner_key)
    receipt = sign_and_submit(http, instance_id, proposal_id, signer="reviewer", key=reviewer_key)
    assert receipt["activation"]["status"] == "accepted", receipt  # type: ignore[index]
    return late.private_key_path


def test_a_late_reviewer_lands_and_approves_a_change_set_and_a_document(
    independent: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    """The verification run's case: an authoring change set holding a subject kind
    and its ClaimTypes refused `readmit_requires_resubmission` ("a generated
    dependency-closure migration"). It recompiles at the head from its retained
    input, as its author's readmit does; a Document lands by its bytes."""

    http, instance_id, reviewer_key = independent
    transport = _transport(http)
    compiled = transport.compile_authoring_input(
        instance_id, input=authoring_example("subject-kind").model_dump(mode="json")
    )
    assert compiled.verdict == "passed", compiled.frontier
    submitted = transport.submit_authoring_intent(
        instance_id, str(compiled.certificate.intent_id), accept="never"
    )
    change_set = submitted.status.proposal_id
    assert change_set is not None
    instance = get_playbill_manager().get(instance_id)
    body = service_store_playbill_body(instance, content=b"# Late review\n").digest
    document = service_propose_playbill_document(
        instance,
        shell=_shell(body),
        actor_id="operator",
        proposal_name="late-review-document",
        timestamp="2026-11-16T12:00:00.000000Z",
    ).proposal.admission.proposal_id

    late_key = register_late(
        http, instance_id, tmp_path, reviewer_key, tmp_path / "owner-custody" / "operator.ed25519"
    )

    for source in (change_set, document):
        receipt = sign_and_submit(
            http, instance_id, source, signer="late", key=late_key, headers=LATE
        )
        landed = receipt["challenge_proposal_id"]
        assert landed != source
        assert receipt["readmits"] == source, receipt
        assert receipt["activation"]["status"] == "accepted", receipt  # type: ignore[index]
        # The landed proposal is the author's, signed afresh by the late reviewer.
        successor = service_inspect_playbill_proposal(instance, proposal_id=str(landed))
        assert successor.proposal.admission.actor_id == "operator"  # type: ignore[union-attr]


def test_a_byte_landing_the_head_refuses_publishes_nothing(
    independent: tuple[TestClient, str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A landing by bytes (a Document) used to submit straight away, and a submission
    publishes even a refused evaluation: a refused challenge left a new proposal
    behind. It now previews first and publishes nothing when the head refuses."""

    from cruxible_client.contracts.proposal_models import (
        ProposalPreview,
        ProposalPreviewEvaluation,
    )
    from cruxible_core.proposals.proposals import ProposalService

    http, instance_id, reviewer_key = independent
    instance = get_playbill_manager().get(instance_id)
    body = service_store_playbill_body(instance, content=b"# Refused at the head\n").digest
    document = service_propose_playbill_document(
        instance,
        shell=_shell(body),
        actor_id="operator",
        proposal_name="refused-at-head",
        timestamp="2026-11-16T12:00:00.000000Z",
    ).proposal.admission.proposal_id
    register_late(
        http, instance_id, tmp_path, reviewer_key, tmp_path / "owner-custody" / "operator.ed25519"
    )
    admissions = instance.proposal_evidence().list_admissions()
    head = instance.accepted_coordinate()

    def refused_preview(self, **_options):  # type: ignore[no-untyped-def]
        return ProposalPreview(
            evaluation=ProposalPreviewEvaluation(
                verdict="refused", evaluated_base_oid=head.git_oid, rebased=True
            )
        )

    def no_submit(self, **_options):  # type: ignore[no-untyped-def]
        raise AssertionError("a refused landing must not be submitted")

    monkeypatch.setattr(ProposalService, "preview", refused_preview)
    monkeypatch.setattr(ProposalService, "submit", no_submit)
    refused = http.post(
        f"/api/v1/{instance_id}/proposals/{document}/approval-challenge",
        json={"signer_id": "late"},
        headers=LATE,
    )

    assert refused.status_code == 400, refused.text
    assert refused.json()["context"]["recompile_reason"] == "evaluation_refused"
    assert instance.proposal_evidence().list_admissions() == admissions
    assert instance.proposal_evidence().read_withdrawal(document) is None
    assert instance.accepted_coordinate() == head


def test_migration_detection_reads_the_leaf_in_the_authors_namespace() -> None:
    """A principal named `claim-type-migration-owner` made every ordinary ref of theirs
    read as a generated migration; only the submitter's migration leaf does."""

    from types import SimpleNamespace

    from cruxible_core.service.proposals.proposals import cannot_rebase, exact_bytes_cannot_rebase

    members = (
        SimpleNamespace(artifact_kind="claim-type"),
        SimpleNamespace(artifact_kind="subject-kind"),
    )

    def proposal(actor: str, leaf: str):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            admission=SimpleNamespace(
                actor_id=actor,
                proposal_id="sha256:" + "a" * 64,
                target_ref=f"refs/proposals/{actor}/{leaf}",
            ),
            candidate=SimpleNamespace(members=members),
        )

    ordinary = proposal("claim-type-migration-owner", "intent-0123456789abcdef")
    migration = proposal("owner", "claim-type-migration-0123456789abcdef")

    ordinary_reason = cannot_rebase(ordinary)  # type: ignore[arg-type]
    migration_reason = cannot_rebase(migration)  # type: ignore[arg-type]
    # Both hold a ClaimType closure with no retained input; only one is a migration.
    assert ordinary_reason is not None and "migration" not in ordinary_reason[0]
    assert migration_reason is not None and "migration" in migration_reason[0]
    assert exact_bytes_cannot_rebase(migration) is True  # type: ignore[arg-type]
