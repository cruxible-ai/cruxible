from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.captures import (
    CaptureProductionUnproven,
    capture_contract_digest,
    capture_contract_path,
    evaluate_capture_contract_law,
    render_capture_contract,
    verify_capture,
)
from cruxible_client.contracts.claim_types import claim_type_digest, render_claim_type
from cruxible_client.contracts.claims import (
    ClaimBacking,
    build_claim_citation,
    claim_path,
    render_claim,
)
from cruxible_client.contracts.providers import provider_digest, provider_path, render_provider
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import render_subject, subject_digest, subject_path
from cruxible_core.evidence.source_readers import (
    ExternalSourceReadRequestV1,
    ProducerBindingV1,
)
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from tests.core_support._pc_c_support import (
    NOW,
    body_store,
    capture_contract,
    digest,
    provider,
    provider_run,
)
from tests.core_support._subject_kind_support import core_assigned
from tests.support.external_sources import FakeVersionedExternalSourceReader
from tests.support.provider_builds import capture_provider_build
from tests.test_claims.test_claims import TIMESTAMP, _claim, _claim_type, _subject


def test_general_contract_and_exact_external_capture_do_not_copy_a_table(tmp_path: Path) -> None:
    contract = capture_contract()
    accepted = evaluate_capture_contract_law(
        contract,
        path="capture-contracts/test.orders-v1.json",
        predecessor=None,
    )
    assert accepted.verdict == "accepted"
    provider_artifact = provider(contract)
    binding = ProducerBindingV1(
        provider=provider_artifact.identity,
        logical_source_identity="commerce.production.orders",
        adapter_digest=digest("test-adapter", "postgres-v1"),
    )
    reader = FakeVersionedExternalSourceReader()
    reader.seed(
        source_identity="commerce.production.orders",
        coordinate_type="postgres-lsn-v1",
        coordinate="0/16B6C50",
        selector_type="relation-primary-key-v1",
        selector={"relation": "orders", "key": {"order_id": "ord-482"}},
        value={"order_id": "ord-482", "status": "released"},
    )
    store = body_store(tmp_path)
    result = reader.acquire(
        ExternalSourceReadRequestV1(
            contract=contract,
            provider=provider_artifact,
            binding=binding,
            coordinate_type="postgres-lsn-v1",
            coordinate="0/16B6C50",
            selector_type="relation-primary-key-v1",
            selector={"relation": "orders", "key": {"order_id": "ord-482"}},
            materialization="external",
            run_coordinate=provider_run(provider_artifact),
            observed_at=NOW,
            resource_budget=contract.selection_budget,
        ),
        store=store,
    )
    assert result.envelope.commitment.materialization == "external"
    assert result.canonical_material is None
    assert store.verify(result.capture_digest)
    assert not store.verify(result.envelope.commitment.digest)
    verified = verify_capture(
        result.capture_digest,
        store=store,
        contract=contract,
        producer_artifact_digests={
            provider_artifact.identity.qualified: provider_digest(provider_artifact).tagged
        },
        # A fixture-reader v1 envelope: it replays, but is never a new citation.
        require_production_proof=False,
    )
    assert verified.source == result.envelope.source
    assert reader.replay_available(verified.source)
    # Replaying fixture bytes is distinct from admitting a new citation.
    with pytest.raises(CaptureProductionUnproven) as refused:
        verify_capture(
            result.capture_digest,
            store=store,
            contract=contract,
            producer_artifact_digests={
                provider_artifact.identity.qualified: provider_digest(provider_artifact).tagged
            },
        )
    assert refused.value.reason == "v1_envelope"


