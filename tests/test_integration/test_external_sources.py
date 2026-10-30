from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.candidates import CandidateRecord
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
    ClaimLawEvidenceV1,
    build_claim_citation,
    claim_path,
    render_claim,
)
from cruxible_client.contracts.providers import provider_digest, provider_path, render_provider
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import render_subject, subject_digest, subject_path
from cruxible_core.evidence.source_readers import (
    ExternalSourceReadRequestV1,
    FakeVersionedExternalSourceReader,
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


@pytest.mark.parametrize("light", [False, True], ids=["v1-unmaterialized", "v2-retained"])
def test_external_capture_supports_claim_only_through_exact_contract_mapping(
    tmp_path: Path,
    light: bool,
) -> None:
    # New citations must prove production; no legacy-admission monkeypatch.
    # The unmaterialized legacy-success premise remains pending disposition.
    from cruxible_client.contracts.policies import (
        ClaimEvidenceAdmissionPolicy,
        ClaimEvidenceAdmissionPolicyV1,
        ClaimEvidenceAdmissionRule,
        ClaimEvidenceAdmissionRuleV1,
    )
    from tests.core_support._light_capture_support import record_contract
    from tests.core_support._support import initialize_local

    contract = record_contract() if light else capture_contract()
    coordinate_type = "database-snapshot-v1" if light else "postgres-lsn-v1"
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
    if light:
        import json

        from cruxible_client.contracts.canonical import canonical_bytes
        from cruxible_client.contracts.captures import CaptureEnvelope, parse_capture_envelope
        from cruxible_client.contracts.procedures.artifacts import procedure_path, render_procedure
        from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
        from cruxible_client.contracts.providers import parse_provider
        from cruxible_core.storage.cas import BodyAccessContext
        from tests.support.procedures import revalidated
        from tests.test_procedures import test_procedure_source_runs as fixtures

        # Real current Source planning, exact workspace bytes, provider receipt
        # and produced-Capture journal record, without provider-side JSON parsing.
        instance, owner, procedure, root, _policy = fixtures._world(
            tmp_path, contents=canonical_bytes(value), contract=contract, accept_procedure=False
        )
        source, shape = procedure.definition.nodes
        request = {**source.request, "coordinate": "0/16B6C50", "selector": selector}
        definition = procedure.definition.model_copy(
            update={"nodes": (source.model_copy(update={"request": request}), shape)}
        )
        procedure = revalidated(
            procedure.model_copy(
                update={
                    "definition": definition,
                    "definition_digest": compute_procedure_definition_digest(definition).tagged,
                }
            )
        )
        fixtures._accept_more(
            instance,
            owner,
            {procedure_path(procedure.identity.name): render_procedure(procedure)},
            name="external-source-mapping",
        )
        state, invoker = fixtures._run(instance, root, evaluation_time=NOW)
        assert state.status == "succeeded", state.terminal
        assert invoker.spawn_calls == 1
        (observation,) = state.source_observations
        assert observation.capture_digest is not None
        capture_digest_value = observation.capture_digest
        access = BodyAccessContext(principal_id="external-source-test", can_read_body=True)
        envelope = parse_capture_envelope(
            instance.body_store().read(capture_digest_value, access=access)
        )
        assert isinstance(envelope, CaptureEnvelope)
        assert envelope.source.source_identity == "commerce.production.orders"
        assert envelope.source.coordinate_type == coordinate_type
        assert envelope.source.coordinate == "0/16B6C50"
        assert envelope.source.selector == selector
        assert envelope.source.replayability == "exact"
        assert envelope.run_coordinate.run_id == state.run_id
        tree = instance.tree_at(instance.accepted_coordinate().git_oid)
        path = provider_path(envelope.producer.name)
        provider_artifact = parse_provider(tree[path], path=path)
        assert (
            envelope.run_coordinate.executable_digest == provider_digest(provider_artifact).tagged
        )
        material = json.loads(instance.body_store().read(envelope.commitment.digest, access=access))
        assert material["content"]["text"] == canonical_bytes(value).decode("utf-8")
        assert observation.source_read_receipt is not None
        assert material["source"]["bytes_digest"] == observation.source_read_receipt.bytes_digest
    else:
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
            "artifact_format": "playbill-claim-type-v7",
            "evidence_requirement": "captured",
            "revision_evidence": "replace",
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
                rules=(
                    ClaimEvidenceAdmissionRule(
                        rule_id="external-order-record",
                        claim_roles=("observation",),
                        capture_contracts=(
                            {
                                "contract": contract.identity.name,
                                "logical_sources": ["commerce.production.orders"],
                            },
                        ),
                        admission="direct",
                        subject_binding="contract_source_mapping",
                    ),
                )
            ),
        }
    )
    if not light:
        claim_type = _claim_type().model_copy(
            update={
                "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV1(
                    rules=(
                        ClaimEvidenceAdmissionRuleV1(
                            rule_id="external-order-record",
                            claim_roles=("observation",),
                            capture_contract_digests=(contract_digest,),
                            evidence_kinds=("database_record",),
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

    candidate_tree = core_assigned(
        instance,
        {
            **instance.tree_at(base.git_oid),
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
    assert not proposed.evaluation.diagnostics, proposed.evaluation.diagnostics
    assert isinstance(proposed.candidate, CandidateRecord)
    evidence = ClaimLawEvidenceV1.model_validate(
        next(
            item.result["claim_evidence"]
            for item in proposed.candidate.law_evidence
            if item.path == claim_path(claim.identity.name)
        )
    )
    assert evidence.initial_verdict == "supported"
    assert evidence.evidence_basis == ("direct",)
    assert claim.backing.source_mappings == ()
    if not light:
        assert envelope.commitment.materialization == "external"
        assert acquired.canonical_material is None
        assert not instance.body_store().verify(envelope.commitment.digest)
        return
    assert envelope.commitment.materialization == "cas"
    assert json.loads(material["content"]["text"]) == value
    assert instance.body_store().verify(envelope.commitment.digest)
    assert (
        verify_capture(
            capture_digest_value,
            store=instance.body_store(),
            contract=contract,
            producer_artifact_digests={
                provider_artifact.identity.qualified: provider_digest(provider_artifact).tagged
            },
            producer_receipt_resolver=instance.capture_verification_context(),
        )
        == envelope
    )

    # Retention is not subject binding: without the declared record mapping,
    # or with a different source label, these bytes cannot support this Claim.
    (rule,) = claim_type.evidence_admission_policy.rules
    alternatives = {
        "byte-binding": rule.model_copy(update={"subject_binding": "exact_claim_subject"}),
        "other-source": rule.model_copy(
            update={
                "capture_contracts": (
                    rule.capture_contracts[0].model_copy(
                        update={"logical_sources": ("commerce.other.orders",)}
                    ),
                )
            }
        ),
    }
    for name, incompatible_rule in alternatives.items():
        incompatible_type = claim_type.model_copy(
            update={
                "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
                    rules=(incompatible_rule,)
                )
            }
        )
        incompatible_digest = claim_type_digest(incompatible_type).tagged
        incompatible_claim = claim.model_copy(
            update={
                "statement": claim.statement.model_copy(
                    update={"claim_type_digest": incompatible_digest}
                ),
                "pins": tuple(
                    pin.model_copy(update={"artifact_digest": incompatible_digest})
                    if pin.role == "claim-type"
                    else pin
                    for pin in claim.pins
                ),
            }
        )
        refused = instance.proposal_service().submit(
            actor=AuthenticatedActor(actor_id="owner"),
            request=ProposalAdmissionRequest(
                target_ref=f"refs/proposals/owner/{name}", proposed_base_oid=base.git_oid
            ),
            candidate_tree={
                **candidate_tree,
                "claim-types/project.work_item/status.json": render_claim_type(incompatible_type),
                claim_path(claim.identity.name): render_claim(incompatible_claim),
            },
            timestamp=TIMESTAMP,
        )
        assert refused.candidate is None
        assert "cruxible.claim.captured_evidence_required" in {
            diagnostic.code for diagnostic in refused.evaluation.diagnostics
        }

    # Lose only the material through the CAS API: the real envelope and
    # production journal remain, but a new citation cannot use missing bytes.
    assert instance.body_store().erase(envelope.commitment.digest)
    assert instance.body_store().verify(capture_digest_value)
    assert not instance.body_store().verify(envelope.commitment.digest)
    unavailable = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/missing-material", proposed_base_oid=base.git_oid
        ),
        candidate_tree=candidate_tree,
        timestamp=TIMESTAMP,
    )
    assert unavailable.candidate is None
    assert "cruxible.claim.capture_unverified" in {
        diagnostic.code for diagnostic in unavailable.evaluation.diagnostics
    }
    assert instance.accepted_coordinate() == base
