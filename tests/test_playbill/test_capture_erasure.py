from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    CaptureRunCoordinateV1,
    build_cas_capture,
    build_coordinator_self_source_capture,
    capture_contract_digest,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_references import OpenSourceRequestV1, SourceHandleV1
from cruxible_core.playbill.capture_erasure import (
    CaptureErasureError,
    CaptureErasureStatementV1,
    capture_erasure_statement_bytes,
    erase_capture_body,
    parse_capture_erasure_receipt,
    render_capture_erasure_receipt,
    verify_erasure_receipt,
)
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.dereference import dereference_source_handle
from cruxible_core.playbill.projection import AcceptedCoordinate
from tests.test_playbill._pc_c_support import (
    NOW,
    body_store,
    capture_contract,
    digest,
)


class _Signer:
    key_id = "daemon-erasure-1"

    def __init__(self) -> None:
        self.private_key = Ed25519PrivateKey.generate()

    def sign_erasure(self, statement: CaptureErasureStatementV1) -> str:
        return self.private_key.sign(capture_erasure_statement_bytes(statement)).hex()

    @property
    def public_key(self) -> str:
        return self.private_key.public_key().public_bytes_raw().hex()


def _capture(tmp_path: Path, *, erasure: bool):
    contract = capture_contract(erasure=erasure)
    store = body_store(tmp_path)
    result = build_cas_capture(
        store=store,
        contract=contract,
        source_body=b'{"personal_id":"pseudonym-1"}',
        run_coordinate=CaptureRunCoordinateV1(
            run_kind="watcher",
            run_id="retention-run",
            bound_generation=digest("generation", "one"),
            executable_identity=contract.identity,
            executable_digest=capture_contract_digest(contract).tagged,
        ),
        run_receipt_digest=digest("receipt", "retention"),
        producer=ArtifactIdentity(kind="Watcher", name="retention"),
        producer_binding_digest=digest("binding", "retention"),
        observed_at=NOW,
    )
    return contract, store, result


def test_authorized_erasure_preserves_envelope_and_emits_signed_receipt(tmp_path: Path) -> None:
    contract, store, result = _capture(tmp_path, erasure=True)
    signer = _Signer()
    receipt = erase_capture_body(
        instance_id="inst-erasure",
        capture_digest=result.capture_digest,
        contract=contract,
        store=store,
        erased_at=NOW,
        authorized_by="owner",
        authorization_proof_digest=digest("authorization", "erase-one"),
        signer=signer,
    )
    verify_erasure_receipt(receipt, public_key=signer.public_key)
    assert store.verify(result.capture_digest)
    assert not store.verify(result.commitment_digest)
    assert receipt.body_digest == result.commitment_digest
    assert store.verify(receipt.cas_digest)
    assert (
        parse_capture_erasure_receipt(
            store.read(
                receipt.cas_digest,
                access=BodyAccessContext(principal_id="test", can_read_body=True),
            )
        )
        == receipt
    )
    assert receipt.cas_digest.endswith(
        hashlib.sha256(render_capture_erasure_receipt(receipt)).hexdigest()
    )


def test_contract_without_erasure_authority_refuses_body_deletion(tmp_path: Path) -> None:
    contract, store, result = _capture(tmp_path, erasure=False)
    with pytest.raises(CaptureErasureError, match="prohibits"):
        erase_capture_body(
            instance_id="inst-erasure",
            capture_digest=result.capture_digest,
            contract=contract,
            store=store,
            erased_at=NOW,
            authorized_by="owner",
            authorization_proof_digest=digest("authorization", "erase-one"),
            signer=_Signer(),
        )
    assert store.verify(result.commitment_digest)


def test_coordinator_body_cannot_be_authorized_for_erasure_and_missing_is_honest(
    tmp_path: Path,
) -> None:
    store = body_store(tmp_path)
    coordinate = AcceptedCoordinate(
        git_oid="11" * 32,
        semantic_root=digest("semantic", "coordinator"),
        generation_root=digest("generation", "coordinator"),
        compiler_digest=digest("compiler", "coordinator"),
    )
    result = build_coordinator_self_source_capture(
        store=store,
        actor_id="owner",
        claim_id="CLM-0123456789abcdef0123456789abcdef",
        body=b"retained coordinator body",
        observed_at=NOW,
        accepted_coordinate=coordinate,
    )
    with pytest.raises(CaptureErasureError, match="prohibits"):
        erase_capture_body(
            instance_id="inst-erasure",
            capture_digest=result.capture_digest,
            contract=COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
            store=store,
            erased_at=NOW,
            authorized_by="owner",
            authorization_proof_digest=digest("authorization", "forbidden"),
            signer=_Signer(),
        )
    assert store.verify(result.commitment_digest)

    # Simulate corruption below the policy layer. Readers report absence; they
    # never downgrade the retained profile to attested-only or fabricate bytes.
    assert store.erase(result.commitment_digest)
    handle = SourceHandleV1(
        subject=SemanticAddress.whole_artifact(
            "claims/01/CLM-0123456789abcdef0123456789abcdef.yaml"
        ),
        at=coordinate,
        source=result.envelope.source,
        commitment=result.envelope.commitment,
        access_class="instance",
    )
    opened = dereference_source_handle(
        OpenSourceRequestV1(source_handle=handle, resource_budget_bytes=1024),
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
        resolver=_StoreResolver(store),
    )
    assert opened.status == "unavailable"
    assert opened.commitment_verified is False
    assert opened.material_kind == "metadata_only"
    assert opened.coverage.reason_codes == ("body_unavailable",)


class _StoreResolver:
    def __init__(self, store) -> None:
        self.store = store

    def read_cas(self, content_digest: str, *, access: BodyAccessContext) -> bytes | None:
        if not self.store.verify(content_digest):
            return None
        return self.store.read(content_digest, access=access)

    def read_ledger(self, artifact_path: str) -> bytes | None:
        return None

    def read_external(self, source) -> object | None:
        return None