def test_attested_only_acquisition_remains_honest_when_source_cannot_replay(
    tmp_path: Path,
) -> None:
    contract = capture_contract()
    provider_artifact = provider(contract)
    binding = ProducerBindingV1(
        provider=provider_artifact.identity,
        logical_source_identity="commerce.production.orders",
        adapter_digest=digest("test-adapter", "api-v1"),
    )
    reader = FakeVersionedExternalSourceReader()
    selector = {"relation": "orders", "key": {"order_id": "ord-latest"}}
    reader.seed(
        source_identity="commerce.production.orders",
        coordinate_type="postgres-lsn-v1",
        coordinate="latest",
        selector_type="relation-primary-key-v1",
        selector=selector,
        value={"order_id": "ord-latest", "status": "pending"},
        replayability="attested_only",
    )
    result = reader.acquire(
        ExternalSourceReadRequestV1(
            contract=contract,
            provider=provider_artifact,
            binding=binding,
            coordinate_type="postgres-lsn-v1",
            coordinate="latest",
            selector_type="relation-primary-key-v1",
            selector=selector,
            materialization="none",
            run_coordinate=provider_run(provider_artifact),
            observed_at=NOW,
            resource_budget=contract.selection_budget,
        ),
        store=body_store(tmp_path),
    )
    assert result.envelope.source.replayability == "attested_only"  # type: ignore[union-attr]
    assert result.envelope.commitment.materialization == "none"
    assert not reader.replay_available(result.envelope.source)  # type: ignore[arg-type]


def test_external_coordinates_refuse_secrets_and_unpinned_schemas() -> None:
    contract = capture_contract()
    provider_artifact = provider(contract)
    binding = ProducerBindingV1(
        provider=provider_artifact.identity,
        logical_source_identity="commerce.production.orders",
        adapter_digest=digest("test-adapter", "postgres-v1"),
    )
    payload = {
        "contract": contract,
        "provider": provider_artifact,
        "binding": binding,
        "coordinate_type": "postgres-lsn-v1",
        "coordinate": {"connection_string": "postgres://secret"},
        "selector_type": "relation-primary-key-v1",
        "selector": {"id": "1"},
        "materialization": "external",
        "run_coordinate": provider_run(provider_artifact),
        "observed_at": NOW,
        "resource_budget": contract.selection_budget,
    }
    with pytest.raises(ValidationError, match="secret-bearing|locators"):
        ExternalSourceReadRequestV1.model_validate(payload)
    payload["coordinate"] = "0/1"
    payload["coordinate_type"] = "unregistered-v1"
    with pytest.raises(ValidationError, match="not pinned"):
        ExternalSourceReadRequestV1.model_validate(payload)


