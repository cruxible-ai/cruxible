"""Authenticated Procedure execution and log-sufficiency laws."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest

import cruxible_client.contracts.procedures.results as procedure_result_contracts
import cruxible_core.procedures.execution as execution_module
import cruxible_core.service.procedures.procedure_runs as procedure_run_service
from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    GenerationRoot,
    SemanticRoot,
    canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.captures import (
    CanonicalDuration,
    CaptureRetentionErasurePolicy,
)
from cruxible_client.contracts.errors import ExecutionError
from cruxible_client.contracts.procedures.acquisition_rules import (
    ACQUISITION_RULES_DIGEST_DOMAIN,
    AcquisitionInputDecision,
)
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    ProcedureArtifact,
    ProcedureOwnedContract,
    parse_procedure,
    procedure_artifact_digest,
    procedure_owned_contract_digest,
    procedure_path,
)
from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_client.contracts.procedures.contracts import (
    OwnedProcedureContractValidator,
    ValidatedProcedureContract,
)
from cruxible_client.contracts.procedures.graph import (
    ProcedureGraphFormatError,
    compute_procedure_definition_digest,
)
from cruxible_client.contracts.procedures.line_specs import (
    AcceptedLineSpec,
    LineSpec,
    line_spec_digest,
    line_spec_path,
)
from cruxible_client.contracts.procedures.models import (
    CallNode,
    GuardNode,
    GuardPredicate,
    HaltNode,
    PredicateOperand,
    ProcedureBudget,
    ProcedureDefinition,
    ProcedureHardCaps,
    ProjectNode,
    ReadNode,
    RepeatBodyNode,
    RepeatNode,
    TransformNode,
)
from cruxible_client.contracts.procedures.results import (
    ProcedureAcquisitionPlan,
    ProcedureAdmissionRefusal,
    ProcedureProviderBinding,
    ProcedureRunReceipt,
    procedure_acquisition_plan_digest,
)
from cruxible_client.contracts.procedures.windows import BoundObservationWindow
from cruxible_client.contracts.query.grammar import QueryBudgets
from cruxible_client.contracts.query.results import (
    ClaimQueryResult,
    QueryProjectedField,
    QueryResultRow,
    QueryRowBinding,
    QueryTruncation,
)
from cruxible_client.contracts.resolution_contracts import (
    ClaimVersionReference,
    InvestigationBinding,
    ResolutionContractReference,
)
from cruxible_core.compiler.compiler import current_compiler_coordinate
from cruxible_core.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    LocalJournalBackend,
    parse_journal_payload,
)
from cruxible_core.exhaust.line_track_records import LineTrackRecordReducer
from cruxible_core.exhaust.promotions import VerifiedExhaustRecordV1
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.indexes.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.indexes.typed_state import TypedStateReader
from cruxible_core.procedures.execution import (
    ExhaustRunMaterialV1,
    LandedCaptureRunMaterialV1,
    PreparedProcedureRun,
    ProcedureAdmissionBoundPayload,
    ProcedureAdmissionMaterialManifest,
    ProcedureAdmissionMaterialMember,
    ProcedureExecutor,
    ProcedureParentBindingV1,
    ProcedureResourceBudgetV1,
    ProcedureRunAdmission,
    ProcedureRunRefusalV1,
    ProcedureSelectionDecision,
    StateTapReadResultV1,
    _check_return_budget,
    _RunRefusal,
    admission_fields,
    capture_admission_material_member,
    parse_admission_payload,
    prepare_direct_procedure_run,
    procedure_admission_digest,
    procedure_admission_material_digest,
    procedure_direct_partition,
    procedure_line_journal_stream,
    procedure_line_partition,
    procedure_replay_input_vector,
    procedure_selection_decision_digest,
    procedure_semantic_replay_key_digest,
    procedure_semantic_result_digest,
    procedure_semantic_run_id,
    seal_procedure_run_admission,
    verify_line_admission_spec,
)
from cruxible_core.procedures.input_planes import (
    ExhaustRunInputV1,
    LandedCaptureRunInputV1,
)
from cruxible_core.procedures.run_index import ProcedureRunIndex
from cruxible_core.service.procedures.procedure_runs import service_prepare_playbill_line_admission
from cruxible_core.storage.cas import BodyAccessContext, ContentAddressedBodyStore
from tests.core_support._support import initialize_local
from tests.support.operation_world import contract_files
from tests.support.procedures import (
    MAX_WALL_CLOCK,
    RUN_WALL_CLOCK,
    accepted_artifact,
    accepted_procedure,
    bind_fixture,
    named,
)

NOW = datetime(2026, 8, 17, 13, 0, tzinfo=timezone.utc)


def _digest(label: str) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-procedure-execution-test-v1",
        {"label": label},
    ).tagged


def _pin(role: str, kind: str, name: str) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=ArtifactIdentity(kind=kind, name=name),
        artifact_digest=_digest(name),
    )


def _coordinate() -> AcceptedCoordinate:
    return AcceptedCoordinate(
        git_oid="a" * 40,
        semantic_root=typed_digest(
            SemanticRoot,
            "playbill-procedure-execution-semantic-v1",
            {"value": "accepted"},
        ).tagged,
        generation_root=typed_digest(
            GenerationRoot,
            "playbill-procedure-execution-generation-v1",
            {"value": "accepted"},
        ).tagged,
        compiler_digest=_digest("compiler"),
    )


def _actor() -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id="operator",
        org_id="instance-a",
        operation_id="operation-a",
        timestamp=NOW,
    )


def _budget(*, providers: int = 0, items: int = 100) -> ProcedureBudget:
    return ProcedureBudget(
        wall_clock=RUN_WALL_CLOCK,
        max_provider_calls=providers,
        max_capture_bytes=0,
        max_items=items,
    )


def _hard_caps(*, providers: int = 0, items: int = 100) -> ProcedureHardCaps:
    return ProcedureHardCaps(
        max_wall_clock=MAX_WALL_CLOCK,
        max_provider_calls=providers,
        max_capture_bytes=0,
        max_items=items,
        max_repeat_attempts=3,
    )


def _accepted(
    definition: ProcedureDefinition,
    *,
    pins: tuple[ArtifactPin, ...] = (),
    activation_policy: str = "abort",
) -> AcceptedProcedure:
    """One accepted Procedure whose Contract pins resolve to carried Contracts.

    ``pins`` adds non-Contract envelope pins; every Contract the graph pins is
    rewritten to a permissive owned Contract of the same name.
    """

    return accepted_procedure(
        definition,
        extra_pins=tuple(pin for pin in pins if pin.target.kind != "Contract"),
        activation_policy=activation_policy,
    )


def _state_procedure(*, false_branch: bool = False, max_items: int = 100) -> AcceptedProcedure:
    contract_in = _pin("contract-in", "Contract", "input")
    contract_out = _pin("contract-out", "Contract", "output")
    query = _pin("query", "QueryDefinition", "accepted-items")
    predicate = GuardPredicate(
        left=PredicateOperand(
            kind="step",
            alias="rows",
            path=("result", "truncation", "returned_result_count"),
        ),
        operator="gt",
        right=PredicateOperand(kind="literal", value=100 if false_branch else 0),
    )
    definition = ProcedureDefinition(
        name="state-procedure",
        contract_in=named(contract_in),
        contract_out=named(contract_out),
        nodes=(
            ReadNode(
                node_id="read",
                query=named(query),
                parameters={"status": "accepted"},
                as_="rows",
                next="gate",
            ),
            GuardNode(
                node_id="gate",
                predicate=predicate,
                on_true="project",
                on_false="$abort",
                refusal_code="no-items",
                message="No accepted items satisfy the query.",
            ),
            ProjectNode(
                node_id="project",
                fields={"items": "$steps.rows.result.rows", "status": "ok"},
                contract_out=named(contract_out),
                as_="result",
            ),
        ),
        returns="result",
        budget=_budget(items=max_items),
        hard_caps=_hard_caps(items=max_items),
        terminal_capability=1,
    )
    return _accepted(definition, pins=(contract_in, contract_out, query))


def _owned_contract(name: str, fields: dict[str, PropertySchema]) -> ProcedureOwnedContract:
    return ProcedureOwnedContract(
        identity=ArtifactIdentity(kind="Contract", name=name),
        schema=ContractSchema(fields=fields),
    )


def _owned_pin(role: str, contract: ProcedureOwnedContract) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=contract.identity,
        artifact_digest=procedure_owned_contract_digest(contract).tagged,
    )


def _owned_accepted(
    definition: ProcedureDefinition,
    *,
    contracts: tuple[ProcedureOwnedContract, ...],
    pins: tuple[ArtifactPin, ...],
) -> AcceptedProcedure:
    procedure = ProcedureArtifact(
        identity=ArtifactIdentity(kind="Procedure", name=definition.name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        owned_contracts=tuple(
            sorted(
                contracts,
                key=lambda contract: canonical_bytes(contract.model_dump(mode="json")),
            )
        ),
        activation_policy="abort",
    )
    return bind_fixture(accepted_artifact(procedure), extra_pins=pins)


def _owned_boundary_procedure(boundary: str) -> AcceptedProcedure:
    """Two items cross a typed list boundary under a one-item budget.

    ``contract-out`` checks the list at a project's own Contract, ``return``
    at the Procedure's return Contract (the project's own is opaque).
    """

    empty = _owned_contract("empty-input", {})
    opaque = _owned_contract("opaque-output", {"items": PropertySchema(type="json")})
    typed = ProcedureOwnedContract(
        identity=ArtifactIdentity(kind="Contract", name=f"list-{boundary}"),
        schema=ContractSchema(
            fields={
                "items": PropertySchema(
                    type="list",
                    item_fields={"id": PropertySchema(type="string")},
                )
            },
        ),
    )
    entry_pin = _owned_pin("contract-in", empty)
    opaque_out = _owned_pin("contract-out", opaque)
    typed_out = _owned_pin("contract-out", typed)
    listed = ProjectNode(
        node_id="listed",
        literal={"items": [{"id": "one"}, {"id": "two"}]},
        contract_out=named(opaque_out),
        as_="listed",
        next="compute",
    )
    compute = ProjectNode(
        node_id="compute",
        fields={"items": "$steps.listed.items"},
        contract_out=named(typed_out if boundary == "contract-out" else opaque_out),
        as_="result",
    )
    definition = ProcedureDefinition(
        name=f"owned-{boundary}",
        contract_in=named(entry_pin),
        contract_out=named(typed_out),
        nodes=(listed, compute),
        returns="result",
        budget=_budget(items=1),
        hard_caps=_hard_caps(items=1),
        terminal_capability=1,
    )
    return _owned_accepted(
        definition, contracts=(empty, opaque, typed), pins=(entry_pin, opaque_out, typed_out)
    )


def _repeat_transform_procedure(*, max_items: int = 100) -> AcceptedProcedure:
    contract_in = _pin("contract-in", "Contract", "input")
    contract_out = _pin("contract-out", "Contract", "output")
    definition = ProcedureDefinition(
        name="repeat-transform-procedure",
        contract_in=named(contract_in),
        contract_out=named(contract_out),
        nodes=(
            RepeatNode(
                node_id="repeat",
                max_attempts=2,
                body=(
                    RepeatBodyNode(
                        node_id="shape-body",
                        operation="transform",
                        transform_kind="shape",
                        spec={
                            "tag": "cruxible-transform-shape-spec-v1",
                            "input": [{"id": "a"}, {"id": "b"}],
                            "fields": {"identifier": {"from": "$item.fields.id"}},
                        },
                        as_="shaped",
                    ),
                ),
                until=GuardPredicate(
                    left=PredicateOperand(kind="exists", alias="shaped"),
                    operator="eq",
                    right=PredicateOperand(kind="literal", value=True),
                ),
                as_="repeated",
            ),
        ),
        returns="repeated",
        budget=_budget(items=max_items),
        hard_caps=_hard_caps(items=max_items),
        terminal_capability=1,
    )
    return _accepted(definition, pins=(contract_in, contract_out))


def _guarded_filter_procedure(
    *,
    operator: str,
    result_next: str | None = None,
) -> AcceptedProcedure:
    """A closed filter, a count guard over what it kept, then a count of it."""

    empty = _owned_contract("guard-empty-input", {})
    count_result = _owned_contract(
        "guard-count-result",
        {"count": PropertySchema(type="int")},
    )
    entry_pin = _owned_pin("contract-in", empty)
    result_pin = _owned_pin("contract-out", count_result)
    definition = ProcedureDefinition(
        name=f"guarded-filter-{operator}{'-explicit-next' if result_next else ''}",
        contract_in=named(entry_pin),
        contract_out=named(result_pin),
        nodes=(
            TransformNode.model_validate(
                {
                    "node_id": "filter",
                    "transform_kind": "filter",
                    "spec": {
                        "tag": "cruxible-transform-filter-spec-v1",
                        "input": [
                            {"id": "one", "keep": True},
                            {"id": "two", "keep": False},
                        ],
                        "where": "eq($item.fields.keep, true)",
                    },
                    "as": "filtered",
                    "next": "gate",
                }
            ),
            GuardNode(
                node_id="gate",
                predicate=GuardPredicate(
                    left=PredicateOperand(kind="count", alias="filtered"),
                    operator=operator,  # type: ignore[arg-type]
                    right=PredicateOperand(kind="literal", value=0),
                ),
                on_true="aggregate",
                on_false="stop",
                refusal_code="guard.no_filtered_items",
                message="No filtered items are available.",
            ),
            TransformNode.model_validate(
                {
                    "node_id": "aggregate",
                    "transform_kind": "aggregate",
                    "spec": {
                        "tag": "cruxible-transform-aggregate-spec-v1",
                        "input": "$steps.filtered",
                        "op": "count",
                    },
                    "as": "counted",
                    "next": "result",
                }
            ),
            ProjectNode(
                node_id="result",
                fields={"count": "$steps.counted.result"},
                contract_out=named(result_pin),
                as_="result",
                next=result_next,
            ),
            HaltNode(node_id="stop", reason="No filtered items."),
        ),
        returns="result",
        budget=_budget(),
        hard_caps=_hard_caps(),
        terminal_capability=1,
    )
    return _owned_accepted(
        definition,
        contracts=(empty, count_result),
        pins=(entry_pin, result_pin),
    )


def _guarded_scalar_procedure(
    *,
    alias: str = "summary",
    path: tuple[str, ...] = ("count",),
) -> AcceptedProcedure:
    empty = _owned_contract("scalar-empty-input", {})
    count_result = _owned_contract(
        "scalar-count-result",
        {"count": PropertySchema(type="int")},
    )
    entry_pin = _owned_pin("contract-in", empty)
    count_out = _owned_pin("contract-out", count_result)
    definition = ProcedureDefinition(
        name="guarded-scalar-" + alias + "-" + "-".join(path),
        contract_in=named(entry_pin),
        contract_out=named(count_out),
        nodes=(
            ProjectNode(
                node_id="summarize",
                literal={"count": 1},
                contract_out=named(count_out),
                as_="summary",
                next="gate",
            ),
            GuardNode(
                node_id="gate",
                predicate=GuardPredicate(
                    left=PredicateOperand(kind="step", alias=alias, path=path),
                    operator="gt",
                    right=PredicateOperand(kind="literal", value=0),
                ),
                on_true="emit",
                on_false="stop",
                refusal_code="guard.nonpositive_summary",
                message="The summary count is not positive.",
            ),
            ProjectNode(
                node_id="emit",
                fields="$steps.summary",
                contract_out=named(count_out),
                as_="result",
            ),
            HaltNode(node_id="stop", reason="Summary count was not positive."),
        ),
        returns="result",
        budget=_budget(),
        hard_caps=_hard_caps(),
        terminal_capability=1,
    )
    return _owned_accepted(
        definition,
        contracts=(empty, count_result),
        pins=(entry_pin, count_out),
    )


_QUERY_COORDINATE = AcceptedProjectionCoordinate(
    instance_id="instance-a",
    repository_path="/accepted/ledger.git",
    git_object_format="sha1",
    git_oid="a" * 40,
    semantic_root=_coordinate().semantic_root,
    generation_root=_coordinate().generation_root,
    compiler=current_compiler_coordinate(),
)


def _query_result(value: object) -> ClaimQueryResult:
    """A completed typed query result whose rows project each item's fields.

    A state tap reads accepted state only through a typed query; each item of
    ``value["items"]`` becomes one row whose projected fields are its keys.
    """

    assert isinstance(value, dict)
    items = value["items"]
    assert isinstance(items, list)
    rows = tuple(
        QueryResultRow(
            bindings=(QueryRowBinding(binding="item"),),
            fields=tuple(
                QueryProjectedField(name=name, state="present", value=item[name])
                for name in sorted(item)
            ),
        )
        for item in items
    )
    return ClaimQueryResult(
        verdict="completed",
        definition_path="queries/accepted-items.json",
        definition_digest=_digest("accepted-items-query"),
        parameter_digest=_digest("accepted-items-parameters"),
        coordinate=_QUERY_COORDINATE,
        evaluated_at=NOW,
        budgets=QueryBudgets(max_results=100, max_traversal_depth=0),
        result_shape="subject",
        result_cardinality="many",
        result_binding="item",
        dedupe="subject",
        rows=rows,
        truncation=QueryTruncation(
            candidate_result_count=len(rows),
            returned_result_count=len(rows),
        ),
    )


def _query_rows(value: object) -> list[object]:
    """The canonical rows a state tap over ``value`` exposes at ``result.rows``."""

    return [row.model_dump(mode="json") for row in _query_result(value).rows]


class _StateReader:
    def __init__(self, value=None) -> None:
        self.value = value or {"items": [{"id": "one"}]}
        self.calls = []

    def read_accepted_state(self, *, query, parameters, coordinate, **_kwargs):
        self.calls.append((query, parameters, coordinate))
        return StateTapReadResultV1(
            value=_query_result(self.value).model_dump(mode="json"),
            effective_budgets=QueryBudgets(
                max_results=100,
                max_traversal_depth=0,
            ),
        )


class _Contracts:
    def validate_contract(self, *, contract, payload, direction):
        assert contract.target.kind == "Contract"
        assert direction in {"input", "output"}
        return payload

    def validate_contract_with_budget(self, *, contract, payload, direction, max_items):
        assert max_items >= 1
        return ValidatedProcedureContract(value=payload, list_observations=())

    def unique_list_field_path(self, contract):  # type: ignore[no-untyped-def]
        return None


class _Authority:
    def __init__(self, digest: str) -> None:
        self.digest = digest
        self.calls = 0

    def current_procedure_digest(self, identity, *, coordinate):
        self.calls += 1
        return self.digest


@dataclass
class _Fixture:
    journal: LocalJournalBackend
    bodies: ContentAddressedBodyStore
    stream: JournalStreamIdentityV1
    run_index: ProcedureRunIndex


def _fixture(tmp_path) -> _Fixture:
    journal_root = tmp_path / "journal"
    journal_root.mkdir(mode=0o700)
    cas_root = tmp_path / "cas"
    cas_root.mkdir(mode=0o700)
    journal = LocalJournalBackend(journal_root)
    stream = JournalStreamIdentityV1(
        instance_id="instance-a",
        journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
        stream_id="procedures",
    )
    head = journal.read_head(stream, "runs")
    journal.activate_writer(stream, "runs", fencing_token="writer", expected_head=head)
    return _Fixture(
        journal=journal,
        bodies=ContentAddressedBodyStore(cas_root),
        stream=stream,
        run_index=ProcedureRunIndex(tmp_path / "run-index.sqlite"),
    )


def _prepare(accepted: AcceptedProcedure, fixture: _Fixture, reader: _StateReader, **kwargs):
    return prepare_direct_procedure_run(
        accepted,
        instance_id="instance-a",
        accepted_coordinate=kwargs.get("accepted_coordinate", _coordinate()),
        invocation_input=kwargs.get("invocation_input", {"value": 7}),
        actor_context=kwargs.get("actor_context", _actor()),
        state_reader=reader,
        bodies=fixture.bodies,
        journal_stream=fixture.stream,
        journal_partition_id=kwargs.get("journal_partition_id", "runs"),
        admitted_at=NOW,
        runtime_policy=kwargs.get("runtime_policy"),
    )


def test_a_pure_direct_run_binds_no_line_plan_invocation_or_occurrence(tmp_path) -> None:
    """The one carrier, read for a pure run: nothing about Providers or a Line is invented."""

    fixture = _fixture(tmp_path)
    prepared = _prepare(_state_procedure(), fixture, _StateReader(), journal_partition_id=None)
    admission = prepared.admission
    assert admission.tag == "playbill-procedure-run-admission-v10"
    assert admission.invocation_origin == "actor"
    assert not admission.plans_providers
    assert admission.parent_binding is None
    assert (
        admission.line_identity,
        admission.occurrence_id,
        admission.selection_decision,
        admission.provider_output_bytes_cap,
        admission.deployment_snapshot_digest,
    ) == (None, None, None, None, None)
    assert admission.resolved_provider_bindings == ()
    assert admission.occurrence_evaluation_time == admission.admitted_at
    assert prepared.acquisition_plan is None
    assert prepared.admission_material_manifest.members == ()
    # Its identity is derived, never supplied: the direct lane's run id and partition.
    assert admission.run_id == procedure_semantic_run_id(admission.semantic_replay_key_digest)
    assert admission.journal_partition_id == procedure_direct_partition(
        admission.semantic_replay_key_digest
    )
    assert admission.admission_binding_digest == procedure_admission_digest(admission)
    payload = prepared.bound_payload().model_dump(mode="json")
    assert payload["tag"] == "playbill-procedure-admission-bound-payload-v10"
    assert "acquisition_plan" not in payload or payload["acquisition_plan"] is None
    with pytest.raises(ValueError, match="binds no Provider, selection, output cap or trigger"):
        ProcedureRunAdmission.model_validate(
            {**admission.model_dump(mode="python"), "provider_output_bytes_cap": 1024}
        )
    with pytest.raises(ValueError, match="direct admission cannot claim Line state"):
        seal_procedure_run_admission(
            {**admission_fields(admission), "occurrence_id": "OCC-invented"}
        )


def test_a_pure_investigation_reseals_identity_and_refuses_a_trigger(tmp_path) -> None:
    """Binding an investigation is replay semantics: the run gets a new derived identity."""

    fixture = _fixture(tmp_path)
    prepared = _prepare(_state_procedure(), fixture, _StateReader(), journal_partition_id=None)
    investigated = execution_module.bind_prepared_investigation(
        prepared, investigation=_investigation()
    )
    before, after = prepared.admission, investigated.admission
    assert after.investigation == _investigation()
    assert after.semantic_replay_key_digest != before.semantic_replay_key_digest
    assert after.run_id == procedure_semantic_run_id(after.semantic_replay_key_digest)
    assert after.run_id != before.run_id
    assert after.journal_partition_id == procedure_direct_partition(
        after.semantic_replay_key_digest
    )
    # Rebinding the same investigation reproduces the same identity exactly.
    again = execution_module.bind_prepared_investigation(prepared, investigation=_investigation())
    assert again.admission == after
    with pytest.raises(ExecutionError, match="Line trigger requires a Line acquisition admission"):
        execution_module.bind_prepared_investigation(
            prepared,
            investigation=_investigation(),
            trigger=cast(Any, object()),
        )


def test_independent_execution_commits_one_semantic_result_across_partitions(tmp_path) -> None:
    accepted = _state_procedure()
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_fixture = _fixture(first_root)
    second_fixture = _fixture(second_root)
    second_fixture.journal.activate_writer(
        second_fixture.stream,
        "other-runs",
        fencing_token="writer",
        expected_head=second_fixture.journal.read_head(
            second_fixture.stream,
            "other-runs",
        ),
    )
    first = _prepare(accepted, first_fixture, _StateReader())
    second = _prepare(
        accepted,
        second_fixture,
        _StateReader(),
        journal_partition_id="other-runs",
        actor_context=_actor().model_copy(update={"operation_id": "another-request"}),
    )
    assert first.admission.semantic_replay_key_digest == second.admission.semantic_replay_key_digest
    assert first.admission.admission_binding_digest == second.admission.admission_binding_digest

    results = []
    for fixture, prepared in ((first_fixture, first), (second_fixture, second)):
        result = ProcedureExecutor(
            journal=fixture.journal,
            bodies=fixture.bodies,
            run_index=fixture.run_index,
            fencing_token="writer",
            activation_authority=_Authority(accepted.artifact_digest),
            contract_validator=_Contracts(),
        ).execute(prepared, accepted)
        stored = fixture.journal.all_records(
            fixture.stream,
            prepared.admission.journal_partition_id,
        )[-1]
        payload = parse_journal_payload(
            fixture.bodies.read(
                stored.record.payload_digest,
                access=BodyAccessContext(principal_id="test", can_read_body=True),
            )
        )
        assert isinstance(payload, dict)
        results.append((payload["semantic_result_digest"], result.receipt))

    assert results[0][0] == results[1][0]
    assert results[0][1] != results[1][1]


def _investigation() -> InvestigationBinding:
    """An exact investigation binding; only its identity enters replay semantics."""

    coordinate = _coordinate()
    return InvestigationBinding.model_construct(
        contract=ResolutionContractReference.model_construct(
            identity=ArtifactIdentity(kind="ResolutionContract", name="contract"),
            artifact_digest=_digest("resolution-contract"),
            coordinate=coordinate,
        ),
        hypothesis=ClaimVersionReference.model_construct(
            identity=ArtifactIdentity(kind="Claim", name="hypothesis"),
            artifact_digest=_digest("hypothesis"),
            statement_digest=_digest("hypothesis-statement"),
            coordinate=coordinate,
        ),
        window=BoundObservationWindow(starts_at=NOW, ends_at=NOW + timedelta(hours=1)),
    )


#: Every hand-built Line admission's acquisition plan, by plan digest.
_LINE_PLANS: dict[str, ProcedureAcquisitionPlan] = {}


def _line_admission(
    accepted: AcceptedProcedure,
    fixture: _Fixture,
    *,
    occurrence_id: str = "OCC-0001",
    occurrence_evaluation_time: datetime = NOW,
    admitted_at: datetime = NOW,
    attempt: int = 1,
    landed_capture_inputs: tuple[LandedCaptureRunInputV1, ...] = (),
    exhaust_inputs: tuple[ExhaustRunInputV1, ...] = (),
    coordinate: AcceptedCoordinate | None = None,
    actor_context: GovernedActorContext | None = None,
    line_identity: ArtifactIdentity | None = None,
    line_spec_digest: str | None = None,
    selection_decision: ProcedureSelectionDecision | None = None,
) -> ProcedureRunAdmission:
    """A Line occurrence's admission with a real, zero-occurrence acquisition plan.

    Its plan (retrievable with ``_line_plan``) plans no Provider call, which is
    exactly what a Line over a Provider-free Procedure binds.
    """

    prepare_kwargs: dict[str, object] = {}
    if coordinate is not None:
        prepare_kwargs["accepted_coordinate"] = coordinate
    if actor_context is not None:
        prepare_kwargs["actor_context"] = actor_context
    return _line_admission_from(
        _prepare(accepted, fixture, _StateReader(), **prepare_kwargs).admission,
        occurrence_id=occurrence_id,
        occurrence_evaluation_time=occurrence_evaluation_time,
        admitted_at=admitted_at,
        attempt=attempt,
        landed_capture_inputs=landed_capture_inputs,
        exhaust_inputs=exhaust_inputs,
        line_identity=line_identity,
        line_spec_digest=line_spec_digest,
        selection_decision=selection_decision,
    )


def _line_admission_from(
    direct: ProcedureRunAdmission,
    *,
    occurrence_id: str = "OCC-0001",
    occurrence_evaluation_time: datetime = NOW,
    admitted_at: datetime = NOW,
    attempt: int = 1,
    landed_capture_inputs: tuple[LandedCaptureRunInputV1, ...] = (),
    exhaust_inputs: tuple[ExhaustRunInputV1, ...] = (),
    line_identity: ArtifactIdentity | None = None,
    line_spec_digest: str | None = None,
    selection_decision: ProcedureSelectionDecision | None = None,
    taint_labels: tuple[str, ...] = ("sensitive",),
    epsilon_member: bool = True,
) -> ProcedureRunAdmission:
    """A direct admission's semantic fields, re-admitted as one Line occurrence."""

    policy_digest = _digest("acquisition-policy")
    decision = selection_decision or ProcedureSelectionDecision(
        policy_digest=policy_digest,
        verdict="selected",
        decisions=(),
    )
    line_identity = line_identity or ArtifactIdentity(kind="Line", name="daily-triage")
    spec_digest = line_spec_digest or _digest("line-spec")
    exhaust_binding = _digest("exhaust-binding") if exhaust_inputs else None
    plan = ProcedureAcquisitionPlan(
        accepted_coordinate=direct.accepted_coordinate,
        line_identity=line_identity,
        line_spec_digest=spec_digest,
        occurrence_id=occurrence_id,
        occurrence_evaluation_time=occurrence_evaluation_time,
        acquisition_policy_format=ACQUISITION_RULES_DIGEST_DOMAIN,
        acquisition_policy_digest=decision.policy_digest,
        selection_receipt_digest=_digest("selection-receipt"),
        selection_decision=decision,
        selection_decision_digest=procedure_selection_decision_digest(decision),
        has_exhaust_occurrences=exhaust_binding is not None,
        exhaust_access_binding_digest=exhaust_binding,
    )
    plan_digest = procedure_acquisition_plan_digest(plan)
    _LINE_PLANS[plan_digest] = plan
    return seal_procedure_run_admission(
        {
            **admission_fields(direct),
            "attempt": attempt,
            "invocation_origin": "line",
            "journal_stream": procedure_line_journal_stream("instance-a"),
            "line_identity": line_identity,
            "line_spec_digest": spec_digest,
            "occurrence_id": occurrence_id,
            "occurrence_evaluation_time": occurrence_evaluation_time,
            "deployment_snapshot_digest": _digest("deployment"),
            "acquisition_policy_digest": decision.policy_digest,
            "selection_receipt_digest": _digest("selection-receipt"),
            "sensitivity_policy_digest": _digest("sensitivity"),
            "mandate_coordinate_digest": _digest("mandate"),
            "calibration_coordinate_digest": _digest("calibration"),
            "taint_labels": taint_labels,
            "epsilon_member": epsilon_member,
            "admitted_at": admitted_at,
            "resolved_provider_bindings": (),
            "selection_decision": decision,
            "selection_decision_digest": procedure_selection_decision_digest(decision),
            "provider_output_bytes_cap": 1_048_576,
            "acquisition_plan_digest": plan_digest,
            "exhaust_access_binding_digest": exhaust_binding,
            "landed_capture_inputs": landed_capture_inputs,
            "exhaust_inputs": exhaust_inputs,
        }
    )


