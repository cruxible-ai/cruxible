from __future__ import annotations

from pathlib import Path

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    CaptureRunCoordinateV1,
    InputReceiptSetManifestV1,
    build_cas_capture,
    build_coordinator_self_source_capture,
    build_derived_cas_capture,
    build_ledger_capture,
    capture_contract_digest,
    capture_contract_is_self_asserted,
    capture_is_coordinator_self_source,
    evaluate_capture_contract_law,
    verify_capture,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_references import LedgerSourceReferenceV1
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.projection import AcceptedCoordinate
from tests.test_playbill._pc_c_support import (
    NOW,
    artifact_digest,
    body_store,
    capture_contract,
    digest,
    provider,
    provider_run,
)


def _accepted_coordinate() -> AcceptedCoordinate:
    return AcceptedCoordinate(
        git_oid="11" * 32,
        semantic_root=digest("semantic", "one"),
        generation_root=digest("generation", "one"),
        compiler_digest=digest("compiler", "one"),
    )


class _LedgerResolver:
    def __init__(self, source: LedgerSourceReferenceV1, content: bytes) -> None:
        self.source = source
        self.content = content

    def read_ledger_source(self, source: LedgerSourceReferenceV1) -> bytes:
        if source != self.source:
            raise AssertionError("unexpected ledger source")
        return self.content


def test_cas_and_ledger_capture_commitments_verify_without_copying_ledger_bytes(
    tmp_path: Path,
) -> None:
    contract = capture_contract()
    provider_artifact = provider(contract)
    store = body_store(tmp_path)
    cas_result = build_cas_capture(
        store=store,
        contract=contract,
        source_body=b'{"status":"released"}',
        run_coordinate=provider_run(provider_artifact),
        run_receipt_digest=digest("receipt", "cas"),
        producer=provider_artifact.identity,
        producer_binding_digest=digest("binding", "cas"),
        observed_at=NOW,
    )
    assert (
        verify_capture(
            cas_result.capture_digest,
            store=store,
            contract=contract,
            producer_artifact_digests={
                provider_artifact.identity.qualified: provider_run(
                    provider_artifact
                ).executable_digest
            },
        )
        == cas_result.envelope
    )

    ledger_body = b'{"policy":"approved"}\n'
    ledger_source = LedgerSourceReferenceV1(
        address=SemanticAddress.whole_artifact("documents/release-policy.yaml"),
        coordinate=_accepted_coordinate(),
    )
    ledger_result = build_ledger_capture(
        store=store,
        contract=contract,
        source=ledger_source,
        source_body=ledger_body,
        run_coordinate=provider_run(provider_artifact, run_id="ledger-run"),
        run_receipt_digest=digest("receipt", "ledger"),
        producer=provider_artifact.identity,
        producer_binding_digest=digest("binding", "ledger"),
        observed_at=NOW,
    )
    assert not store.verify(ledger_result.commitment_digest)
    assert (
        verify_capture(
            ledger_result.capture_digest,
            store=store,
            contract=contract,
            ledger_resolver=_LedgerResolver(ledger_source, ledger_body),
            producer_artifact_digests={
                provider_artifact.identity.qualified: provider_run(
                    provider_artifact
                ).executable_digest
            },
        )
        == ledger_result.envelope
    )


def test_derived_capture_requires_and_replays_exact_input_manifest(tmp_path: Path) -> None:
    contract = capture_contract(epistemic_grade="derived")
    provider_artifact = provider(contract)
    store = body_store(tmp_path)
    manifest = InputReceiptSetManifestV1(
        input_receipt_digests=(digest("receipt", "input"),),
        input_capture_digests=(digest("capture", "input"),),
        input_claim_artifact_digests=(artifact_digest("claim", "input"),),
    )
    result = build_derived_cas_capture(
        store=store,
        contract=contract,
        output_body=b'{"released":true}',
        manifest=manifest,
        reducer_digest=artifact_digest("reducer", "release-v1"),
        run_coordinate=provider_run(provider_artifact),
        run_receipt_digest=digest("receipt", "derived"),
        producer=provider_artifact.identity,
        producer_binding_digest=digest("binding", "derived"),
        observed_at=NOW,
    )
    assert result.envelope.input_receipt_set_manifest_digest is not None
    assert (
        verify_capture(
            result.capture_digest,
            store=store,
            contract=contract,
            producer_artifact_digests={
                provider_artifact.identity.qualified: provider_run(
                    provider_artifact
                ).executable_digest
            },
        )
        == result.envelope
    )


def test_contract_run_can_produce_capture_without_claiming_provider_identity(
    tmp_path: Path,
) -> None:
    contract = capture_contract()
    result = build_cas_capture(
        store=body_store(tmp_path),
        contract=contract,
        source_body=b"bounded source",
        run_coordinate=CaptureRunCoordinateV1(
            run_kind="watcher",
            run_id="watcher-run",
            bound_generation=digest("generation", "one"),
            executable_identity=contract.identity,
            executable_digest=capture_contract_digest(contract).tagged,
        ),
        run_receipt_digest=digest("receipt", "watcher"),
        producer=ArtifactIdentity(kind="Watcher", name="orders"),
        producer_binding_digest=digest("binding", "watcher"),
        observed_at=NOW,
    )
    assert result.envelope.producer.kind == "Watcher"


def test_coordinator_self_source_profile_is_cas_only_and_claim_bound(tmp_path: Path) -> None:
    contract = COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
    store = body_store(tmp_path)
    claim_id = "CLM-0123456789abcdef0123456789abcdef"
    body = b"The work item is ready."
    result = build_coordinator_self_source_capture(
        store=store,
        actor_id="owner",
        claim_id=claim_id,
        body=body,
        observed_at=NOW,
        accepted_coordinate=_accepted_coordinate(),
    )

    assert contract.identity.qualified == ("CaptureContract:playbill.coordinator-self-source-v1")
    assert contract.logical_source_identities == ("playbill.coordinator-authoring",)
    assert contract.allowed_source_kinds == ("cas",)
    assert contract.allowed_materialization_modes == ("cas",)
    assert contract.retention_erasure_policy.erasure == "prohibited"
    assert capture_contract_is_self_asserted(contract)
    assert result.source_body_materialized is True
    assert store.verify(result.commitment_digest)
    assert store.verify(result.capture_digest)
    assert (
        store.read(
            result.commitment_digest,
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
        == body
    )
    assert verify_capture(result.capture_digest, store=store, contract=contract) == result.envelope
    assert capture_is_coordinator_self_source(
        result.envelope,
        contract=contract,
        claim_id=claim_id,
    )
    assert not capture_is_coordinator_self_source(
        result.envelope,
        contract=contract,
        claim_id="CLM-abcdefabcdefabcdefabcdefabcdefab",
    )
    assert (
        evaluate_capture_contract_law(
            contract,
            path="capture-contracts/playbill.coordinator-self-source-v1.yaml",
            actor_roles=("owner",),
            predecessor=None,
        ).verdict
        == "accepted"
    )
    rewritten = contract.model_copy(
        update={"authority": contract.authority.model_copy(update={"approve_roles": ("security",)})}
    )
    refused = evaluate_capture_contract_law(
        rewritten,
        path="capture-contracts/playbill.coordinator-self-source-v1.yaml",
        actor_roles=("owner",),
        predecessor=None,
    )
    assert refused.verdict == "refused"
    assert refused.diagnostics[0].code == ("playbill.capture_contract.coordinator_profile_mismatch")