def test_external_capture_admission_refuses_the_retired_contract_format(
    tmp_path: Path,
) -> None:
    # New citations must prove production; no legacy-admission monkeypatch.
    # The current-only floor refuses new legacy artifacts, independently of replay.
    from cruxible_client.contracts.policies import (
        ClaimEvidenceAdmissionPolicy,
        ClaimEvidenceAdmissionRule,
    )
    from tests.core_support._support import initialize_local

    contract = capture_contract()
    coordinate_type = "postgres-lsn-v1"
    shell = _subject()
    subject_address = SemanticAddress.whole_artifact(
        subject_path(shell.subject_kind, shell.subject_id)
    )
    selector = {
        "relation": "orders",
        "key": {"order_id": "ord-482"},
        "semantic_subject": subject_address.model_dump(mode="json"),
    }
    value = {"order_id": "ord-482", "status": "ready"}
    instance, _owner = initialize_local(tmp_path)
    provider_artifact = capture_provider_build(contract)
    reader = FakeVersionedExternalSourceReader()
    reader.seed(
        source_identity="commerce.production.orders",
        coordinate_type=coordinate_type,
        coordinate="0/16B6C50",
        selector_type="relation-primary-key-v1",
        selector=selector,
        value=value,
    )
    binding = ProducerBindingV1(
        provider=provider_artifact.identity,
        logical_source_identity="commerce.production.orders",
        adapter_digest=digest("test-adapter", "postgres-v1"),
    )
    acquired = reader.acquire(
        ExternalSourceReadRequestV1(
            contract=contract,
            provider=provider_artifact,
            binding=binding,
            coordinate_type=coordinate_type,
            coordinate="0/16B6C50",
            selector_type="relation-primary-key-v1",
            selector=selector,
            materialization="external",
            run_coordinate=provider_run(provider_artifact),
            observed_at=NOW,
            resource_budget=contract.selection_budget,
        ),
        store=instance.body_store(),
    )
    capture_digest_value = acquired.capture_digest
    envelope = acquired.envelope
    base = instance.accepted_coordinate()
    contract_digest = capture_contract_digest(contract).tagged
    claim_type = _claim_type().model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
                rules=(
                    ClaimEvidenceAdmissionRule(
                        rule_id="external-order-record",
                        claim_roles=("observation",),
                        capture_contracts=(contract.identity.name,),
                        admission="direct",
                        subject_binding="contract_source_mapping",
                    ),
                )
            )
        }
    )
    claim_type_digest_value = claim_type_digest(claim_type).tagged
    claim = _claim(
        claim_id="CLM-0123456789abcdef0123456789abcdef",
        capture_digest=capture_digest_value,
        source_digest=envelope.commitment.digest,
        source_length=1,
    )
    claim = claim.model_copy(
        update={
            "statement": claim.statement.model_copy(
                update={
                    "claim_type_digest": claim_type_digest_value,
                    "subject": subject_address,
                }
            ),
            "backing": ClaimBacking(
                referent_context=claim.backing.referent_context,
                capture_digests=(capture_digest_value,),
                citations=(
                    build_claim_citation(
                        claim.identity,
                        capture_digest=capture_digest_value,
                        role="evidence",
                        origin="independent",
                    ),
                ),
            ),
            "pins": tuple(
                sorted(
                    (
                        ArtifactPin(
                            role="capture-contract",
                            target=contract.identity,
                            artifact_digest=contract_digest,
                        ),
                        ArtifactPin(
                            role="claim-type",
                            target=claim_type.identity,
                            artifact_digest=claim_type_digest_value,
                        ),
                        ArtifactPin(
                            role="provider",
                            target=provider_artifact.identity,
                            artifact_digest=provider_digest(provider_artifact).tagged,
                        ),
                        ArtifactPin(
                            role="subject",
                            target=shell.identity,
                            artifact_digest=subject_digest(shell).tagged,
                        ),
                    ),
                    key=lambda item: (item.role, item.target.qualified),
                )
            ),
        }
    )
    from tests.support.operation_world import contract_files

    accepted_tree = dict(instance.tree_at(base.git_oid))
    candidate_tree = core_assigned(
        instance,
        {
            **accepted_tree,
            **contract_files(),
            subject_path(shell.subject_kind, shell.subject_id): render_subject(shell),
            "claim-types/project.work_item/status.json": render_claim_type(claim_type),
            capture_contract_path(contract.identity.name): render_capture_contract(contract),
            provider_path(provider_artifact.identity.name): render_provider(provider_artifact),
            claim_path(claim.identity.name): render_claim(claim),
        },
    )
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/external-claim",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=candidate_tree,
        timestamp=TIMESTAMP,
    )
    assert contract.artifact_format == "playbill-capture-contract-v1"
    assert contract.identity.qualified == "CaptureContract:test.orders-v1"
    assert envelope.commitment.materialization == "external"
    assert acquired.canonical_material is None
    assert not instance.body_store().verify(envelope.commitment.digest)
    assert proposed.candidate is None
    (format_refusal,) = [
        diagnostic
        for diagnostic in proposed.evaluation.diagnostics
        if diagnostic.subject
        == SemanticAddress.whole_artifact(capture_contract_path(contract.identity.name))
    ]
    assert format_refusal.code == "cruxible.proposal.member_format_invalid"
    assert format_refusal.severity == "error"
    assert format_refusal.message == "new CaptureContracts require cruxible-capture-contract-v4"
    assert instance.accepted_coordinate() == base
    assert dict(instance.tree_at(instance.accepted_coordinate().git_oid)) == accepted_tree