def _line_plan(admission: ProcedureRunAdmission) -> ProcedureAcquisitionPlan:
    assert admission.acquisition_plan_digest is not None
    return _LINE_PLANS[admission.acquisition_plan_digest]


def _line_bound_prepared(fixture: _Fixture, direct: PreparedProcedureRun) -> PreparedProcedureRun:
    """A direct prepared run re-admitted as an untainted Line occurrence, writer active."""

    admission = _line_admission_from(direct.admission, taint_labels=(), epsilon_member=False)
    fixture.journal.activate_writer(
        admission.journal_stream,
        admission.journal_partition_id,
        fencing_token="writer",
        expected_head=fixture.journal.read_head(
            admission.journal_stream, admission.journal_partition_id
        ),
    )
    return _line_prepared(admission, direct)


def _line_prepared(
    admission: ProcedureRunAdmission,
    direct: PreparedProcedureRun,
    *,
    manifest: ProcedureAdmissionMaterialManifest | None = None,
    landed_capture_materials: tuple[LandedCaptureRunMaterialV1, ...] = (),
    exhaust_materials: tuple[ExhaustRunMaterialV1, ...] = (),
) -> PreparedProcedureRun:
    """The prepared run of a hand-built Line admission over a direct run's state reads."""

    manifest = manifest or ProcedureAdmissionMaterialManifest(members=())
    plan = _line_plan(admission)
    return PreparedProcedureRun(
        admission=admission,
        accepted_state_materials=direct.accepted_state_materials,
        landed_capture_materials=landed_capture_materials,
        exhaust_materials=exhaust_materials,
        admission_material_manifest=manifest,
        admission_material_manifest_digest=procedure_admission_material_digest(manifest),
        acquisition_plan=plan,
        acquisition_plan_digest=procedure_acquisition_plan_digest(plan),
    )


