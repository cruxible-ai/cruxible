from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cruxible_core.playbill.artifacts import ArtifactIdentity
from cruxible_core.playbill.capture_erasure import (
    CaptureErasureError,
    CaptureErasureStatementV1,
    capture_erasure_statement_bytes,
    erase_capture_body,
    verify_erasure_receipt,
)
from cruxible_core.playbill.captures import (
    CaptureRunCoordinateV1,
    build_cas_capture,
    capture_contract_digest,
)
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
