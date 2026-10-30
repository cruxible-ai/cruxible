"""Live end-to-end coverage for every production Capture verifier caller."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cruxible_client.contracts.authoring.models import (
    ClaimAuthoringPayload,
    ClaimDependencyDrafts,
    ExistingCaptureCitationSource,
)
from cruxible_client.contracts.capture_contract_inputs import (
    CaptureContractInputRecord,
    lower_capture_contract_input,
)
from cruxible_client.contracts.captures import (
    CaptureContract,
    capture_components,
    capture_contract_digest,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
)
from cruxible_client.contracts.procedures.artifacts import render_procedure
from cruxible_client.contracts.providers import provider_path, render_provider
from cruxible_client.contracts.subjects import render_subject, subject_path
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.lowering import lower_authoring
from cruxible_core.authoring.store import AuthoringIntentStore
from cruxible_core.exhaust import LocalJournalBackend
from cruxible_core.procedures.egress import (
    CaptureTerminalEgressSink,
    TerminalEgressReceiptV2,
    compute_effective_rung,
)
from cruxible_core.procedures.execution import ProcedureExecutor
from cruxible_core.procedures.reference_binding import authored_procedure
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.evidence.claim_attestations import service_append_claim_attestation
from tests.core_support._light_capture_support import record_contract as capture_contract
from tests.core_support._support import generate_client
from tests.test_authoring.test_authoring_existing_capture import _activate
from tests.test_authoring.test_authoring_preflight import _working_payload
from tests.test_claims.test_claim_attestation_service import _request
from tests.test_claims.test_claims import _claim_type, _subject
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_procedures.p2b4_unit1.test_source_v4_runtime import (
    _acquisition_rules,
    _payloads,
    _source_fixture,
    _source_provider,
    _SourceInvoker,
    source_contract_files,
)
from tests.test_procedures.test_procedure_execution import _Authority, _Contracts


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


@dataclass(frozen=True)
class ProducedCaptures:
    """An instance whose accepted Procedure ran once and journaled what it produced."""

    instance: PlaybillInstance
    owner: object
    contract: CaptureContract
    terminal_contract: CaptureContract
    records: tuple[Any, ...]
    payloads: tuple[Any, ...]
    run_id: str

    def contract_for(self, envelope):
        return (
            self.contract
            if envelope.capture_contract_digest == capture_contract_digest(self.contract).tagged
            else self.terminal_contract
        )

    def payload(self, event_kind: str) -> dict[str, Any]:
        kinds = [stored.record.event_kind for stored in self.records]
        value = self.payloads[kinds.index(event_kind)]
        assert isinstance(value, dict)
        return value


def run_capture_producers(
    tmp_path: Path,
    *,
    include_terminal: bool = True,
) -> ProducedCaptures:
    """Accept a Source Procedure under an admitting ClaimType and run it once.

    The Source acquisition journals ``produced_capture`` when its rule selects
    the read; with ``include_terminal`` an ``emit_capture`` terminal emits a v2
    Procedure Capture of the same result.
    """

    instance, owner = _instance(tmp_path)
    contract = capture_contract()
    terminal_contract = lower_capture_contract_input(
        CaptureContractInputRecord(
            name="test.procedure-output", shape="procedure_output", max_bytes=4096
        ),
        named=True,
    )
    contract_digest = capture_contract_digest(contract).tagged
    shell = _subject()
    claim_type = _claim_type().model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
                rules=(
                    ClaimEvidenceAdmissionRule(
                        rule_id="live-v2-capture",
                        claim_roles=("normative", "observation"),
                        capture_contracts=tuple(
                            sorted((contract.identity.name, terminal_contract.identity.name))
                        ),
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
    tree[capture_contract_path(terminal_contract.identity.name)] = render_capture_contract(
        terminal_contract
    )
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
        include_terminal=include_terminal,
        contract=contract,
        terminal_contract=terminal_contract,
    )
    provider = _source_provider()
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree.update(source_contract_files())
    tree[provider_path(provider.provider.identity.name)] = render_provider(provider.provider)
    tree[accepted.path] = render_procedure(authored_procedure(accepted))
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
    rung = compute_effective_rung(
        procedure_terminal_capability=accepted.procedure.definition.terminal_capability,
        requested_terminal_rung=0,
        selector_privacies={},
        taint_labels=(),
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
            observed_at=prepared.admission.occurrence_evaluation_time,
            coordinate_type=capture_components(contract).coordinate_schema_pins[0].target.name,
        ),
        capture_contracts={
            contract_digest: contract,
            capture_contract_digest(terminal_contract).tagged: terminal_contract,
        },
        acquisition_rules=_acquisition_rules(policy),
        effective_rung=rung,
        egress_sink=CaptureTerminalEgressSink(
            store=fixture.bodies,
            contracts={
                contract_digest: contract,
                capture_contract_digest(terminal_contract).tagged: terminal_contract,
            },
            producer=accepted.procedure.identity,
            producer_binding_digest=accepted.artifact_digest,
        ),
    ).execute(prepared, accepted)
    if include_terminal:
        assert result.status == "succeeded"
    records, payloads = _payloads(prepared, fixture)
    return ProducedCaptures(
        instance=instance,
        owner=owner,
        contract=contract,
        terminal_contract=terminal_contract,
        records=tuple(records),
        payloads=tuple(payloads),
        run_id=prepared.admission.run_id,
    )


def test_both_v2_producer_arms_verify_through_every_live_production_consumer(
    tmp_path: Path,
) -> None:
    produced_world = run_capture_producers(tmp_path)
    instance, owner = produced_world.instance, produced_world.owner
    records, payloads = produced_world.records, produced_world.payloads
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
        payload = ClaimAuthoringPayload(
            statement=_working_payload(occurrence_count=1).statement.model_copy(
                update={"qualifier": f"v2-arm-{index}"}
            ),
            rationale="Exercise the exact live v2 Capture verifier chain.",
            source=ExistingCaptureCitationSource(capture_digest=capture_digest_value),
            citation_role="evidence",
            dependency_drafts=ClaimDependencyDrafts(),
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