def _accepted_line_for_admission(
    admission: ProcedureRunAdmission,
    accepted_procedure: AcceptedProcedure,
) -> AcceptedLineSpec:
    procedure_pin = ArtifactPin(
        role="procedure",
        target=accepted_procedure.procedure.identity,
        artifact_digest=accepted_procedure.artifact_digest,
    )
    assert admission.line_identity is not None
    line = LineSpec(
        identity=admission.line_identity,
        occurrence_epoch=1,
        procedure=procedure_pin,
        parameters={},
        max_authority="observe",
        budgets={},
        epsilon={"$decimal": "0"},
    )
    return AcceptedLineSpec(
        path=line_spec_path(line.identity.name),
        line=line,
        artifact_digest=line_spec_digest(line).tagged,
    )


def test_line_run_id_names_its_occurrence_and_attempt(tmp_path) -> None:
    """Attempts of one occurrence share replay identity; another occurrence does not.

    The acquisition plan names the occurrence and its evaluation instant, so a
    different occurrence is different replay semantics; a later attempt of the
    same occurrence is the same semantics under a new run id. Both journal in
    the Line's one partition.
    """

    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    first = _line_admission(accepted, fixture)
    second = _line_admission(
        accepted,
        fixture,
        occurrence_id="OCC-0002",
        occurrence_evaluation_time=NOW + timedelta(hours=1),
        admitted_at=NOW + timedelta(hours=2),
    )
    later_attempt = _line_admission(accepted, fixture, attempt=2)
    later_admission = _line_admission(accepted, fixture, admitted_at=NOW + timedelta(hours=3))

    assert first.semantic_replay_key_digest != second.semantic_replay_key_digest
    assert first.run_id != second.run_id
    assert first.semantic_replay_key_digest == later_attempt.semantic_replay_key_digest
    assert first.admission_binding_digest == later_attempt.admission_binding_digest
    assert first.run_id != later_attempt.run_id
    # When the occurrence was admitted is not replay semantics.
    assert first.semantic_replay_key_digest == later_admission.semantic_replay_key_digest
    assert first.run_id == later_admission.run_id
    assert first.journal_partition_id == second.journal_partition_id
    assert first.journal_partition_id == procedure_line_partition(first.line_identity)
    with pytest.raises(ValueError, match="run_id does not reproduce"):
        ProcedureRunAdmission.model_validate(
            {**first.model_dump(mode="python"), "run_id": second.run_id}
        )


