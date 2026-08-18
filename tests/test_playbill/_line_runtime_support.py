"""Shared exact fixtures for PC-E2 Line run admission and execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from cruxible_core.playbill.acquisition_policies import (
    AcquisitionCandidateV1,
    IndependentCoherenceV1,
    InputAcquisitionRuleV1,
    SourceAcquisitionPolicyV1,
    acquisition_policy_digest,
)
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.artifacts import (
    ArtifactAuthority,
    ArtifactIdentity,
    ArtifactPin,
)
from cruxible_core.playbill.canonical import (
    ArtifactDigest,
    GenerationRoot,
    SemanticRoot,
    Sha256Value,
    typed_digest,
)
from cruxible_core.playbill.capture_journal import CaptureLandingEventV1
from cruxible_core.playbill.captures import (
    CanonicalDurationV1,
    CaptureEnvelopeV1,
    CaptureRunCoordinateV1,
    CaptureSelectionBudgetV1,
    capture_contract_digest,
)
from cruxible_core.playbill.cas import ContentAddressedBodyStore
from cruxible_core.playbill.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    LocalJournalBackend,
    VerifiedExhaustRecordV1,
)
from cruxible_core.playbill.lines import (
    LineDeploymentV1,
    LineJournalBindingV1,
    LineLeaseV1,
    LineRunnerIdentityV1,
    acquire_line_lease,
    bind_line_deployment,
)
from cruxible_core.playbill.occurrences import CadenceOccurrenceV1
from cruxible_core.playbill.procedures.acquisition import ExternalSourceAcquirer
from cruxible_core.playbill.procedures.artifacts import (
    AcceptedProcedureV1,
    ProcedureArtifactV1,
    procedure_artifact_digest,
    procedure_path,
)
from cruxible_core.playbill.procedures.closure import LineSlotBindingV1
from cruxible_core.playbill.procedures.graph import compute_procedure_definition_digest_v3
from cruxible_core.playbill.procedures.line_specs import (
    AcceptedLineSpecV1,
    CadenceTriggerPolicyV1,
    LineSpecV1,
    line_spec_digest,
    line_spec_path,
)
from cruxible_core.playbill.procedures.models import (
    ExhaustTapNodeV3,
    GuardNodeV3,
    GuardPredicateV1,
    InboxEgressNodeV3,
    PredicateOperandV1,
    ProcedureBudgetV3,
    ProcedureDefinitionV3,
    ProcedureHardCapsV3,
    ProcedureNodeV3,
    ProcedurePinSlotRefV1,
    ProcedurePinSlotV1,
    SourceNodeV3,
    StateTapNodeV3,
    TransformNodeV3,
)
from cruxible_core.playbill.procedures.run_index import ProcedureRunIndex
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.providers import provider_digest
from cruxible_core.playbill.run_inputs import (
    LineDeploymentBindingSnapshotV1,
    ProcedureMandateReadV1,
    ProcedureSensitivityPolicyV1,
    build_deployment_binding_snapshot,
    build_sensitivity_policy,
    provider_binding_snapshot,
)
from cruxible_core.playbill.source_readers import (
    FakeVersionedExternalSourceReader,
    ProducerBindingV1,
)
from tests.test_playbill._pc_c_support import capture_contract, provider

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
LINE_ID = "orders-triage"
INSTANCE_ID = "instance-a"
ADAPTER_DIGEST = typed_digest(Sha256Value, "playbill-e2-test-v1", {"value": "adapter"}).tagged
SOURCE_IDENTITY = "commerce.production.orders"
SOURCE_COORDINATE = {"lsn": "0/16B6C50"}
SOURCE_SELECTOR = {"table": "orders"}


def digest(label: str) -> str:
    return typed_digest(ArtifactDigest, "playbill-e2-test-v1", {"label": label}).tagged


def raw_digest(label: str) -> str:
    return typed_digest(Sha256Value, "playbill-e2-raw-v1", {"label": label}).value


def pin(role: str, kind: str, name: str, *, artifact_digest: str | None = None) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=ArtifactIdentity(kind=kind, name=name),
        artifact_digest=artifact_digest or digest(name),
    )


def coordinate() -> AcceptedCoordinate:
    return AcceptedCoordinate(
        git_oid="c" * 40,
        semantic_root=typed_digest(
            SemanticRoot, "playbill-e2-semantic-v1", {"value": "accepted"}
        ).tagged,
        generation_root=typed_digest(
            GenerationRoot, "playbill-e2-generation-v1", {"value": "accepted"}
        ).tagged,
        compiler_digest=digest("compiler"),
    )


def actor() -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="service_account",
        actor_id="line-runner",
        org_id=INSTANCE_ID,
        operation_id="operation-a",
        timestamp=NOW,
    )


CAPTURE_CONTRACT = capture_contract()
SOURCE_PROVIDER = provider(CAPTURE_CONTRACT)
CAPTURE_PIN = pin(
    "capture-contract",
    "CaptureContract",
    CAPTURE_CONTRACT.identity.name,
    artifact_digest=capture_contract_digest(CAPTURE_CONTRACT).tagged,
)
PROVIDER_PIN = pin(
    "provider",
    "Provider",
    SOURCE_PROVIDER.identity.name,
    artifact_digest=provider_digest(SOURCE_PROVIDER).tagged,
)
PRODUCER_BINDING = ProducerBindingV1(
    provider=SOURCE_PROVIDER.identity,
    logical_source_identity=SOURCE_IDENTITY,
    adapter_digest=ADAPTER_DIGEST,
)

QUERY_INTERFACE = digest("query-interface")
QUERY_PIN = pin("query", "QueryDefinition", "open-orders")
REDUCER_PIN = pin("reducer", "Reducer", "count-records")
CONTRACT_IN = pin("contract-in", "Contract", "run-input")
CONTRACT_OUT = pin("contract-out", "Contract", "run-output")
FILTER_IN = pin("contract-in", "Contract", "filter-input")
FILTER_OUT = pin("contract-out", "Contract", "filter-output")
INTERFACE_DIGESTS = {QUERY_PIN.artifact_digest: QUERY_INTERFACE}


class StateReader:
    def __init__(self, value: object | None = None) -> None:
        self.value = value or {
            "items": [
                {"id": "a", "status": "open"},
                {"id": "b", "status": "closed"},
            ]
        }
        self.calls: list[tuple[ArtifactPin, object, AcceptedCoordinate]] = []

    def read_accepted_state(
        self,
        *,
        query: ArtifactPin,
        parameters: object,
        coordinate: AcceptedCoordinate,
    ) -> object:
        self.calls.append((query, parameters, coordinate))
        return self.value


class Contracts:
    def validate_contract(
        self,
        *,
        contract: ArtifactPin,
        payload: object,
        direction: Literal["input", "output"],
    ) -> object:
        assert direction in {"input", "output"}
        return payload


class Authority:
    def __init__(self, current: str) -> None:
        self.current = current

    def current_procedure_digest(
        self,
        identity: ArtifactIdentity,
        *,
        coordinate: AcceptedCoordinate,
    ) -> str | None:
        return self.current


class FixedClock:
    def __init__(self, now: datetime = NOW) -> None:
        self._now = now
        self._ticks = 0

    def now(self) -> datetime:
        return self._now

    def monotonic_ns(self) -> int:
        self._ticks += 1
        return self._ticks


class CountingReducer:
    """One deterministic exhaust reducer pinned by its own declared digest."""

    def __init__(self, reducer_digest: str = REDUCER_PIN.artifact_digest) -> None:
        self._digest = reducer_digest

    @property
    def reducer_digest(self) -> str:
        return self._digest

    def reduce(self, records: tuple[VerifiedExhaustRecordV1, ...]) -> object:
        return {
            "kinds": sorted({item.event_kind for item in records}),
            "record_count": len(records),
        }


def _budget() -> ProcedureBudgetV3:
    return ProcedureBudgetV3(
        wall_clock=CanonicalDurationV1(microseconds=5_000_000),
        max_provider_calls=0,
        max_capture_bytes=65_536,
        max_items=100,
    )


def _hard_caps() -> ProcedureHardCapsV3:
    return ProcedureHardCapsV3(
        max_wall_clock=CanonicalDurationV1(microseconds=10_000_000),
        max_provider_calls=1,
        max_capture_bytes=131_072,
        max_items=200,
        max_repeat_attempts=2,
    )


def source_node(*, next_node: str) -> SourceNodeV3:
    return SourceNodeV3(
        node_id="fetch",
        capture_contract=CAPTURE_PIN,
        provider=PROVIDER_PIN,
        request={
            "coordinate": SOURCE_COORDINATE,
            "coordinate_type": "postgres-lsn-v1",
            "materialization": "cas",
            "selector": SOURCE_SELECTOR,
            "selector_type": "relation-primary-key-v1",
        },
        as_="orders",
        next=next_node,
    )


def default_nodes() -> tuple[ProcedureNodeV3, ...]:
    """One graph exercising all three input planes plus a branch and a fanout."""

    return (
        StateTapNodeV3(
            node_id="read",
            query=ProcedurePinSlotRefV1(slot_name="query"),
            parameters={"status": "open"},
            as_="rows",
            next="tap",
        ),
        ExhaustTapNodeV3(
            node_id="tap",
            reducer_or_query=REDUCER_PIN,
            journal_identity="prior-runs",
            as_="receipts",
            next="fetch",
        ),
        source_node(next_node="pick"),
        TransformNodeV3(
            node_id="pick",
            transform_kind="filter_items",
            contract_in=FILTER_IN,
            contract_out=FILTER_OUT,
            spec={"items": "$steps.rows.items", "where": {"status": "open"}},
            as_="picked",
            next="gate",
        ),
        GuardNodeV3(
            node_id="gate",
            predicate=GuardPredicateV1(
                left=PredicateOperandV1(kind="count", alias="picked"),
                operator="gt",
                right=PredicateOperandV1(kind="literal", value=0),
            ),
            on_true="emit",
            on_false="$abort",
            refusal_code="no-open-orders",
            message="No open order rows survived the filter.",
        ),
        InboxEgressNodeV3(node_id="emit", input={"items": "$steps.picked.items"}),
    )


def accepted_procedure(
    *,
    name: str = "orders-triage",
    nodes: tuple[ProcedureNodeV3, ...] | None = None,
    returns: str = "picked",
) -> AcceptedProcedureV1:
    definition = ProcedureDefinitionV3(
        name=name,
        contract_in=CONTRACT_IN,
        contract_out=CONTRACT_OUT,
        nodes=nodes if nodes is not None else default_nodes(),
        returns=returns,
        pin_slots=(
            ProcedurePinSlotV1(
                slot_name="query",
                pin_role="query",
                artifact_kind="QueryDefinition",
                interface_digest=QUERY_INTERFACE,
            ),
        ),
        budget=_budget(),
        hard_caps=_hard_caps(),
        terminal_capability=1,
    )
    pins = tuple(
        sorted(
            {
                CONTRACT_IN,
                CONTRACT_OUT,
                FILTER_IN,
                FILTER_OUT,
                REDUCER_PIN,
                CAPTURE_PIN,
                PROVIDER_PIN,
            },
            key=lambda item: (item.role, item.target.qualified, item.artifact_digest),
        )
    )
    procedure = ProcedureArtifactV1(
        identity=ArtifactIdentity(kind="Procedure", name=name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest_v3(definition).tagged,
        authority=ArtifactAuthority(
            propose_roles=("procedure-author",),
            approve_roles=("procedure-reviewer",),
        ),
        pins=pins,
        activation_policy="drain",
    )
    return AcceptedProcedureV1(
        path=procedure_path(name),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )


def acquisition_policy(
    *,
    requirement: Literal["required", "optional", "conservative_default"] = "required",
    on_unavailable: Literal["refuse", "omit_optional", "declared_conservative_default"] = "refuse",
    conservative_default: object | None = None,
) -> SourceAcquisitionPolicyV1:
    fallback = "refuse" if requirement == "required" else on_unavailable
    return SourceAcquisitionPolicyV1(
        identity=ArtifactIdentity(kind="SourceAcquisitionPolicy", name="orders-acquisition"),
        inputs=(
            InputAcquisitionRuleV1(
                input_name="orders",
                requirement=requirement,
                permitted_replayability=("exact",),
                on_unavailable=on_unavailable,
                on_stale=fallback,  # type: ignore[arg-type]
                on_oversized=fallback,  # type: ignore[arg-type]
                on_conflict="refuse",
                conservative_default=conservative_default,
            ),
        ),
        coherence=IndependentCoherenceV1(),
        authority=ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",)),
    )


def accepted_line(
    accepted: AcceptedProcedureV1,
    policy: SourceAcquisitionPolicyV1,
    *,
    epsilon: str = "0.1",
) -> AcceptedLineSpecV1:
    procedure_pin = pin(
        "procedure",
        "Procedure",
        accepted.procedure.identity.name,
        artifact_digest=accepted.artifact_digest,
    )
    policy_pin = pin(
        "acquisition-policy",
        "SourceAcquisitionPolicy",
        policy.identity.name,
        artifact_digest=acquisition_policy_digest(policy).tagged,
    )
    cadence = CadenceTriggerPolicyV1(cadence_policy_digest=digest("hourly"))
    cadence_pin = pin(
        "trigger-cadence-policy",
        "Policy",
        "hourly",
        artifact_digest=cadence.cadence_policy_digest,
    )
    line = LineSpecV1(
        identity=ArtifactIdentity(kind="Line", name=LINE_ID),
        occurrence_epoch=1,
        procedure=procedure_pin,
        parameters={"status": "open"},
        slot_bindings=(LineSlotBindingV1(slot_name="query", artifact_pin=QUERY_PIN),),
        trigger_policy=cadence,
        acquisition_policy=policy_pin,
        requested_terminal_rung=1,
        budgets={
            "max_capture_bytes": 65_536,
            "max_items": 100,
            "max_provider_calls": 0,
            "max_wall_clock_microseconds": 5_000_000,
        },
        epsilon={"$decimal": epsilon},
        authority=ArtifactAuthority(
            propose_roles=("line-author",),
            approve_roles=("line-reviewer",),
        ),
        pins=tuple(
            sorted(
                {procedure_pin, policy_pin, cadence_pin, QUERY_PIN},
                key=lambda item: (item.role, item.target.qualified, item.artifact_digest),
            )
        ),
    )
    return AcceptedLineSpecV1(
        path=line_spec_path(LINE_ID),
        line=line,
        artifact_digest=line_spec_digest(line).tagged,
    )


@dataclass
class LineRuntimeFixture:
    journal: LocalJournalBackend
    bodies: ContentAddressedBodyStore
    captures: ContentAddressedBodyStore
    run_index: ProcedureRunIndex
    deployment: LineDeploymentV1
    lease: LineLeaseV1
    stream: JournalStreamIdentityV1
    reader: FakeVersionedExternalSourceReader
    acquirer: ExternalSourceAcquirer
    accepted_line: AcceptedLineSpecV1
    accepted_procedure: AcceptedProcedureV1
    policy: SourceAcquisitionPolicyV1
    root: Path

    @property
    def run_partition(self) -> str:
        return self.deployment.journal_binding.run_partition_id

    def run_records(self) -> tuple[object, ...]:
        return self.journal.all_records(self.stream, self.run_partition)

    def binding_snapshot(self) -> LineDeploymentBindingSnapshotV1:
        return build_deployment_binding_snapshot(
            self.deployment,
            provider_bindings=(
                provider_binding_snapshot(
                    binding=PRODUCER_BINDING,
                    provider_artifact_digest=PROVIDER_PIN.artifact_digest,
                    contract=CAPTURE_CONTRACT,
                ),
            ),
        )

    def sensitivity(self) -> ProcedureSensitivityPolicyV1:
        return build_sensitivity_policy({"orders": CAPTURE_CONTRACT})


def build_fixture(
    tmp_path: Path,
    *,
    accepted: AcceptedProcedureV1 | None = None,
    policy: SourceAcquisitionPolicyV1 | None = None,
    seed_source: bool = True,
) -> LineRuntimeFixture:
    accepted = accepted or accepted_procedure()
    policy = policy or acquisition_policy()
    line = accepted_line(accepted, policy)

    journal_root = tmp_path / "journal"
    journal_root.mkdir(mode=0o700, parents=True)
    bodies_root = tmp_path / "cas"
    bodies_root.mkdir(mode=0o700, parents=True)
    captures_root = tmp_path / "captures"
    captures_root.mkdir(mode=0o700, parents=True)
    journal = LocalJournalBackend(journal_root)
    bodies = ContentAddressedBodyStore(bodies_root)
    captures = ContentAddressedBodyStore(captures_root)

    stream = JournalStreamIdentityV1(
        instance_id=INSTANCE_ID,
        journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
        stream_id="lines",
    )
    reader = FakeVersionedExternalSourceReader()
    if seed_source:
        reader.seed(
            source_identity=SOURCE_IDENTITY,
            coordinate_type="postgres-lsn-v1",
            coordinate=SOURCE_COORDINATE,
            selector_type="relation-primary-key-v1",
            selector=SOURCE_SELECTOR,
            value=[{"order_id": "o-1"}, {"order_id": "o-2"}],
        )
    acquirer = ExternalSourceAcquirer(
        reader=reader,
        store=captures,
        contracts={CAPTURE_PIN.artifact_digest: CAPTURE_CONTRACT},
        providers={PROVIDER_PIN.artifact_digest: SOURCE_PROVIDER},
        bindings={PROVIDER_PIN.artifact_digest: PRODUCER_BINDING},
        budgets={"orders": CaptureSelectionBudgetV1(max_bytes=4096, max_rows=4, max_items=4)},
    )
    deployment = bind_line_deployment(
        line,
        deployment_id="deployment-a",
        runner=LineRunnerIdentityV1(runner_id="runner-a"),
        journal_binding=LineJournalBindingV1(
            logical_stream=stream,
            control_partition_id="line-control",
            run_partition_id="line-runs",
            backend_id="local-a",
        ),
        activated_at=NOW,
    )
    lease = acquire_line_lease(journal, deployment, fencing_token="writer-a", acquired_at=NOW)
    return LineRuntimeFixture(
        journal=journal,
        bodies=bodies,
        captures=captures,
        run_index=ProcedureRunIndex(tmp_path / "run-index.sqlite"),
        deployment=deployment,
        lease=lease,
        stream=stream,
        reader=reader,
        acquirer=acquirer,
        accepted_line=line,
        accepted_procedure=accepted,
        policy=policy,
        root=tmp_path,
    )


def cadence_occurrence(*, tick_index: int = 0) -> CadenceOccurrenceV1:
    return CadenceOccurrenceV1(
        line_id=LINE_ID,
        occurrence_epoch=1,
        cadence_policy_digest=digest("hourly"),
        tick_index=tick_index,
    )


def mandate_read() -> ProcedureMandateReadV1:
    return ProcedureMandateReadV1(
        accepted_coordinate=coordinate(),
        requested_basis_digests=(),
        resolved_basis_digests=(),
        evaluated_at=NOW,
    )


def landing_event(
    capture_digest_value: str,
    *,
    sequence: int = 1,
) -> CaptureLandingEventV1:
    event_id = raw_digest(f"landing-{sequence}")
    return CaptureLandingEventV1(
        instance_id=INSTANCE_ID,
        partition_id=raw_digest("orders-partition"),
        sequence=sequence,
        event_id=event_id,
        idempotency_key=event_id,
        capture_digest=capture_digest_value,
        capture_contract_digest=capture_contract_digest(CAPTURE_CONTRACT).tagged,
        run_coordinate=CaptureRunCoordinateV1(
            run_kind="provider",
            run_id="landing-run-1",
            bound_generation=coordinate().generation_root,
            executable_identity=SOURCE_PROVIDER.identity,
            executable_digest=PROVIDER_PIN.artifact_digest,
        ),
        run_receipt_digest=digest("landing-receipt"),
        producer_binding_digest=PRODUCER_BINDING.digest,
        previous_event_digest=None,
        landed_at=NOW,
    )


def landed_candidate(
    *,
    envelope: CaptureEnvelopeV1,
    capture_digest_value: str,
    sequence: int = 1,
) -> AcquisitionCandidateV1:
    return AcquisitionCandidateV1(
        input_name="orders",
        envelope=envelope,
        capture_digest=capture_digest_value,
        landing_event=landing_event(capture_digest_value, sequence=sequence),
        current_replay_available=True,
        selection_budget=CaptureSelectionBudgetV1(max_bytes=4096, max_rows=4, max_items=4),
        selected_bytes=64,
        selected_rows=2,
        selected_items=2,
    )


__all__ = [
    "ADAPTER_DIGEST",
    "CAPTURE_CONTRACT",
    "CAPTURE_PIN",
    "CONTRACT_IN",
    "CONTRACT_OUT",
    "FILTER_IN",
    "FILTER_OUT",
    "INSTANCE_ID",
    "INTERFACE_DIGESTS",
    "LINE_ID",
    "NOW",
    "PRODUCER_BINDING",
    "PROVIDER_PIN",
    "QUERY_PIN",
    "REDUCER_PIN",
    "SOURCE_COORDINATE",
    "SOURCE_IDENTITY",
    "SOURCE_PROVIDER",
    "SOURCE_SELECTOR",
    "Authority",
    "Contracts",
    "CountingReducer",
    "FixedClock",
    "LineRuntimeFixture",
    "StateReader",
    "accepted_line",
    "accepted_procedure",
    "acquisition_policy",
    "actor",
    "build_fixture",
    "cadence_occurrence",
    "coordinate",
    "default_nodes",
    "digest",
    "landed_candidate",
    "landing_event",
    "mandate_read",
    "pin",
    "raw_digest",
    "source_node",
]
