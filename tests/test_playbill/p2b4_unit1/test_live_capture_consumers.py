"""Live end-to-end coverage for every production Capture verifier caller."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cruxible_client.contracts.authoring.models import (
    ClaimAuthoringPayloadV3,
    ClaimDependencyDraftsV1,
    ExistingCaptureCitationSourceV1,
)
from cruxible_client.contracts.captures import (
    capture_contract_digest,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
)
from cruxible_client.contracts.procedures.artifacts import render_procedure
from cruxible_client.contracts.provider_interfaces import render_provider_interface
from cruxible_client.contracts.providers import provider_path, render_provider
from cruxible_client.contracts.subjects import render_subject, subject_path
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.lowering import lower_authoring
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.exhaust import LocalJournalBackend
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.procedures.egress import (
    CaptureTerminalEgressSink,
    TerminalEgressReceiptV2,
    compute_effective_rung,
)
from cruxible_core.playbill.procedures.execution import ProcedureExecutor
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.provider_classifiers import ProviderBucketClassifierRegistry
from cruxible_core.service.playbill_claim_attestations import service_append_claim_attestation
from tests.test_playbill._p2b1_support import (
    accepted_interface,
    accepted_provider,
    install_demo_classifier,
)
from tests.test_playbill._pc_c_support import capture_contract
from tests.test_playbill._support import generate_client
from tests.test_playbill.p2b4_unit1.test_source_v4_runtime import (
    _payloads,
    _source_fixture,
    _SourceInvoker,
)
from tests.test_playbill.test_authoring_existing_capture import _activate
from tests.test_playbill.test_authoring_preflight import _working_payload
from tests.test_playbill.test_claim_attestation_service import _request
from tests.test_playbill.test_claims import _claim_type, _subject
from tests.test_playbill.test_procedure_execution import _Authority, _Contracts
from tests.test_playbill.test_resolution_contracts import _accept_tree


def _instance(root: Path) -> tuple[PlaybillInstance, object]:
    managed = root / "managed"
    owner = generate_client(
        root,
        managed_root=managed,
        principal_id="owner",
        roles=("owner",),
    )
    reviewer = generate_client(
        root,
        managed_root=managed,
        principal_id="reviewer",
        roles=("reviewer",),
    )
    return (
        PlaybillInstance.initialize(
            managed,
            instance_id="instance-a",
            client_principals=(owner.principal, reviewer.principal),
            workspace_roots=(root / "workspace",),
            timestamp="2026-09-02T12:00:00.000000Z",
        ),
        owner,
    )


def test_both_v2_producer_arms_verify_through_every_live_production_consumer(
    tmp_path: Path,
) -> None:
    instance, owner = _instance(tmp_path)
    contract = capture_contract()
    contract_digest = capture_contract_digest(contract).tagged
    shell = _subject()
    claim_type = _claim_type().model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV1(
                rules=(
                    ClaimEvidenceAdmissionRuleV1(
                        rule_id="live-v2-capture",
                        claim_roles=("normative", "observation"),
                        capture_contract_digests=(contract_digest,),
                        evidence_kinds=contract.evidence_kinds,
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            )
        }
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[subject_path(shell.subject_kind, shell.subject_id)] = render_subject(shell)
    tree[claim_type_path(claim_type.predicate)] = render_claim_type(claim_type)
    tree[capture_contract_path(contract.identity.name)] = render_capture_contract(contract)
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp="2026-09-02T12:01:00.000000Z",
        proposal_name="v2-capture-surface",
    )

    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    accepted, prepared, fixture, policy, _contract = _source_fixture(
        runtime_root,
        include_terminal=True,
    )
    interface = accepted_interface()
    provider = accepted_provider()
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[interface.path] = render_provider_interface(interface.registration)
    tree[provider_path(provider.provider.identity.name)] = render_provider(provider.provider)
    tree[accepted.path] = render_procedure(accepted.procedure)
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp="2026-09-02T12:02:00.000000Z",
        proposal_name="v2-capture-producers",
    )

    journal_root = instance.root / instance.descriptor.storage.exhaust / "procedure-runs"
    journal_root.mkdir()
    fixture.journal = LocalJournalBackend(journal_root)
    fixture.bodies = instance.body_store()
    head = fixture.journal.read_head(
        prepared.admission.journal_stream,
        prepared.admission.journal_partition_id,
    )
    fixture.journal.activate_writer(
        prepared.admission.journal_stream,
        prepared.admission.journal_partition_id,
        fencing_token="writer",
        expected_head=head,
    )
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    rung = compute_effective_rung(
        procedure_terminal_capability=accepted.procedure.definition.terminal_capability,
        requested_terminal_rung=0,
        selector_privacies={},
        taint_labels=(),
        mandate_grants={},
        calibration_caps=(),
        evaluation_time=prepared.admission.occurrence_evaluation_time,
        procedure_definition_digest=prepared.admission.definition_digest,
        line_spec_digest=prepared.admission.line_spec_digest or "",
        sensitivity_policy_digest=prepared.admission.sensitivity_policy_digest or "",
        mandate_coordinate_digest=prepared.admission.mandate_coordinate_digest or "",
        calibration_coordinate_digest=prepared.admission.calibration_coordinate_digest or "",
    )
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=_SourceInvoker(
            observed_at=prepared.admission.occurrence_evaluation_time
        ),
        provider_classifier_registry=registry,
        capture_contracts={contract_digest: contract},
        acquisition_policy=policy,
        effective_rung=rung,
        egress_sink=CaptureTerminalEgressSink(
            store=fixture.bodies,
            contracts={contract_digest: contract},
            producer=accepted.procedure.identity,
            producer_binding_digest=accepted.artifact_digest,
        ),
    ).execute(prepared, accepted)
    assert result.status == "succeeded"
    records, payloads = _payloads(prepared, fixture)
    kinds = [stored.record.event_kind for stored in records]
    produced = payloads[kinds.index("produced_capture")]
    terminal = payloads[kinds.index("terminal_egress")]
    assert isinstance(produced, dict) and isinstance(terminal, dict)
    terminal_receipt = TerminalEgressReceiptV2.model_validate(terminal["receipt"])
    capture_digests = (
        str(produced["capture_digest"]),
        terminal_receipt.children[0].egress_digest,
    )

    actor = AuthenticatedActor(actor_id="owner")
    for index, capture_digest_value in enumerate(capture_digests, start=1):
        claim_id = "CLM-" + str(index) * 32
        coordinator = AuthoringIntentCoordinator(
            instance=instance,
            store=AuthoringIntentStore(instance.root / instance.descriptor.storage.exhaust),
            claim_id_factory=lambda claim_id=claim_id: claim_id,
        )
        payload = ClaimAuthoringPayloadV3(
            statement=_working_payload(occurrence_count=1).statement.model_copy(
                update={"qualifier": f"v2-arm-{index}"}
            ),
            rationale="Exercise the exact live v2 Capture verifier chain.",
            source=ExistingCaptureCitationSourceV1(capture_digest=capture_digest_value),
            citation_role="evidence",
            dependency_drafts=ClaimDependencyDraftsV1(),
        )
        intent = coordinator.create(
            actor=actor,
            payload=payload,
            canonical_timestamp=f"2026-09-02T12:0{index + 2}:00.000000Z",
        ).intent

        lowered = lower_authoring(instance, intent=intent, actor_id=actor.actor_id)
        assert lowered.resolved_authoring["capture_digest"] == capture_digest_value
        submitted = coordinator.submit(intent.intent_id, actor=actor)
        assert submitted.status.proposal_id is not None, (
            None
            if submitted.intent.last_preflight is None
            else submitted.intent.last_preflight.frontier.diagnostics
        )
        _activate(instance, submitted)
        instance.refresh()

        attested_at = datetime(2026, 9, 2, 13, index, tzinfo=UTC)
        appended = service_append_claim_attestation(
            instance,
            request=_request(
                instance,
                owner,
                claim_id,
                tmp_path,
                basis="new_capture",
                captures=(capture_digest_value,),
                attested_at=attested_at,
            ),
            actor_id="owner",
            recorded_at=attested_at,
        )
        assert appended.submitted_by == "owner"

    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_coordinate() == instance.accepted_coordinate()