def test_line_v3_admission_binds_the_exact_accepted_line_spec(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted_procedure = _state_procedure()
    admission = _line_admission(accepted_procedure, fixture)
    accepted_line = _accepted_line_for_admission(admission, accepted_procedure)
    bound = admission.model_copy(update={"line_spec_digest": accepted_line.artifact_digest})

    verify_line_admission_spec(bound, accepted_line)
    with pytest.raises(ExecutionError, match="another accepted LineSpec"):
        verify_line_admission_spec(
            bound.model_copy(update={"line_identity": ArtifactIdentity(kind="Line", name="other")}),
            accepted_line,
        )


def test_served_line_admission_binds_the_accepted_runtime_policy_or_refuses(
    tmp_path,
    monkeypatch,
) -> None:
    # `service_prepare_playbill_line_admission` now resolves the Line's
    # Procedure by name in the instance's accepted projection (B2), so this
    # test needs a Procedure the compiler actually admits (format v3, via the
    # real authoring/lowering path), not a hand-built v2 envelope.
    from tests.core_support._identity_support import accept_tree
    from tests.support.operation_world import author_procedure, slotted_spec

    instance, _owner = initialize_local(tmp_path)
    accept_tree(instance, contract_files(), name="slot-contracts")
    spec = slotted_spec(name="runtime-policy-procedure")
    author_procedure(instance, spec, name="runtime-policy-procedure")
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = procedure_path(spec.procedure)
    accepted_procedure_artifact = parse_procedure(tree[path], path=path)
    from tests.support.operation_world import operation_build
    from tests.support.procedures import bind_to_build

    accepted = bind_to_build(
        AcceptedProcedure(
            path=path,
            procedure=accepted_procedure_artifact,
            artifact_digest=procedure_artifact_digest(accepted_procedure_artifact).tagged,
        ),
        operation_build(),
        "compute",
    )

    fixture = _fixture(tmp_path)
    admission = _line_admission(accepted, fixture).model_copy(
        update={"provider_output_bytes_cap": 17}
    )
    accepted_line = _accepted_line_for_admission(admission, accepted)
    admission = admission.model_copy(update={"line_spec_digest": accepted_line.artifact_digest})
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    admission = admission.model_copy(
        update={
            "bound_coordinate": coordinate,
            "accepted_coordinate": coordinate,
            "head_at_admission": coordinate,
            "accepted_state_inputs": tuple(
                item.model_copy(update={"read_coordinate": coordinate})
                for item in admission.accepted_state_inputs
            ),
        }
    )

    bound = service_prepare_playbill_line_admission(
        instance,
        admission=admission,
        accepted_line=accepted_line,
    )

    assert isinstance(bound, ProcedureRunAdmission)
    assert bound.provider_output_bytes_cap == 64 * 1024 * 1024
    assert bound.semantic_replay_key_digest != procedure_semantic_replay_key_digest(admission)
    assert bound.semantic_replay_key_digest == procedure_semantic_replay_key_digest(bound)
    assert bound.admission_binding_digest == procedure_admission_digest(bound)

    with monkeypatch.context() as patch:
        patch.setattr(TypedStateReader, "source", lambda *_args: None)
        refused = service_prepare_playbill_line_admission(
            instance,
            admission=admission,
            accepted_line=accepted_line,
        )
    assert isinstance(refused, ProcedureAdmissionRefusal)
    assert refused.code == "procedure_runtime_policy_absent"

    mismatched = service_prepare_playbill_line_admission(
        instance,
        admission=admission,
        accepted_line=accepted_line.model_copy(update={"artifact_digest": "sha256:" + "9" * 64}),
    )
    assert isinstance(mismatched, ProcedureAdmissionRefusal)
    assert mismatched.code == "artifact_binding_mismatch"
    assert "another accepted LineSpec" in mismatched.message


def test_replay_carriers_have_one_client_contract_definition_source() -> None:
    for name in (
        "ProcedureAdmissionMaterialManifest",
        "ProcedureAdmissionMaterialMember",
        "ProcedureProviderBinding",
        "ProcedureReplayInputProjection",
        "ProcedureSelectionDecision",
    ):
        assert getattr(execution_module, name) is getattr(procedure_result_contracts, name)


def test_equal_line_admission_digests_retrieve_two_exact_runs(tmp_path, monkeypatch) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    direct = _prepare(accepted, fixture, _StateReader())
    first = _line_admission(accepted, fixture)
    # A second attempt of the same occurrence: the same binding, its own run.
    second = _line_admission(accepted, fixture, attempt=2)
    assert first.admission_binding_digest == second.admission_binding_digest
    assert first.run_id != second.run_id
    prepared = tuple(_line_prepared(admission, direct) for admission in (first, second))
    fixture.journal.activate_writer(
        first.journal_stream,
        first.journal_partition_id,
        fencing_token="writer",
        expected_head=fixture.journal.read_head(first.journal_stream, first.journal_partition_id),
    )
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    )
    for item in prepared:
        assert executor.execute(item, accepted).status == "succeeded"

    records = fixture.journal.all_records(first.journal_stream, first.journal_partition_id)
    monkeypatch.setattr(
        procedure_run_service,
        "_records_for_run",
        lambda _instance, run_id: tuple(item for item in records if item.record.run_id == run_id),
    )

    class _Instance:
        def body_store(self):  # type: ignore[no-untyped-def]
            return fixture.bodies

    first_state = procedure_run_service._state_from_records(  # noqa: SLF001
        _Instance(), run_id=first.run_id
    )
    second_state = procedure_run_service._state_from_records(  # noqa: SLF001
        _Instance(), run_id=second.run_id
    )
    assert first_state.run_id == first.run_id
    assert second_state.run_id == second.run_id
    assert first_state.receipt != second_state.receipt
    assert fixture.run_index.get(first.run_id) is not None
    assert fixture.run_index.get(second.run_id) is not None


def test_line_replay_key_includes_byte_inputs_and_excludes_provenance(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    admission = _line_admission(_state_procedure(), fixture)
    baseline = admission.semantic_replay_key_digest

    for update in (
        {"admitted_at": NOW + timedelta(days=1)},
        {"deployment_snapshot_digest": _digest("other-deployment")},
        {"selection_receipt_digest": _digest("other-selection-receipt")},
        {"occurrence_id": "OCC-other"},
        {"procedure_path": "procedures/provenance-only.json"},
    ):
        changed = admission.model_copy(update=update)
        assert procedure_semantic_replay_key_digest(changed) == baseline

    for update in (
        {"provider_output_bytes_cap": cast(int, admission.provider_output_bytes_cap) + 1},
        {"lane": "replay"},
        {"epsilon_member": False},
        {"occurrence_evaluation_time": NOW + timedelta(days=1)},
        {"acquisition_plan_digest": _digest("other-plan")},
        {"mandate_coordinate_digest": _digest("other-mandate")},
    ):
        changed = admission.model_copy(update=update)
        assert procedure_semantic_replay_key_digest(changed) != baseline


def test_line_replay_key_membership_is_closed(tmp_path) -> None:
    """Exactly the one semantic projection's members decide replay identity.

    The Line, occurrence, acquisition policy, selection decision and Provider
    bindings enter through the acquisition plan's digest; changing one of them
    alone (without the plan) is not a different replay. Provenance never
    enters.
    """

    fixture = _fixture(tmp_path)
    capture = LandedCaptureRunInputV1(
        input_name="capture",
        capture_digest=_digest("key-capture"),
        capture_contract_digest=_digest("key-capture-contract"),
        landing_cursor="partition:0001",
    )
    exhaust = ExhaustRunInputV1(
        input_name="exhaust",
        journal_identity="procedure-exhaust-v1/procedures",
        first_cursor="partition:0001",
        last_cursor="partition:0002",
        reducer_or_query_digest=_digest("key-reducer"),
        result_digest=_digest("key-exhaust-result"),
    )
    decision = ProcedureSelectionDecision(
        policy_digest=_digest("acquisition-policy"),
        verdict="selected",
        decisions=(
            AcquisitionInputDecision(
                input_name="capture",
                disposition="selected",
                considered_capture_digests=(_digest("considered-capture"),),
                selected_capture_digests=(capture.capture_digest,),
                selected_cursors=(capture.landing_cursor,),
                reason_codes=("selected-current",),
            ),
        ),
        coherence_proof_digest=_digest("coherence"),
    )
    binding = ProcedureProviderBinding(
        node_id="provider",
        slot="fetch",
        operation="read",
        operation_signature_digest=_digest("signature"),
        provider_artifact_digest=_digest("provider"),
        implementation_digest=_digest("implementation"),
        effect_class="external_read",
        secret_binding_identity_digests=(_digest("secret-identity"),),
    )
    admission = _line_admission(
        _state_procedure(),
        fixture,
        landed_capture_inputs=(capture,),
        exhaust_inputs=(exhaust,),
        selection_decision=decision,
    )
    parent = ProcedureParentBindingV1(
        parent_run_id="RUN-" + "a" * 64,
        parent_admission_digest=_digest("parent-admission"),
        node_id="invoke",
        ancestors=(_pin("procedure", "Procedure", "parent"),),
        activation_policies=("drain",),
    )
    baseline = procedure_semantic_replay_key_digest(admission)
    assert baseline == admission.semantic_replay_key_digest
    accepted_input = admission.accepted_state_inputs[0]
    pin = admission.full_pins[0]
    node_pins = admission.node_pin_sets[0]
    included = {
        "procedure identity": {
            "procedure_identity": ArtifactIdentity(kind="Procedure", name="other")
        },
        "Procedure artifact": {"procedure_artifact_digest": _digest("other-procedure")},
        "invocation input": {"invocation_input": {"other": True}},
        "bound coordinate": {
            "bound_coordinate": admission.bound_coordinate.model_copy(
                update={"semantic_root": _digest("other-semantic-root")}
            )
        },
        "full pins": {
            "full_pins": (pin.model_copy(update={"artifact_digest": _digest("other-pin")}),)
        },
        "node pin sets": {
            "node_pin_sets": (node_pins.model_copy(update={"node_id": "other-node"}),)
        },
        "pin set digest": {"pin_set_digest": _digest("other-pin-set")},
        "state query": {
            "accepted_state_inputs": (
                accepted_input.model_copy(
                    update={"query_definition_digest": _digest("other-query")}
                ),
            )
        },
        "state parameters": {
            "accepted_state_inputs": (
                accepted_input.model_copy(update={"parameters_digest": _digest("other-params")}),
            )
        },
        "state result": {
            "accepted_state_inputs": (
                accepted_input.model_copy(update={"result_digest": _digest("other-result")}),
            )
        },
        "state read coordinate": {
            "accepted_state_inputs": (
                accepted_input.model_copy(
                    update={
                        "read_coordinate": accepted_input.read_coordinate.model_copy(
                            update={"git_oid": "b" * 40}
                        )
                    }
                ),
            )
        },
        "state query budgets": {
            "accepted_state_inputs": (
                accepted_input.model_copy(
                    update={
                        "effective_query_budgets": (
                            accepted_input.effective_query_budgets.model_copy(
                                update={
                                    "max_results": (
                                        accepted_input.effective_query_budgets.max_results + 1
                                    )
                                }
                            )
                        )
                    }
                ),
            )
        },
        "Capture digest": {
            "landed_capture_inputs": (
                capture.model_copy(update={"capture_digest": _digest("other-capture")}),
            )
        },
        "Capture contract": {
            "landed_capture_inputs": (
                capture.model_copy(
                    update={"capture_contract_digest": _digest("other-capture-contract")}
                ),
            )
        },
        "Capture cursor": {
            "landed_capture_inputs": (capture.model_copy(update={"landing_cursor": "other:1"}),)
        },
        "exhaust journal": {
            "exhaust_inputs": (exhaust.model_copy(update={"journal_identity": "other/journal"}),)
        },
        "exhaust first cursor": {
            "exhaust_inputs": (exhaust.model_copy(update={"first_cursor": "other:1"}),)
        },
        "exhaust last cursor": {
            "exhaust_inputs": (exhaust.model_copy(update={"last_cursor": "other:2"}),)
        },
        "exhaust reducer": {
            "exhaust_inputs": (
                exhaust.model_copy(update={"reducer_or_query_digest": _digest("other-reducer")}),
            )
        },
        "exhaust result": {
            "exhaust_inputs": (
                exhaust.model_copy(update={"result_digest": _digest("other-exhaust-result")}),
            )
        },
        "Procedure budget": {
            "budget": admission.budget.model_copy(
                update={"max_items": (admission.budget.max_items or 1) + 1}
            )
        },
        "Procedure hard caps": {
            "hard_caps": admission.hard_caps.model_copy(
                update={"max_items": admission.hard_caps.max_items + 1}
            )
        },
        "acquisition plan": {"acquisition_plan_digest": _digest("other-plan")},
        "exhaust binding": {"exhaust_access_binding_digest": _digest("other-exhaust-binding")},
        "evaluation instant": {"occurrence_evaluation_time": NOW + timedelta(hours=1)},
        "resource budget": {
            "resource_budget": ProcedureResourceBudgetV1(
                result_bytes_cap=4096, repeat_attempts_cap=3
            )
        },
        "investigation": {"investigation": _investigation()},
        "parent binding": {"parent_binding": parent},
        "mandate": {"mandate_coordinate_digest": _digest("other-mandate")},
        "calibration": {"calibration_coordinate_digest": _digest("other-calibration")},
        "sensitivity": {"sensitivity_policy_digest": _digest("other-sensitivity")},
        "lane": {"lane": "replay"},
        "taint": {"taint_labels": ("other-taint",)},
        "epsilon": {"epsilon_member": False},
        "Provider output cap": {
            "provider_output_bytes_cap": cast(int, admission.provider_output_bytes_cap) + 1
        },
    }
    for label, update in included.items():
        assert (
            procedure_semantic_replay_key_digest(admission.model_copy(update=update)) != baseline
        ), label

    excluded = {
        "run id": {"run_id": "RUN-" + "f" * 64},
        "admission digest": {"admission_binding_digest": _digest("other-admission")},
        "admitted time": {"admitted_at": NOW + timedelta(days=1)},
        "head at admission": {
            "head_at_admission": admission.head_at_admission.model_copy(
                update={"git_oid": "c" * 40}
            )
        },
        "Line identity": {"line_identity": ArtifactIdentity(kind="Line", name="other-line")},
        "LineSpec digest": {"line_spec_digest": _digest("other-line-spec")},
        "occurrence": {"occurrence_id": "OCC-other"},
        "acquisition policy": {"acquisition_policy_digest": _digest("other-policy")},
        "selection decision": {
            "selection_decision": decision.model_copy(update={"verdict": "refused"})
        },
        "Provider bindings": {"resolved_provider_bindings": (binding,)},
        "attempt": {"attempt": admission.attempt + 1},
        "actor": {
            "actor_context": admission.actor_context.model_copy(update={"operation_id": "other-op"})
        },
        "Procedure path": {"procedure_path": "procedures/other.json"},
        "journal stream": {
            "journal_stream": JournalStreamIdentityV1(
                instance_id=admission.instance_id,
                journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
                stream_id="other-stream",
            )
        },
        "journal partition": {"journal_partition_id": "line:other"},
        "deployment": {"deployment_snapshot_digest": _digest("other-deployment")},
        "selection receipt": {"selection_receipt_digest": _digest("other-receipt")},
        "selection decision digest": {
            "selection_decision_digest": _digest("other-decision-digest")
        },
        "accepted material body": {
            "accepted_state_inputs": (
                accepted_input.model_copy(
                    update={"material_body_digest": _digest("other-material")}
                ),
            )
        },
    }
    for label, update in excluded.items():
        assert (
            procedure_semantic_replay_key_digest(admission.model_copy(update=update)) == baseline
        ), label


def test_three_plane_projection_is_stable_and_material_digest_free(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    admission = _line_admission(_state_procedure(), fixture)
    capture = LandedCaptureRunInputV1(
        input_name="capture",
        capture_digest=_digest("capture"),
        capture_contract_digest=_digest("capture-contract"),
        landing_cursor="partition:0001",
    )
    exhaust = ExhaustRunInputV1(
        input_name="exhaust",
        journal_identity="procedure-exhaust-v1/procedures",
        first_cursor="partition:0001",
        last_cursor="partition:0002",
        reducer_or_query_digest=_digest("reducer"),
        result_digest=_digest("exhaust-result"),
    )
    projected = procedure_replay_input_vector(
        admission.model_copy(
            update={"landed_capture_inputs": (capture,), "exhaust_inputs": (exhaust,)}
        )
    )

    assert [(item.input_name, item.plane, item.kind) for item in projected] == [
        ("capture", "landed_capture", "capture"),
        ("exhaust", "exhaust", "reduced_exhaust"),
        ("rows", "accepted_state", "query_result"),
    ]
    assert projected[2].value_or_body_digest == admission.accepted_state_inputs[0].result_digest
    assert (
        "material_body_digest"
        not in canonical_bytes([item.model_dump(mode="json") for item in projected]).decode()
    )


def test_executor_recognizes_a_source_node_as_a_landed_capture_input(tmp_path) -> None:
    capture_contract = _pin("capture-contract", "CaptureContract", "v4-source")
    source = CallNode(
        node_id="source",
        captures={"capture": named(capture_contract)},
        slot="fetch",
        input={},
        as_="capture",
    )
    source_definition = ProcedureDefinition(
        name="source-landed-capture",
        contract_in=named(_pin("contract-in", "Contract", "provider-input")),
        contract_out=named(_pin("contract-out", "Contract", "provider-output")),
        slots={"fetch": {"in": "test.value@1", "out": "test.value@1", "effect": "external_read"}},
        nodes=(source,),
        returns="capture",
        budget=_budget(),
        hard_caps=_hard_caps(),
        terminal_capability=1,
    )
    source_accepted = _accepted(source_definition, pins=(capture_contract,))
    fixture = _fixture(tmp_path)
    landed = LandedCaptureRunInputV1(
        # A landed Capture binds one capture output, <alias>.<field>.
        input_name="capture.capture",
        capture_digest=_digest("v4-source-capture"),
        capture_contract_digest=capture_contract.artifact_digest,
        landing_cursor="partition:0001",
    )
    admission = _line_admission(
        source_accepted,
        fixture,
        landed_capture_inputs=(landed,),
    )
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(source_accepted.artifact_digest),
        contract_validator=_Contracts(),
    )

    executor._verify_input_planes(admission, source_accepted)  # noqa: SLF001

    with pytest.raises(ExecutionError, match="names no Procedure input node"):
        executor._verify_input_planes(  # noqa: SLF001
            admission.model_copy(
                update={
                    "landed_capture_inputs": (
                        landed.model_copy(update={"input_name": "capture.missing"}),
                    )
                }
            ),
            source_accepted,
        )


def test_admission_material_manifest_is_sorted_and_missing_is_typed(tmp_path) -> None:
    member = ProcedureAdmissionMaterialMember(
        input_name="capture",
        plane="landed_capture",
        semantic_digest=_digest("capture"),
        body_digest=None,
        retention_authority_digest=_digest("capture-contract"),
        body_retention="never_materialize",
    )
    manifest = ProcedureAdmissionMaterialManifest(members=(member,))
    assert procedure_admission_material_digest(manifest).startswith("sha256:")
    bodies_root = tmp_path / "material-cas"
    bodies_root.mkdir()
    bodies = ContentAddressedBodyStore(bodies_root)
    with pytest.raises(Exception) as exc_info:
        execution_module.read_admission_material_body(bodies, member)
    assert getattr(exc_info.value, "code") == "admission_material_unavailable_by_policy"
    assert getattr(exc_info.value, "details")["input_name"] == "capture"

    optional = member.model_copy(update={"body_retention": "optional"})
    with pytest.raises(Exception) as optional_exc:
        execution_module.read_admission_material_body(bodies, optional)
    assert getattr(optional_exc.value, "code") == "admission_material_unavailable_by_policy"

    absent_optional = optional.model_copy(update={"body_digest": _digest("absent-optional")})
    with pytest.raises(Exception) as absent_optional_exc:
        execution_module.read_admission_material_body(bodies, absent_optional)
    assert getattr(absent_optional_exc.value, "code") == "admission_material_unavailable_by_policy"

    required_missing = ProcedureAdmissionMaterialMember.model_validate(
        {
            **optional.model_dump(mode="python"),
            "body_digest": _digest("absent-required"),
            "body_retention": "required_for_duration",
            "retain_until": NOW + timedelta(seconds=3),
        }
    )
    with pytest.raises(Exception) as required_missing_exc:
        execution_module.read_admission_material_body(bodies, required_missing)
    assert getattr(required_missing_exc.value, "code") == "replay_material_unavailable"

    stored = bodies.store(b'{"capture":"retained"}')
    retained_optional = optional.model_copy(update={"body_digest": stored.digest})
    reopened = ContentAddressedBodyStore(bodies_root)
    assert (
        execution_module.read_admission_material_body(
            reopened,
            retained_optional,
        )
        == b'{"capture":"retained"}'
    )
    body_path = (
        bodies_root
        / "sha256"
        / stored.digest.removeprefix("sha256:")[:2]
        / stored.digest.removeprefix("sha256:")
    )
    body_path.write_bytes(b"corrupt")
    with pytest.raises(Exception) as corrupt_exc:
        execution_module.read_admission_material_body(reopened, retained_optional)
    assert getattr(corrupt_exc.value, "code") == "replay_material_unavailable"

    with pytest.raises(ValueError, match="never_materialize"):
        ProcedureAdmissionMaterialMember(
            **{
                **member.model_dump(mode="python"),
                "body_digest": _digest("forbidden-body"),
            }
        )

    capture_input = LandedCaptureRunInputV1(
        input_name="capture",
        capture_digest=_digest("retained-capture"),
        capture_contract_digest=_digest("retained-capture-contract"),
        landing_cursor="partition:0001",
    )
    policy = CaptureRetentionErasurePolicy(
        body_retention="required_for_duration",
        minimum_retention=CanonicalDuration(microseconds=3_000_000),
        erasure="prohibited",
        selector_privacy="direct_allowed",
    )
    retained = capture_admission_material_member(
        capture_input,
        policy=policy,
        admitted_at=NOW,
        body_digest=_digest("retained-body"),
    )
    assert retained.retain_until == NOW + timedelta(seconds=3)
    assert retained.retention_authority_digest == capture_input.capture_contract_digest


def test_admission_bound_manifest_matches_the_exact_three_plane_admission(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    capture_input = LandedCaptureRunInputV1(
        input_name="capture",
        capture_digest=_digest("manifest-capture"),
        capture_contract_digest=_digest("manifest-contract"),
        landing_cursor="partition:0001",
    )
    admission = _line_admission(
        _state_procedure(),
        fixture,
        landed_capture_inputs=(capture_input,),
    )
    member = ProcedureAdmissionMaterialMember(
        input_name="capture",
        plane="landed_capture",
        semantic_digest=capture_input.capture_digest,
        body_digest=_digest("manifest-body"),
        retention_authority_digest=capture_input.capture_contract_digest,
        body_retention="optional",
    )
    manifest = ProcedureAdmissionMaterialManifest(members=(member,))
    plan = _line_plan(admission)
    bound = ProcedureAdmissionBoundPayload(
        admission=admission,
        admission_material_manifest=manifest,
        admission_material_manifest_digest=procedure_admission_material_digest(manifest),
        acquisition_plan=plan,
        acquisition_plan_digest=procedure_acquisition_plan_digest(plan),
    )
    assert bound.admission_material_manifest == manifest
    assert parse_admission_payload(bound.model_dump(mode="json")) == bound

    wrong = ProcedureAdmissionMaterialManifest(
        members=(member.model_copy(update={"semantic_digest": _digest("substituted")}),)
    )
    with pytest.raises(ValueError, match="disagrees with its admitted input"):
        ProcedureAdmissionBoundPayload(
            admission=admission,
            admission_material_manifest=wrong,
            admission_material_manifest_digest=procedure_admission_material_digest(wrong),
            acquisition_plan=plan,
            acquisition_plan_digest=procedure_acquisition_plan_digest(plan),
        )
    with pytest.raises(ValueError, match="acquisition plan differs from admission"):
        ProcedureAdmissionBoundPayload(
            admission=admission,
            admission_material_manifest=manifest,
            admission_material_manifest_digest=procedure_admission_material_digest(manifest),
        )


@pytest.mark.parametrize(
    "tag",
    (
        "playbill-procedure-admission-bound-payload-v2",
        "playbill-procedure-admission-bound-payload-v9",
    ),
)
def test_a_removed_admission_envelope_is_refused_never_reinterpreted(tmp_path, tag) -> None:
    fixture = _fixture(tmp_path)
    payload = _prepare(_state_procedure(), fixture, _StateReader()).bound_payload()
    retired = {**payload.model_dump(mode="json"), "tag": tag}
    with pytest.raises(ExecutionError, match="admission payload version is unsupported"):
        parse_admission_payload(retired)


def test_a_zero_provider_line_run_journals_and_receipts_its_empty_plan(
    tmp_path,
    monkeypatch,
) -> None:
    """A Line over a Provider-free Procedure binds a real plan that plans no call."""

    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    direct = _prepare(accepted, fixture, _StateReader())
    admission = _line_admission(accepted, fixture)
    prepared = _line_prepared(admission, direct)
    assert prepared.acquisition_plan is not None
    assert prepared.acquisition_plan.external_occurrences == ()
    fixture.journal.activate_writer(
        fixture.stream,
        admission.journal_partition_id,
        fencing_token="writer",
        expected_head=fixture.journal.read_head(
            fixture.stream,
            admission.journal_partition_id,
        ),
    )

    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(prepared, accepted)
    assert result.status == "succeeded"

    records = fixture.journal.all_records(fixture.stream, admission.journal_partition_id)
    bound_record = next(item for item in records if item.record.event_kind == "admission_bound")
    bound_payload = parse_journal_payload(
        fixture.bodies.read(
            bound_record.record.payload_digest,
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
    )
    assert bound_payload["tag"] == "playbill-procedure-admission-bound-payload-v10"
    assert ProcedureRunAdmission.model_validate(bound_payload["admission"]) == admission
    assert bound_payload["acquisition_plan_digest"] == admission.acquisition_plan_digest
    monkeypatch.setattr(procedure_run_service, "_records_for_run", lambda *_args: records)

    class _Instance:
        def body_store(self):  # type: ignore[no-untyped-def]
            return fixture.bodies

    state = procedure_run_service._state_from_records(  # noqa: SLF001
        _Instance(),
        run_id=admission.run_id,
    )
    assert isinstance(state.receipt, ProcedureRunReceipt)
    assert state.receipt.resolved_provider_bindings == ()
    assert state.receipt.invocation_receipt_digests == ()
    assert state.receipt.budget.observed.provider_calls == 0
    assert state.receipt.budget.declared.provider_output_bytes_cap == 1_048_576


def test_provider_bindings_are_exactly_the_acquisition_plans_own(tmp_path) -> None:
    """Bindings enter replay identity through the plan; a stray binding is refused."""

    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    direct = _prepare(accepted, fixture, _StateReader())
    admission = _line_admission(accepted, fixture)
    stray = ProcedureProviderBinding(
        node_id="provider",
        slot="fetch",
        operation="read",
        operation_signature_digest=_digest("signature"),
        provider_artifact_digest=_digest("provider"),
        implementation_digest=_digest("implementation"),
        effect_class="external_read",
        secret_binding_identity_digests=(),
    )
    with_stray = admission.model_copy(update={"resolved_provider_bindings": (stray,)})
    assert procedure_semantic_replay_key_digest(with_stray) == (
        admission.semantic_replay_key_digest
    )
    with pytest.raises(ValueError, match="not the acquisition plan's own"):
        _line_prepared(with_stray, direct)


def test_line_track_fold_reads_the_current_admission_payload(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    direct = _prepare(accepted, fixture, _StateReader())
    line_admission = _line_admission(accepted, fixture)
    accepted_line = _accepted_line_for_admission(line_admission, accepted)
    v3 = _line_admission(accepted, fixture, line_spec_digest=accepted_line.artifact_digest)
    payloads = (_line_prepared(v3, direct).bound_payload().model_dump(mode="json"),)
    reducer = LineTrackRecordReducer(
        accepted_line=accepted_line,
        accepted_procedure=accepted,
    )

    for index, payload in enumerate(payloads, start=1):
        record = VerifiedExhaustRecordV1(
            record_digest=_digest(f"nested-record-{index}"),
            generation_digest=_digest("nested-generation"),
            sequence=1,
            event_kind="admission_bound",
            payload_digest=_digest(f"nested-payload-{index}"),
            payload=payload,
            procedure_artifact_digest=accepted.artifact_digest,
            definition_digest=accepted.procedure.definition_digest,
            run_id=f"line-run-{index}",
            occurrence_id=f"occurrence-{index}",
            attempt=1,
            line_spec_digest=accepted_line.artifact_digest,
        )
        output = reducer.reduce((record,))
        assert output["deployment_snapshot_digests"] == [v3.deployment_snapshot_digest]


@pytest.mark.parametrize(
    "changed_input",
    (
        "invocation_input",
        "validated_pins",
        "state_result_digest",
        "effective_query_budgets",
        "evaluation_time",
        "bound_coordinate",
        "procedure_budgets",
    ),
)
def test_each_semantic_run_input_discriminates_the_replay_key(
    tmp_path,
    changed_input: str,
) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    first = _prepare(accepted, fixture, _StateReader()).admission
    update: dict[str, object]
    if changed_input == "invocation_input":
        update = {"invocation_input": {"value": 8}}
    elif changed_input == "validated_pins":
        pin = first.full_pins[0]
        update = {
            "full_pins": (
                pin.model_copy(update={"artifact_digest": _digest("different-pin")}),
                *first.full_pins[1:],
            )
        }
    elif changed_input == "state_result_digest":
        admitted = first.accepted_state_inputs[0]
        update = {
            "accepted_state_inputs": (
                admitted.model_copy(update={"result_digest": _digest("different-result")}),
            )
        }
    elif changed_input == "effective_query_budgets":
        admitted = first.accepted_state_inputs[0]
        update = {
            "accepted_state_inputs": (
                admitted.model_copy(
                    update={
                        "effective_query_budgets": admitted.effective_query_budgets.model_copy(
                            update={"max_results": 101}
                        )
                    }
                ),
            )
        }
    elif changed_input == "evaluation_time":
        later = first.admitted_at + timedelta(microseconds=1)
        update = {"admitted_at": later, "occurrence_evaluation_time": later}
    elif changed_input == "bound_coordinate":
        update = {
            "bound_coordinate": first.bound_coordinate.model_copy(update={"git_oid": "b" * 40})
        }
    else:
        update = {
            "budget": first.budget.model_copy(update={"max_items": first.budget.max_items + 1}),
            "hard_caps": first.hard_caps.model_copy(
                update={"max_items": first.hard_caps.max_items + 1}
            ),
        }
    second = first.model_copy(update=update)

    assert procedure_semantic_replay_key_digest(first) != (
        procedure_semantic_replay_key_digest(second)
    )


def test_distinct_semantic_refusals_have_distinct_result_digests(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    replay_key = _prepare(accepted, fixture, _StateReader()).admission.semantic_replay_key_digest
    first = ProcedureRunRefusalV1(
        code="guard_refused",
        message="first wording",
        node_id="gate",
        detail_code="first.code",
    )
    second = ProcedureRunRefusalV1(
        code="repeat_exhausted",
        message="second wording",
        node_id="repeat",
    )

    assert procedure_semantic_result_digest(
        semantic_replay_key_digest=replay_key,
        status="refused",
        output=None,
        refusal=first,
    ) != procedure_semantic_result_digest(
        semantic_replay_key_digest=replay_key,
        status="refused",
        output=None,
        refusal=second,
    )


def test_a_repeat_body_runs_a_closed_transform_per_attempt(tmp_path) -> None:
    accepted = _repeat_transform_procedure()
    fixture = _fixture(tmp_path)
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(_prepare(accepted, fixture, _StateReader()), accepted)

    assert result.status == "succeeded"
    assert isinstance(result.output, dict)
    (attempt,) = result.output["attempts"]
    assert attempt["attempt"] == 1 and attempt["until"] is True
    shaped = result.output["final"]["shaped"]
    assert shaped["rows"] == [{"identifier": "a"}, {"identifier": "b"}]
    assert shaped["complete"] is True and shaped["count"] == 2
    branch_payloads = [
        parse_journal_payload(
            fixture.bodies.read(
                stored.record.payload_digest,
                access=BodyAccessContext(principal_id="test", can_read_body=True),
            )
        )
        for stored in fixture.journal.all_records(fixture.stream, "runs")
        if stored.record.event_kind == "branch_evaluated"
    ]
    assert len(branch_payloads) == 1
    branch = branch_payloads[0]
    assert isinstance(branch, dict)
    assert branch["repeat_attempt"] == 1
    assert branch["body_lineage"]["shaped"]["items"] is None  # type: ignore[index]


def test_a_repeat_body_transform_is_held_to_the_items_budget(tmp_path) -> None:
    accepted = _repeat_transform_procedure(max_items=1)
    fixture = _fixture(tmp_path)
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(_prepare(accepted, fixture, _StateReader()), accepted)

    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code == "budget_max_items_exceeded"
    assert result.refusal.details["boundary"] == "transform:shape-body"


@pytest.mark.parametrize(
    ("boundary", "expected_boundary"),
    (
        ("contract-out", "contract-out:list-contract-out"),
        ("return", "contract-out:list-return"),
    ),
)
def test_production_validator_enforces_every_list_boundary(
    tmp_path,
    boundary: str,
    expected_boundary: str,
) -> None:
    accepted = _owned_boundary_procedure(boundary)
    fixture = _fixture(tmp_path)
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=OwnedProcedureContractValidator(accepted),
    ).execute(
        _prepare(
            accepted,
            fixture,
            _StateReader({"items": [{"id": "one"}, {"id": "two"}]}),
            invocation_input={},
        ),
        accepted,
    )

    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code == "budget_max_items_exceeded"
    assert result.refusal.details == {
        "boundary": expected_boundary,
        "dimension": "max_items",
        "field_path": "items",
        "limit": 1,
        "observed": 2,
    }


def test_return_seam_counts_extractable_values_and_caps_all_result_bytes() -> None:
    with pytest.raises(_RunRefusal) as item_refusal:
        _check_return_budget({"items": [1, 2]}, max_items=1, node_id="return")
    assert item_refusal.value.refusal.code == "budget_max_items_exceeded"

    with pytest.raises(_RunRefusal) as byte_refusal:
        _check_return_budget(
            "x" * 1_048_576,
            max_items=1,
            node_id="return",
        )
    assert byte_refusal.value.refusal.budget is not None
    assert byte_refusal.value.refusal.budget.budget_kind == "result_bytes"

    _check_return_budget({"nested": {"items": list(range(200))}}, max_items=1, node_id="return")
    _check_return_budget({"nested": {"items": [1]}}, max_items=0, node_id="return")


def test_successful_state_run_binds_inputs_and_logs_every_path(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    reader = _StateReader()
    prepared = _prepare(accepted, fixture, reader)
    assert reader.calls[0][2] == _coordinate()
    assert prepared.admission.accepted_state_inputs[0].input_name == "rows"

    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(prepared, accepted)
    assert result.status == "succeeded"
    assert result.output == {"items": _query_rows({"items": [{"id": "one"}]}), "status": "ok"}

    records = fixture.journal.all_records(fixture.stream, "runs")
    kinds = [item.record.event_kind for item in records]
    assert kinds == [
        "attempt_started",
        "admission_bound",
        "node_fired",
        "branch_evaluated",
        "node_fired",
        "node_fired",
        "attempt_finalized",
    ]
    assert all(fixture.bodies.verify(item.record.payload_digest) for item in records)
    assert result.receipt.record_digests == tuple(item.record_digest for item in records)


def _final_payload(fixture: _Fixture, partition_id: str = "runs") -> dict[str, object]:
    stored = fixture.journal.all_records(fixture.stream, partition_id)[-1]
    payload = parse_journal_payload(
        fixture.bodies.read(
            stored.record.payload_digest,
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
    )
    assert isinstance(payload, dict)
    return payload


def test_v2_execution_reads_retained_state_from_an_independent_handle(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    prepared = _prepare(accepted, fixture, _StateReader())
    independent_bodies = ContentAddressedBodyStore(fixture.bodies.root)
    assert independent_bodies is not fixture.bodies

    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=independent_bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(prepared, accepted)

    assert result.status == "succeeded"
    assert result.output == {"items": _query_rows({"items": [{"id": "one"}]}), "status": "ok"}


def test_missing_retained_state_is_a_typed_operational_failure(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    prepared = _prepare(accepted, fixture, _StateReader())
    assert fixture.bodies.erase(prepared.accepted_state_materials[0].input.material_body_digest)

    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(prepared, accepted)

    assert result.status == "failed"
    assert _final_payload(fixture)["failure_code"] == "cas_unavailable_at_replay"


def test_retained_state_result_mismatch_is_typed(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    prepared = _prepare(accepted, fixture, _StateReader())
    material = prepared.accepted_state_materials[0]
    other = fixture.bodies.store(canonical_bytes({"items": [{"id": "other"}]}))
    mismatched_input = material.input.model_copy(update={"material_body_digest": other.digest})
    mismatched = prepared.model_copy(
        update={
            "admission": prepared.admission.model_copy(
                update={"accepted_state_inputs": (mismatched_input,)}
            ),
            "accepted_state_materials": (material.model_copy(update={"input": mismatched_input}),),
        }
    )

    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(mismatched, accepted)

    assert result.status == "failed"
    assert _final_payload(fixture)["failure_code"] == "replay_material_mismatch"


class _ExpiredBudgetClock:
    def __init__(self) -> None:
        self.calls = 0

    def now(self):
        return NOW

    def monotonic_ns(self):
        self.calls += 1
        # Every reading after the first is one second past the run's budget.
        return 0 if self.calls == 1 else (RUN_WALL_CLOCK.microseconds + 1_000_000) * 1_000


def test_wall_clock_exhaustion_uses_typed_classification(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        clock=_ExpiredBudgetClock(),
    ).execute(_prepare(accepted, fixture, _StateReader()), accepted)

    assert result.status == "failed"
    assert _final_payload(fixture)["failure_code"] == "wall_clock_exhausted"


def test_false_guard_is_a_typed_refusal_with_complete_finalize(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure(false_branch=True)
    prepared = _prepare(accepted, fixture, _StateReader())
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(prepared, accepted)
    assert result.status == "refused"
    assert result.refusal is not None and result.refusal.code == "guard_refused"
    assert result.refusal.detail_code == "no-items"
    assert fixture.journal.all_records(fixture.stream, "runs")[-1].record.event_kind == (
        "attempt_finalized"
    )


def test_count_guard_selects_mutually_exclusive_arms_with_production_validator(
    tmp_path: Path,
) -> None:
    cases = (
        ("gt", "succeeded", "on_true", {"count": 1}),
        ("eq", "halted", "on_false", None),
    )
    for operator, expected_status, expected_arm, expected_output in cases:
        root = tmp_path / operator
        root.mkdir()
        fixture = _fixture(root)
        accepted = _guarded_filter_procedure(operator=operator)
        result = ProcedureExecutor(
            journal=fixture.journal,
            bodies=fixture.bodies,
            run_index=fixture.run_index,
            fencing_token="writer",
            activation_authority=_Authority(accepted.artifact_digest),
            contract_validator=OwnedProcedureContractValidator(accepted),
        ).execute(
            _prepare(
                accepted,
                fixture,
                _StateReader(),
                invocation_input={},
                run_id=f"run-{operator}",
            ),
            accepted,
        )

        branch_records = [
            parse_journal_payload(
                fixture.bodies.read(
                    stored.record.payload_digest,
                    access=BodyAccessContext(principal_id="test", can_read_body=True),
                )
            )
            for stored in fixture.journal.all_records(fixture.stream, "runs")
            if stored.record.event_kind == "branch_evaluated"
        ]
        assert result.status == expected_status
        assert result.output == expected_output
        assert len(branch_records) == 1
        branch_record = branch_records[0]
        assert isinstance(branch_record, dict)
        assert branch_record["selected_arm"] == expected_arm


def test_scalar_step_guard_returns_through_true_arm_with_production_validator(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    accepted = _guarded_scalar_procedure()
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=OwnedProcedureContractValidator(accepted),
    ).execute(
        _prepare(accepted, fixture, _StateReader(), invocation_input={}),
        accepted,
    )

    assert result.status == "succeeded"
    assert result.refusal is None
    assert result.output == {"count": 1}


def test_explicit_next_into_guard_arm_halt_remains_authoritative(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    accepted = _guarded_filter_procedure(operator="gt", result_next="stop")
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=OwnedProcedureContractValidator(accepted),
    ).execute(
        _prepare(accepted, fixture, _StateReader(), invocation_input={}),
        accepted,
    )

    assert result.status == "halted"
    assert result.output is None
    assert result.refusal is None


def test_unresolved_step_guard_operand_is_typed_refusal_not_false_branch(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    accepted = _guarded_scalar_procedure(path=("missing",))
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=OwnedProcedureContractValidator(accepted),
    ).execute(
        _prepare(accepted, fixture, _StateReader(), invocation_input={}),
        accepted,
    )

    assert result.status == "refused"
    assert result.output is None
    assert result.refusal is not None
    assert result.refusal.code == "runtime_reference_unresolved"
    assert result.refusal.node_id == "gate"


def test_guard_missing_alias_is_rejected_by_static_law() -> None:
    with pytest.raises(ProcedureGraphFormatError, match="R15: guard.*missing"):
        _guarded_scalar_procedure(alias="missing")


def test_json_escape_hatch_is_not_charged_as_a_typed_collection(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    empty = _owned_contract("json-empty-input", {})
    opaque = _owned_contract(
        "json-output",
        {
            "items": PropertySchema(type="json"),
            "status": PropertySchema(type="string"),
        },
    )
    entry_pin = _owned_pin("contract-in", empty)
    output_pin = _owned_pin("contract-out", opaque)
    query = _pin("query", "QueryDefinition", "json-accepted-items")
    definition = ProcedureDefinition(
        name="json-escape-hatch",
        contract_in=named(entry_pin),
        contract_out=named(output_pin),
        nodes=(
            ReadNode(
                node_id="read",
                query=named(query),
                parameters={},
                as_="rows",
                next="project",
            ),
            ProjectNode(
                node_id="project",
                fields={"items": "$steps.rows.result.rows", "status": "ok"},
                contract_out=named(output_pin),
                as_="result",
            ),
        ),
        returns="result",
        budget=_budget(items=1),
        hard_caps=_hard_caps(items=1),
        terminal_capability=1,
    )
    accepted = _owned_accepted(
        definition,
        contracts=(empty, opaque),
        pins=(entry_pin, output_pin, query),
    )
    prepared = _prepare(
        accepted,
        fixture,
        _StateReader({"items": [{"id": "one"}, {"id": "two"}]}),
        invocation_input={},
    )
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=OwnedProcedureContractValidator(accepted),
    ).execute(prepared, accepted)
    assert result.status == "succeeded"
    assert result.refusal is None
    assert result.output == {
        "items": _query_rows({"items": [{"id": "one"}, {"id": "two"}]}),
        "status": "ok",
    }


def test_noncurrent_procedure_refuses_before_any_journal_record(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    prepared = _prepare(accepted, fixture, _StateReader())
    with pytest.raises(ExecutionError, match="not current"):
        ProcedureExecutor(
            journal=fixture.journal,
            bodies=fixture.bodies,
            run_index=fixture.run_index,
            fencing_token="writer",
            activation_authority=_Authority(_digest("superseded")),
            contract_validator=_Contracts(),
        ).execute(prepared, accepted)
    assert fixture.journal.all_records(fixture.stream, "runs") == ()


def test_deleted_run_index_rebuilds_without_changing_retry_answer(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    prepared = _prepare(accepted, fixture, _StateReader())
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    )
    first = executor.execute(prepared, accepted)

    index_path = fixture.run_index.path
    fixture.run_index.close()
    index_path.unlink()
    rebuilt = ProcedureRunIndex(index_path)
    replay = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=rebuilt,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(prepared, accepted)

    assert replay == first
    assert rebuilt.get(prepared.admission.run_id) is not None


class _CrashingClock:
    def __init__(self) -> None:
        self.now_calls = 0

    def now(self):
        self.now_calls += 1
        if self.now_calls == 2:
            raise KeyboardInterrupt("simulated process crash")
        return NOW

    def monotonic_ns(self):
        return 0


def test_incomplete_attempt_is_recovered_as_typed_no_redispatch_refusal(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    prepared = _prepare(accepted, fixture, _StateReader())
    crashing = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        clock=_CrashingClock(),
    )
    with pytest.raises(KeyboardInterrupt, match="simulated process crash"):
        crashing.execute(prepared, accepted)
    assert len(fixture.journal.all_records(fixture.stream, "runs")) == 1

    with pytest.raises(ExecutionError, match="run_recovery_required"):
        ProcedureExecutor(
            journal=fixture.journal,
            bodies=fixture.bodies,
            run_index=fixture.run_index,
            fencing_token="writer",
            activation_authority=_Authority(accepted.artifact_digest),
            contract_validator=_Contracts(),
        ).execute(prepared, accepted)


@pytest.mark.parametrize("cap,expected", [(1024, "refused"), (2 * 1024 * 1024, "succeeded")])
def test_runtime_result_budget_survives_execution_and_reopen(tmp_path, cap, expected):
    from cruxible_client.contracts.procedure_runtime_policy import ProcedureRuntimePolicy

    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    policy = ProcedureRuntimePolicy(
        provider_output_bytes_cap=4 * 1024 * 1024, result_bytes_cap=cap, repeat_attempts_cap=100
    )
    prepared = _prepare(
        accepted, fixture, _StateReader({"items": [{"id": "x" * 1_100_000}]}), runtime_policy=policy
    )
    assert prepared.admission.resource_budget.result_bytes_cap == cap
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    )
    result = executor.execute(prepared, accepted)
    assert result.status == expected
    reopened = _Fixture(
        journal=LocalJournalBackend(tmp_path / "journal"),
        bodies=ContentAddressedBodyStore(tmp_path / "cas"),
        stream=fixture.stream,
        run_index=ProcedureRunIndex(tmp_path / "run-index.sqlite"),
    )
    replay = ProcedureExecutor(
        journal=reopened.journal,
        bodies=reopened.bodies,
        run_index=reopened.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(prepared, accepted)
    assert replay.status == expected
    assert replay.output == result.output


def test_resource_budget_intersection_and_replay_identity(tmp_path):
    from cruxible_client.contracts.procedure_runtime_policy import ProcedureRuntimePolicy
    from cruxible_core.procedures.execution import resolve_procedure_resource_budget

    policy = ProcedureRuntimePolicy(
        provider_output_bytes_cap=1024, result_bytes_cap=10000, repeat_attempts_cap=100
    )
    budget = _budget().model_copy(update={"max_result_bytes": 8000})
    caps = _hard_caps().model_copy(update={"max_result_bytes": 6000, "max_repeat_attempts": 200})
    effective = resolve_procedure_resource_budget(budget, caps, policy)
    assert effective.result_bytes_cap == 6000
    assert effective.repeat_attempts_cap == 100
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    first = _prepare(accepted, fixture, _StateReader(), runtime_policy=policy)
    second = _prepare(
        accepted,
        fixture,
        _StateReader(),
        runtime_policy=policy.model_copy(update={"result_bytes_cap": 20000}),
    )
    assert first.admission.run_id != second.admission.run_id
    legacy = _prepare(accepted, fixture, _StateReader())
    assert "resource_budget" not in legacy.admission.model_dump(mode="json")
    assert "max_result_bytes" not in _budget().model_dump(mode="json")


def test_repeat_above_legacy_ceiling_uses_admitted_policy(tmp_path):
    from cruxible_client.contracts.procedure_runtime_policy import ProcedureRuntimePolicy
    from cruxible_core.procedures.execution import ProcedureBoundaryRefused

    fixture = _fixture(tmp_path)
    original = _repeat_transform_procedure()
    definition = original.procedure.definition
    definition = definition.model_copy(
        update={
            "nodes": (definition.nodes[0].model_copy(update={"max_attempts": 30}),),
            "hard_caps": definition.hard_caps.model_copy(update={"max_repeat_attempts": 30}),
        }
    )
    accepted = _accepted(definition, pins=original.procedure.pins)  # the original's bound pins
    policy = ProcedureRuntimePolicy(
        provider_output_bytes_cap=1024, result_bytes_cap=10000, repeat_attempts_cap=30
    )
    prepared = _prepare(accepted, fixture, _StateReader(), runtime_policy=policy)
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    )
    assert executor.execute(prepared, accepted).status == "succeeded"
    smaller = _prepare(
        accepted,
        fixture,
        _StateReader(),
        runtime_policy=policy.model_copy(update={"repeat_attempts_cap": 25}),
    )
    with pytest.raises(ProcedureBoundaryRefused, match="Repeat exceeds"):
        executor.execute(smaller, accepted)
