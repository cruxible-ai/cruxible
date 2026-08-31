"""Authenticated graph-v3 execution and log-sufficiency laws."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import TypeAdapter

import cruxible_client.contracts.procedures.results as procedure_result_contracts
import cruxible_core.playbill.procedures.execution as execution_module
import cruxible_core.service.playbill_procedure_runs as procedure_run_service
from cruxible_client.contracts.acquisition_policies import AcquisitionInputDecisionV1
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
    CanonicalDurationV1,
    CaptureRetentionErasurePolicyV1,
)
from cruxible_client.contracts.errors import PlaybillExecutionError
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedureV1,
    ProcedureArtifactV1,
    ProcedureArtifactV2,
    ProcedureOwnedContractV1,
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
    compute_procedure_definition_digest_v3,
)
from cruxible_client.contracts.procedures.line_specs import (
    AcceptedLineSpecV1,
    LineSpecV1,
    ManualTriggerPolicyV1,
    line_spec_digest,
    line_spec_path,
)
from cruxible_client.contracts.procedures.models import (
    ExhaustTapNodeV3,
    GuardNodeV3,
    GuardPredicateV1,
    HaltNodeV3,
    PredicateOperandV1,
    ProcedureBudgetV3,
    ProcedureDefinitionV3,
    ProcedureHardCapsV3,
    ProcedureTransformSpecV1,
    ProjectNodeV3,
    ProviderNodeV3,
    RepeatBodyNodeV3,
    RepeatNodeV3,
    SourceNodeV3,
    StateTapNodeV3,
    TransformNodeV3,
)
from cruxible_client.contracts.procedures.results import (
    ProcedureAdmissionRefusalV1,
    ProcedureRunReceiptV4,
)
from cruxible_client.contracts.query.grammar import QueryBudgetsV1
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.playbill.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    LocalJournalBackend,
    ProcedureExhaustWriter,
    parse_journal_payload,
)
from cruxible_core.playbill.exhaust.line_track_records import LineTrackRecordReducer
from cruxible_core.playbill.exhaust.promotions import VerifiedExhaustRecordV1
from cruxible_core.playbill.material_reservations import (
    ProcedureMaterialRecoveryRequired,
    ProcedureMaterialReservationStore,
    RunMaterialReservationV1,
    reserve_admission_material_body,
)
from cruxible_core.playbill.procedures.execution import (
    PROCEDURE_RESULT_MAX_BYTES,
    ExhaustRunMaterialV1,
    PreparedProcedureRunV3,
    ProcedureAdmissionBoundPayloadV2,
    ProcedureAdmissionBoundPayloadV3,
    ProcedureAdmissionMaterialManifestV1,
    ProcedureAdmissionMaterialMemberV1,
    ProcedureExecutor,
    ProcedureProviderBindingV1,
    ProcedureRunAdmissionV2,
    ProcedureRunAdmissionV3,
    ProcedureRunRefusalV1,
    ProcedureSelectionDecisionV1,
    ProviderInvocationResultV1,
    StateTapReadResultV1,
    _apply_transform,
    _check_return_budget,
    _extract_items,
    _RunRefusal,
    capture_admission_material_member,
    prepare_direct_procedure_run,
    procedure_admission_digest,
    procedure_admission_material_digest,
    procedure_line_journal_stream,
    procedure_line_partition,
    procedure_line_run_id,
    procedure_replay_input_vector,
    procedure_selection_decision_digest,
    procedure_semantic_replay_key_digest,
    procedure_semantic_result_digest,
    run_value_digest,
    verify_line_admission_spec,
)
from cruxible_core.playbill.procedures.input_planes import (
    ExhaustRunInputV1,
    LandedCaptureRunInputV1,
)
from cruxible_core.playbill.procedures.run_index import ProcedureRunIndex
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.service.playbill_procedure_runs import service_prepare_playbill_line_admission
from tests.test_playbill._support import initialize_local

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


def _budget(*, providers: int = 0, items: int = 100) -> ProcedureBudgetV3:
    return ProcedureBudgetV3(
        wall_clock=CanonicalDurationV1(microseconds=1_000_000),
        max_provider_calls=providers,
        max_capture_bytes=0,
        max_items=items,
    )


def _hard_caps(*, providers: int = 0, items: int = 100) -> ProcedureHardCapsV3:
    return ProcedureHardCapsV3(
        max_wall_clock=CanonicalDurationV1(microseconds=2_000_000),
        max_provider_calls=providers,
        max_capture_bytes=0,
        max_items=items,
        max_repeat_attempts=3,
    )


def _accepted(
    definition: ProcedureDefinitionV3,
    *,
    pins: tuple[ArtifactPin, ...],
    activation_policy: str = "abort",
) -> AcceptedProcedureV1:
    procedure = ProcedureArtifactV1(
        identity=ArtifactIdentity(kind="Procedure", name=definition.name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest_v3(definition).tagged,
        pins=tuple(
            sorted(
                pins,
                key=lambda pin: (
                    pin.role.encode(),
                    pin.target.qualified.encode(),
                    pin.artifact_digest.encode(),
                ),
            )
        ),
        activation_policy=activation_policy,  # type: ignore[arg-type]
    )
    return AcceptedProcedureV1(
        path=procedure_path(definition.name),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )


def _state_procedure(*, false_branch: bool = False, max_items: int = 100) -> AcceptedProcedureV1:
    contract_in = _pin("contract-in", "Contract", "input")
    contract_out = _pin("contract-out", "Contract", "output")
    query = _pin("query", "QueryDefinition", "accepted-items")
    predicate = GuardPredicateV1(
        left=PredicateOperandV1(kind="count", alias="rows"),
        operator="gt",
        right=PredicateOperandV1(kind="literal", value=100 if false_branch else 0),
    )
    definition = ProcedureDefinitionV3(
        name="state-procedure",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            StateTapNodeV3(
                node_id="read",
                query=query,
                parameters={"status": "accepted"},
                as_="rows",
                next="gate",
            ),
            GuardNodeV3(
                node_id="gate",
                predicate=predicate,
                on_true="project",
                on_false="$abort",
                refusal_code="no-items",
                message="No accepted items satisfy the query.",
            ),
            ProjectNodeV3(
                node_id="project",
                fields={"items": "$steps.rows.items", "status": "ok"},
                contract_out=contract_out,
                as_="result",
            ),
        ),
        returns="result",
        budget=_budget(items=max_items),
        hard_caps=_hard_caps(items=max_items),
        terminal_capability=1,
    )
    return _accepted(definition, pins=(contract_in, contract_out, query))


def _exhaust_procedure() -> AcceptedProcedureV1:
    contract_in = _pin("contract-in", "Contract", "input")
    contract_out = _pin("contract-out", "Contract", "output")
    reducer = ArtifactPin(
        role="reducer",
        target=ArtifactIdentity(kind="Reducer", name="upstream"),
        artifact_digest=_digest("upstream-reducer"),
    )
    definition = ProcedureDefinitionV3(
        name="exhaust-procedure",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            ExhaustTapNodeV3(
                node_id="read-prior",
                reducer_or_query=reducer,
                journal_identity="upstream-journal",
                as_="prior_rows",
                next="project",
            ),
            ProjectNodeV3(
                node_id="project",
                fields={"items": "$steps.prior_rows.rows", "status": "ok"},
                contract_out=contract_out,
                as_="result",
            ),
        ),
        returns="result",
        budget=_budget(),
        hard_caps=_hard_caps(),
        terminal_capability=1,
    )
    return _accepted(definition, pins=(contract_in, contract_out, reducer))


def _owned_contract(name: str, fields: dict[str, PropertySchema]) -> ProcedureOwnedContractV1:
    return ProcedureOwnedContractV1(
        identity=ArtifactIdentity(kind="Contract", name=name),
        schema=ContractSchema(fields=fields),
    )


def _owned_pin(role: str, contract: ProcedureOwnedContractV1) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=contract.identity,
        artifact_digest=procedure_owned_contract_digest(contract).tagged,
    )


def _owned_accepted(
    definition: ProcedureDefinitionV3,
    *,
    contracts: tuple[ProcedureOwnedContractV1, ...],
    pins: tuple[ArtifactPin, ...],
) -> AcceptedProcedureV1:
    unique_pins = {(pin.role, pin.target.qualified, pin.artifact_digest): pin for pin in pins}
    procedure = ProcedureArtifactV2(
        identity=ArtifactIdentity(kind="Procedure", name=definition.name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest_v3(definition).tagged,
        pins=tuple(
            unique_pins[key]
            for key in sorted(
                unique_pins,
                key=lambda item: tuple(member.encode("utf-8") for member in item),
            )
        ),
        owned_contracts=tuple(
            sorted(
                contracts,
                key=lambda contract: canonical_bytes(contract.model_dump(mode="json")),
            )
        ),
        activation_policy="abort",
    )
    return AcceptedProcedureV1(
        path=procedure_path(definition.name),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )


def _owned_boundary_procedure(boundary: str) -> AcceptedProcedureV1:
    empty = _owned_contract("empty-input", {})
    opaque = _owned_contract("opaque-output", {"items": PropertySchema(type="json")})
    typed = _owned_contract(
        f"list-{boundary}",
        {
            "items": PropertySchema(
                type="list",
                item_fields={"id": PropertySchema(type="string")},
            )
        },
    )
    entry_pin = _owned_pin("contract-in", empty)
    opaque_out = _owned_pin("contract-out", opaque)
    typed_in = _owned_pin("contract-in", typed)
    typed_out = _owned_pin("contract-out", typed)
    query = _pin("query", "QueryDefinition", f"accepted-items-{boundary}")
    read = StateTapNodeV3(
        node_id="read",
        query=query,
        parameters={},
        as_="rows",
        next="compute",
    )
    if boundary == "contract-in":
        compute = TransformNodeV3(
            node_id="compute",
            transform_kind="adapter",
            contract_in=typed_in,
            contract_out=opaque_out,
            spec={"tag": "playbill-transform-adapter-spec-v1", "value": "$steps.rows"},
            as_="result",
        )
        return_pin = opaque_out
        contracts = (empty, opaque, typed)
        pins = (entry_pin, typed_in, opaque_out, query)
    else:
        compute = ProjectNodeV3(
            node_id="compute",
            fields={"items": "$steps.rows.items"},
            contract_out=typed_out if boundary == "contract-out" else opaque_out,
            as_="result",
        )
        return_pin = typed_out
        contracts = (empty, typed) if boundary == "contract-out" else (empty, opaque, typed)
        pins = (entry_pin, compute.contract_out, return_pin, query)
    definition = ProcedureDefinitionV3(
        name=f"owned-{boundary}",
        contract_in=entry_pin,
        contract_out=return_pin,
        nodes=(read, compute),
        returns="result",
        budget=_budget(items=1),
        hard_caps=_hard_caps(items=1),
        terminal_capability=1,
    )
    return _owned_accepted(definition, contracts=contracts, pins=pins)


def _transform_spec(kind: str, spec: object) -> ProcedureTransformSpecV1:
    if kind != "adapter":
        assert isinstance(spec, dict)
    payload = (
        {"tag": "playbill-transform-adapter-spec-v1", "value": spec}
        if kind == "adapter"
        else {"tag": f"playbill-transform-{kind.replace('_', '-')}-spec-v1", **spec}
    )
    return TypeAdapter(ProcedureTransformSpecV1).validate_python(payload)


def _transform_procedure(kind: str, spec: object) -> AcceptedProcedureV1:
    contract_in = _pin("contract-in", "Contract", "input")
    contract_out = _pin("contract-out", "Contract", "output")
    definition = ProcedureDefinitionV3(
        name=f"{kind.replace('_', '-')}-procedure",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            TransformNodeV3(
                node_id="transform",
                transform_kind=kind,  # type: ignore[arg-type]
                contract_in=contract_in,
                contract_out=contract_out,
                spec=_transform_spec(kind, spec),
                as_="result",
            ),
        ),
        returns="result",
        budget=_budget(),
        hard_caps=_hard_caps(),
        terminal_capability=1,
    )
    return _accepted(definition, pins=(contract_in, contract_out))


def _repeat_transform_procedure(*, max_items: int = 100) -> AcceptedProcedureV1:
    contract_in = _pin("contract-in", "Contract", "input")
    contract_out = _pin("contract-out", "Contract", "output")
    definition = ProcedureDefinitionV3(
        name="repeat-transform-procedure",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            RepeatNodeV3(
                node_id="repeat",
                max_attempts=2,
                body=(
                    RepeatBodyNodeV3(
                        node_id="shape-body",
                        operation="transform",
                        transform_kind="shape_items",
                        contract_in=contract_in,
                        contract_out=contract_out,
                        spec={
                            "tag": "playbill-transform-shape-items-spec-v1",
                            "items": [{"id": "a"}, {"id": "b"}],
                            "fields": {"identifier": "$item.id"},
                        },
                        as_="shaped",
                    ),
                ),
                until=GuardPredicateV1(
                    left=PredicateOperandV1(kind="exists", alias="shaped"),
                    operator="eq",
                    right=PredicateOperandV1(kind="literal", value=True),
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


def _owned_repeat_transform_boundary_procedure(boundary: str) -> AcceptedProcedureV1:
    empty = _owned_contract("repeat-empty-input", {})
    opaque = ProcedureOwnedContractV1(
        identity=ArtifactIdentity(kind="Contract", name="repeat-opaque"),
        schema=ContractSchema(fields={}, allow_extra=True),
    )
    list_input = ProcedureOwnedContractV1(
        identity=ArtifactIdentity(kind="Contract", name="repeat-list-input"),
        schema=ContractSchema(
            fields={
                "items": PropertySchema(
                    type="list",
                    item_fields={"id": PropertySchema(type="string")},
                )
            },
            allow_extra=True,
        ),
    )
    list_output = ProcedureOwnedContractV1(
        identity=ArtifactIdentity(kind="Contract", name="repeat-list-output"),
        schema=ContractSchema(
            fields={
                "auxiliary": PropertySchema(
                    type="list",
                    item_fields={"label": PropertySchema(type="string")},
                ),
                "items": PropertySchema(
                    type="list",
                    item_fields={"identifier": PropertySchema(type="string")},
                ),
            },
            allow_extra=True,
        ),
    )
    entry_pin = _owned_pin("contract-in", empty)
    return_pin = _owned_pin("contract-out", opaque)
    body_input = _owned_pin(
        "contract-in",
        list_input if boundary == "contract-in" else opaque,
    )
    body_output = _owned_pin(
        "contract-out",
        list_output if boundary == "contract-out" else opaque,
    )
    transform_kind = "adapter" if boundary == "contract-out" else "shape_items"
    spec = (
        {
            "tag": "playbill-transform-adapter-spec-v1",
            "value": {
                "auxiliary": [],
                "items": [{"identifier": "a"}, {"identifier": "b"}],
            },
        }
        if boundary == "contract-out"
        else {
            "tag": "playbill-transform-shape-items-spec-v1",
            "items": [{"id": "a"}, {"id": "b"}],
            "fields": {"identifier": "$item.id"},
        }
    )
    definition = ProcedureDefinitionV3(
        name=f"repeat-{boundary}-budget",
        contract_in=entry_pin,
        contract_out=return_pin,
        nodes=(
            RepeatNodeV3(
                node_id="repeat",
                max_attempts=1,
                body=(
                    RepeatBodyNodeV3(
                        node_id="shape-body",
                        operation="transform",
                        transform_kind=transform_kind,
                        contract_in=body_input,
                        contract_out=body_output,
                        spec=spec,
                        as_="shaped",
                    ),
                ),
                until=GuardPredicateV1(
                    left=PredicateOperandV1(kind="exists", alias="shaped"),
                    operator="eq",
                    right=PredicateOperandV1(kind="literal", value=True),
                ),
                as_="repeated",
            ),
        ),
        returns="repeated",
        budget=_budget(items=1),
        hard_caps=_hard_caps(items=1),
        terminal_capability=1,
    )
    contracts = tuple(
        {contract.identity.name: contract for contract in (empty, opaque, list_input, list_output)}[
            name
        ]
        for name in sorted(
            {
                empty.identity.name,
                opaque.identity.name,
                (list_input if boundary == "contract-in" else list_output).identity.name,
            }
        )
    )
    return _owned_accepted(
        definition,
        contracts=contracts,
        pins=(entry_pin, return_pin, body_input, body_output),
    )


def _typed_transform_node(**values: object) -> TransformNodeV3:
    return TransformNodeV3.model_validate(values)


def _guarded_filter_procedure(
    *,
    operator: str,
    result_next: str | None = None,
) -> AcceptedProcedureV1:
    item_fields = {
        "id": PropertySchema(type="string"),
        "keep": PropertySchema(type="bool"),
    }
    empty = _owned_contract("guard-empty-input", {})
    filter_spec = _owned_contract(
        "guard-filter-spec",
        {
            "items": PropertySchema(type="list", item_fields=item_fields),
            "where": PropertySchema(type="json"),
        },
    )
    filtered_result = _owned_contract(
        "guard-filtered-result",
        {
            "items": PropertySchema(type="list", item_fields=item_fields),
            "input_count": PropertySchema(type="int"),
            "output_count": PropertySchema(type="int"),
        },
    )
    aggregate_spec = _owned_contract(
        "guard-aggregate-spec",
        {"items": PropertySchema(type="list", item_fields=item_fields)},
    )
    count_result = _owned_contract(
        "guard-count-result",
        {"count": PropertySchema(type="int")},
    )
    entry_pin = _owned_pin("contract-in", empty)
    filter_in = _owned_pin("contract-in", filter_spec)
    filter_out = _owned_pin("contract-out", filtered_result)
    aggregate_in = _owned_pin("contract-in", aggregate_spec)
    result_pin = _owned_pin("contract-out", count_result)
    definition = ProcedureDefinitionV3(
        name=f"guarded-filter-{operator}{'-explicit-next' if result_next else ''}",
        contract_in=entry_pin,
        contract_out=result_pin,
        nodes=(
            _typed_transform_node(
                node_id="filter",
                transform_kind="filter_items",
                contract_in=filter_in,
                contract_out=filter_out,
                spec={
                    "tag": "playbill-transform-filter-items-spec-v1",
                    "items": [
                        {"id": "one", "keep": True},
                        {"id": "two", "keep": False},
                    ],
                    "where": {"keep": True},
                },
                as_="filtered",
                next="gate",
            ),
            GuardNodeV3(
                node_id="gate",
                predicate=GuardPredicateV1(
                    left=PredicateOperandV1(kind="count", alias="filtered"),
                    operator=operator,  # type: ignore[arg-type]
                    right=PredicateOperandV1(kind="literal", value=0),
                ),
                on_true="aggregate",
                on_false="stop",
                refusal_code="guard.no_filtered_items",
                message="No filtered items are available.",
            ),
            _typed_transform_node(
                node_id="aggregate",
                transform_kind="aggregate_items",
                contract_in=aggregate_in,
                contract_out=result_pin,
                spec={
                    "tag": "playbill-transform-aggregate-items-spec-v1",
                    "items": "$steps.filtered.items",
                },
                as_="result",
                next=result_next,
            ),
            HaltNodeV3(node_id="stop", reason="No filtered items."),
        ),
        returns="result",
        budget=_budget(),
        hard_caps=_hard_caps(),
        terminal_capability=1,
    )
    return _owned_accepted(
        definition,
        contracts=(empty, filter_spec, filtered_result, aggregate_spec, count_result),
        pins=(entry_pin, filter_in, filter_out, aggregate_in, result_pin),
    )


def _guarded_scalar_procedure(
    *,
    alias: str = "summary",
    path: tuple[str, ...] = ("count",),
) -> AcceptedProcedureV1:
    empty = _owned_contract("scalar-empty-input", {})
    count_result = _owned_contract(
        "scalar-count-result",
        {"count": PropertySchema(type="int")},
    )
    entry_pin = _owned_pin("contract-in", empty)
    count_in = _owned_pin("contract-in", count_result)
    count_out = _owned_pin("contract-out", count_result)
    definition = ProcedureDefinitionV3(
        name="guarded-scalar-" + alias + "-" + "-".join(path),
        contract_in=entry_pin,
        contract_out=count_out,
        nodes=(
            _typed_transform_node(
                node_id="summarize",
                transform_kind="adapter",
                contract_in=count_in,
                contract_out=count_out,
                spec={
                    "tag": "playbill-transform-adapter-spec-v1",
                    "value": {"count": 1},
                },
                as_="summary",
                next="gate",
            ),
            GuardNodeV3(
                node_id="gate",
                predicate=GuardPredicateV1(
                    left=PredicateOperandV1(kind="step", alias=alias, path=path),
                    operator="gt",
                    right=PredicateOperandV1(kind="literal", value=0),
                ),
                on_true="emit",
                on_false="stop",
                refusal_code="guard.nonpositive_summary",
                message="The summary count is not positive.",
            ),
            _typed_transform_node(
                node_id="emit",
                transform_kind="adapter",
                contract_in=count_in,
                contract_out=count_out,
                spec={
                    "tag": "playbill-transform-adapter-spec-v1",
                    "value": "$steps.summary",
                },
                as_="result",
            ),
            HaltNodeV3(node_id="stop", reason="Summary count was not positive."),
        ),
        returns="result",
        budget=_budget(),
        hard_caps=_hard_caps(),
        terminal_capability=1,
    )
    return _owned_accepted(
        definition,
        contracts=(empty, count_result),
        pins=(entry_pin, count_in, count_out),
    )


def _provider_procedure(
    *,
    effectful: bool,
    activation_policy: str = "epoch-check",
    max_provider_calls: int = 1,
):
    contract_in = _pin("contract-in", "Contract", "input")
    contract_out = _pin("contract-out", "Contract", "output")
    provider = _pin("provider", "Provider", "calculator")
    environment = _pin("environment", "EnvironmentManifest", "python")
    effect_policy = _pin("effect-policy", "EffectPolicy", "network") if effectful else None
    definition = ProcedureDefinitionV3(
        name="provider-procedure",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            ProviderNodeV3(
                node_id="invoke",
                provider=provider,
                contract_in=contract_in,
                contract_out=contract_out,
                environment=environment,
                effect_policy=effect_policy,
                input="$input",
                as_="result",
            ),
        ),
        returns="result",
        budget=_budget(providers=max_provider_calls),
        hard_caps=_hard_caps(providers=1),
        terminal_capability=1,
    )
    return _accepted(
        definition,
        pins=tuple(
            item
            for item in (contract_in, contract_out, provider, environment, effect_policy)
            if item is not None
        ),
        activation_policy=activation_policy,
    )


class _StateReader:
    def __init__(self, value=None) -> None:
        self.value = value or {"items": [{"id": "one"}]}
        self.calls = []

    def read_accepted_state(self, *, query, parameters, coordinate):
        self.calls.append((query, parameters, coordinate))
        return StateTapReadResultV1(
            value=self.value,
            effective_budgets=QueryBudgetsV1(
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


class _ChangingAuthority(_Authority):
    def current_procedure_digest(self, identity, *, coordinate):
        self.calls += 1
        return self.digest if self.calls == 1 else _digest("superseded")


class _Provider:
    def __init__(self, journal: LocalJournalBackend, stream: JournalStreamIdentityV1) -> None:
        self.journal = journal
        self.stream = stream
        self.calls = 0

    def execute_provider(self, **kwargs):
        self.calls += 1
        records = self.journal.all_records(self.stream, "runs")
        assert records[-1].record.event_kind == "effect_intent"
        return ProviderInvocationResultV1(
            output={"answer": kwargs["payload"]},
            trace={"provider_call": self.calls},
        )


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


def _prepare(accepted: AcceptedProcedureV1, fixture: _Fixture, reader: _StateReader, **kwargs):
    return prepare_direct_procedure_run(
        accepted,
        instance_id="instance-a",
        run_id=kwargs.get("run_id", "run-a"),
        accepted_coordinate=_coordinate(),
        invocation_input=kwargs.get("invocation_input", {"value": 7}),
        actor_context=kwargs.get("actor_context", _actor()),
        state_reader=reader,
        bodies=fixture.bodies,
        journal_stream=fixture.stream,
        journal_partition_id=kwargs.get("journal_partition_id", "runs"),
        admitted_at=NOW,
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


def _line_admission(
    accepted: AcceptedProcedureV1,
    fixture: _Fixture,
    *,
    occurrence_id: str = "OCC-0001",
    occurrence_evaluation_time: datetime = NOW,
    admitted_at: datetime = NOW,
    attempt: int = 1,
    landed_capture_inputs: tuple[LandedCaptureRunInputV1, ...] = (),
    exhaust_inputs: tuple[ExhaustRunInputV1, ...] = (),
) -> ProcedureRunAdmissionV3:
    direct = _prepare(accepted, fixture, _StateReader()).admission
    policy_digest = _digest("acquisition-policy")
    decision = ProcedureSelectionDecisionV1(
        policy_digest=policy_digest,
        verdict="selected",
        decisions=(),
    )
    line_identity = ArtifactIdentity(kind="Line", name="daily-triage")
    fields = {name: getattr(direct, name) for name in type(direct).model_fields if name != "tag"}
    fields.update(
        {
            "run_id": "RUN-" + "0" * 64,
            "attempt": attempt,
            "invocation_origin": "line",
            "journal_stream": procedure_line_journal_stream("instance-a"),
            "journal_partition_id": procedure_line_partition(line_identity),
            "line_spec_digest": _digest("line-spec"),
            "occurrence_id": occurrence_id,
            "deployment_snapshot_digest": _digest("deployment"),
            "acquisition_policy_digest": policy_digest,
            "selection_receipt_digest": _digest("selection-receipt"),
            "sensitivity_policy_digest": _digest("sensitivity"),
            "mandate_coordinate_digest": _digest("mandate"),
            "calibration_coordinate_digest": _digest("calibration"),
            "taint_labels": ("sensitive",),
            "epsilon_member": True,
            "admitted_at": admitted_at,
            "line_identity": line_identity,
            "occurrence_evaluation_time": occurrence_evaluation_time,
            "resolved_provider_bindings": (),
            "selection_decision": decision,
            "selection_decision_digest": procedure_selection_decision_digest(decision),
            "provider_output_bytes_cap": 1_048_576,
            "landed_capture_inputs": landed_capture_inputs,
            "exhaust_inputs": exhaust_inputs,
            "semantic_replay_key_digest": "sha256:" + "0" * 64,
            "admission_binding_digest": "sha256:" + "0" * 64,
        }
    )
    provisional = ProcedureRunAdmissionV3.model_construct(**fields)
    replay_key = procedure_semantic_replay_key_digest(provisional)
    provisional = provisional.model_copy(update={"semantic_replay_key_digest": replay_key})
    admission_digest = procedure_admission_digest(provisional)
    run_id = procedure_line_run_id(
        occurrence_id=occurrence_id,
        attempt=provisional.attempt,
        admission_binding_digest=admission_digest,
        occurrence_evaluation_time=occurrence_evaluation_time,
    )
    return ProcedureRunAdmissionV3.model_validate(
        {
            **provisional.model_dump(mode="python"),
            "run_id": run_id,
            "admission_binding_digest": admission_digest,
        }
    )


def _accepted_line_for_admission(
    admission: ProcedureRunAdmissionV3,
    accepted_procedure: AcceptedProcedureV1,
) -> AcceptedLineSpecV1:
    procedure_pin = ArtifactPin(
        role="procedure",
        target=accepted_procedure.procedure.identity,
        artifact_digest=accepted_procedure.artifact_digest,
    )
    acquisition_pin = ArtifactPin(
        role="acquisition-policy",
        target=ArtifactIdentity(kind="SourceAcquisitionPolicy", name="line-policy"),
        artifact_digest=admission.acquisition_policy_digest or "",
    )
    line = LineSpecV1(
        identity=admission.line_identity,
        occurrence_epoch=1,
        procedure=procedure_pin,
        parameters={},
        slot_bindings=(),
        trigger_policy=ManualTriggerPolicyV1(),
        acquisition_policy=acquisition_pin,
        requested_terminal_rung=1,
        budgets={},
        epsilon={"$decimal": "0"},
        pins=tuple(
            sorted(
                (procedure_pin, acquisition_pin),
                key=lambda pin: (pin.role.encode(), pin.target.qualified.encode()),
            )
        ),
    )
    return AcceptedLineSpecV1(
        path=line_spec_path(line.identity.name),
        line=line,
        artifact_digest=line_spec_digest(line).tagged,
    )


def test_line_v3_run_id_is_placement_identity_not_semantic_identity(tmp_path) -> None:
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

    assert first.semantic_replay_key_digest == second.semantic_replay_key_digest
    assert first.admission_binding_digest == second.admission_binding_digest
    assert first.run_id != second.run_id
    assert first.semantic_replay_key_digest == later_attempt.semantic_replay_key_digest
    assert first.admission_binding_digest == later_attempt.admission_binding_digest
    assert first.run_id != later_attempt.run_id
    assert first.journal_partition_id == second.journal_partition_id
    with pytest.raises(ValueError, match="run_id does not reproduce"):
        ProcedureRunAdmissionV3.model_validate(
            {**first.model_dump(mode="python"), "run_id": second.run_id}
        )


def test_line_v3_admission_binds_the_exact_accepted_line_spec(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted_procedure = _state_procedure()
    admission = _line_admission(accepted_procedure, fixture)
    accepted_line = _accepted_line_for_admission(admission, accepted_procedure)
    bound = admission.model_copy(update={"line_spec_digest": accepted_line.artifact_digest})

    verify_line_admission_spec(bound, accepted_line)
    with pytest.raises(PlaybillExecutionError, match="another accepted LineSpec"):
        verify_line_admission_spec(
            bound.model_copy(update={"line_identity": ArtifactIdentity(kind="Line", name="other")}),
            accepted_line,
        )


def test_served_line_admission_binds_the_accepted_runtime_policy_or_refuses(
    tmp_path,
    monkeypatch,
) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    admission = _line_admission(accepted, fixture).model_copy(
        update={"provider_output_bytes_cap": 17}
    )
    accepted_line = _accepted_line_for_admission(admission, accepted)
    admission = admission.model_copy(update={"line_spec_digest": accepted_line.artifact_digest})
    instance, _owner = initialize_local(tmp_path)
    accepted_tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    monkeypatch.setattr(instance, "tree_at", lambda _git_oid: accepted_tree)

    bound = service_prepare_playbill_line_admission(
        instance,
        admission=admission,
        accepted_line=accepted_line,
    )

    assert isinstance(bound, ProcedureRunAdmissionV3)
    assert bound.provider_output_bytes_cap == 1_048_576
    assert bound.semantic_replay_key_digest != procedure_semantic_replay_key_digest(admission)
    assert bound.semantic_replay_key_digest == procedure_semantic_replay_key_digest(bound)
    assert bound.admission_binding_digest == procedure_admission_digest(bound)

    legacy_tree = {
        path: content
        for path, content in accepted_tree.items()
        if path != "governance/procedure-runtime-policy.yaml"
    }
    monkeypatch.setattr(instance, "tree_at", lambda _git_oid: legacy_tree)
    refused = service_prepare_playbill_line_admission(
        instance,
        admission=admission,
        accepted_line=accepted_line,
    )
    assert isinstance(refused, ProcedureAdmissionRefusalV1)
    assert refused.code == "procedure_runtime_policy_absent"

    monkeypatch.setattr(instance, "tree_at", lambda _git_oid: accepted_tree)
    mismatched = service_prepare_playbill_line_admission(
        instance,
        admission=admission,
        accepted_line=accepted_line.model_copy(update={"artifact_digest": "sha256:" + "9" * 64}),
    )
    assert isinstance(mismatched, ProcedureAdmissionRefusalV1)
    assert mismatched.code == "artifact_binding_mismatch"
    assert "another accepted LineSpec" in mismatched.message


def test_replay_carriers_have_one_client_contract_definition_source() -> None:
    for name in (
        "ProcedureAdmissionMaterialManifestV1",
        "ProcedureAdmissionMaterialMemberV1",
        "ProcedureProviderBindingV1",
        "ProcedureReplayInputProjectionV1",
        "ProcedureSelectionDecisionV1",
    ):
        assert getattr(execution_module, name) is getattr(procedure_result_contracts, name)


def test_equal_line_admission_digests_retrieve_two_exact_runs(tmp_path, monkeypatch) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    direct = _prepare(accepted, fixture, _StateReader())
    first = _line_admission(accepted, fixture)
    second = _line_admission(
        accepted,
        fixture,
        occurrence_id="OCC-0002",
        occurrence_evaluation_time=NOW + timedelta(hours=1),
        admitted_at=NOW + timedelta(hours=2),
    )
    assert first.admission_binding_digest == second.admission_binding_digest
    manifest = ProcedureAdmissionMaterialManifestV1(members=())
    prepared = tuple(
        PreparedProcedureRunV3(
            admission=admission,
            accepted_state_materials=direct.accepted_state_materials,
            admission_material_manifest=manifest,
            admission_material_manifest_digest=procedure_admission_material_digest(manifest),
        )
        for admission in (first, second)
    )
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


def test_line_v3_replay_key_includes_byte_inputs_and_excludes_provenance(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    admission = _line_admission(_state_procedure(), fixture)
    baseline = admission.semantic_replay_key_digest

    for update in (
        {"admitted_at": NOW + timedelta(days=1)},
        {"deployment_snapshot_digest": _digest("other-deployment")},
        {"selection_receipt_digest": _digest("other-selection-receipt")},
        {"occurrence_id": "OCC-other"},
        {"occurrence_evaluation_time": NOW + timedelta(days=1)},
        {"procedure_path": "procedures/provenance-only.yaml"},
    ):
        changed = admission.model_copy(update=update)
        assert procedure_semantic_replay_key_digest(changed) == baseline

    alternate_decision = ProcedureSelectionDecisionV1(
        policy_digest=admission.selection_decision.policy_digest,
        verdict="refused",
        decisions=(
            AcquisitionInputDecisionV1(
                input_name="source",
                disposition="refused",
                reason_codes=("unavailable",),
            ),
        ),
    )
    for update in (
        {"provider_output_bytes_cap": admission.provider_output_bytes_cap + 1},
        {"lane": "replay"},
        {"epsilon_member": False},
        {"selection_decision": alternate_decision},
        {"mandate_coordinate_digest": _digest("other-mandate")},
    ):
        changed = admission.model_copy(update=update)
        assert procedure_semantic_replay_key_digest(changed) != baseline


def test_line_v3_replay_key_membership_is_closed(tmp_path) -> None:
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
    decision = ProcedureSelectionDecisionV1(
        policy_digest=_digest("acquisition-policy"),
        verdict="selected",
        decisions=(
            AcquisitionInputDecisionV1(
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
    binding = ProcedureProviderBindingV1(
        node_id="provider",
        provider_artifact_digest=_digest("provider"),
        interface_artifact_digest=_digest("interface-artifact"),
        interface_digest=_digest("interface"),
        classifier_digest=_digest("classifier"),
        accepted_bucket_selectors=("public",),
        implementation_digest=_digest("implementation"),
        secret_binding_identity_digests=(_digest("secret-identity"),),
    )
    admission = _line_admission(
        _state_procedure(),
        fixture,
        landed_capture_inputs=(capture,),
        exhaust_inputs=(exhaust,),
    ).model_copy(
        update={
            "selection_decision": decision,
            "selection_decision_digest": procedure_selection_decision_digest(decision),
            "resolved_provider_bindings": (binding,),
        }
    )
    baseline = procedure_semantic_replay_key_digest(admission)
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
        "acquisition policy": {"acquisition_policy_digest": _digest("other-policy")},
        "selection decision": {
            "selection_decision": decision.model_copy(update={"verdict": "refused"})
        },
        "Provider artifact": {
            "resolved_provider_bindings": (
                binding.model_copy(update={"provider_artifact_digest": _digest("other-provider")}),
            )
        },
        "interface artifact": {
            "resolved_provider_bindings": (
                binding.model_copy(
                    update={"interface_artifact_digest": _digest("other-interface-artifact")}
                ),
            )
        },
        "interface": {
            "resolved_provider_bindings": (
                binding.model_copy(update={"interface_digest": _digest("other-interface")}),
            )
        },
        "classifier": {
            "resolved_provider_bindings": (
                binding.model_copy(update={"classifier_digest": _digest("other-classifier")}),
            )
        },
        "bucket selectors": {
            "resolved_provider_bindings": (
                binding.model_copy(update={"accepted_bucket_selectors": ("restricted",)}),
            )
        },
        "implementation": {
            "resolved_provider_bindings": (
                binding.model_copy(update={"implementation_digest": _digest("other-impl")}),
            )
        },
        "secret identity": {
            "resolved_provider_bindings": (
                binding.model_copy(
                    update={"secret_binding_identity_digests": (_digest("other-secret"),)}
                ),
            )
        },
        "mandate": {"mandate_coordinate_digest": _digest("other-mandate")},
        "calibration": {"calibration_coordinate_digest": _digest("other-calibration")},
        "sensitivity": {"sensitivity_policy_digest": _digest("other-sensitivity")},
        "lane": {"lane": "replay"},
        "taint": {"taint_labels": ("other-taint",)},
        "epsilon": {"epsilon_member": False},
        "Provider output cap": {
            "provider_output_bytes_cap": admission.provider_output_bytes_cap + 1
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
        "occurrence time": {"occurrence_evaluation_time": NOW + timedelta(hours=1)},
        "attempt": {"attempt": admission.attempt + 1},
        "actor": {
            "actor_context": admission.actor_context.model_copy(update={"operation_id": "other-op"})
        },
        "Procedure path": {"procedure_path": "procedures/other.yaml"},
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


def test_admission_material_manifest_is_sorted_and_missing_is_typed(tmp_path) -> None:
    member = ProcedureAdmissionMaterialMemberV1(
        input_name="capture",
        plane="landed_capture",
        semantic_digest=_digest("capture"),
        body_digest=None,
        retention_authority_digest=_digest("capture-contract"),
        body_retention="never_materialize",
    )
    manifest = ProcedureAdmissionMaterialManifestV1(members=(member,))
    assert procedure_admission_material_digest(manifest).startswith("sha256:")
    bodies_root = tmp_path / "material-cas"
    bodies_root.mkdir()
    bodies = ContentAddressedBodyStore(bodies_root)
    with pytest.raises(Exception) as exc_info:
        execution_module.read_admission_material_body(bodies, member)
    assert getattr(exc_info.value, "code") == "replay_material_unavailable"
    assert getattr(exc_info.value, "details")["input_name"] == "capture"

    optional = member.model_copy(update={"body_retention": "optional"})
    with pytest.raises(Exception) as optional_exc:
        execution_module.read_admission_material_body(bodies, optional)
    assert getattr(optional_exc.value, "code") == "replay_material_unavailable"

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
    assert getattr(corrupt_exc.value, "code") == "admission_material_corrupt"

    with pytest.raises(ValueError, match="never_materialize"):
        ProcedureAdmissionMaterialMemberV1(
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
    policy = CaptureRetentionErasurePolicyV1(
        body_retention="required_for_duration",
        minimum_retention=CanonicalDurationV1(microseconds=3_000_000),
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
    member = ProcedureAdmissionMaterialMemberV1(
        input_name="capture",
        plane="landed_capture",
        semantic_digest=capture_input.capture_digest,
        body_digest=_digest("manifest-body"),
        retention_authority_digest=capture_input.capture_contract_digest,
        body_retention="optional",
    )
    manifest = ProcedureAdmissionMaterialManifestV1(members=(member,))
    bound = ProcedureAdmissionBoundPayloadV3(
        admission=admission,
        admission_material_manifest=manifest,
        admission_material_manifest_digest=procedure_admission_material_digest(manifest),
    )
    assert bound.admission_material_manifest == manifest

    wrong = ProcedureAdmissionMaterialManifestV1(
        members=(member.model_copy(update={"semantic_digest": _digest("substituted")}),)
    )
    with pytest.raises(ValueError, match="disagrees with its admitted input"):
        ProcedureAdmissionBoundPayloadV3(
            admission=admission,
            admission_material_manifest=wrong,
            admission_material_manifest_digest=procedure_admission_material_digest(wrong),
        )


def test_manifest_references_remain_gc_reachable_after_lease_promotion(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    capture_input = LandedCaptureRunInputV1(
        input_name="capture",
        capture_digest=_digest("reachable-capture"),
        capture_contract_digest=_digest("reachable-contract"),
        landing_cursor="partition:0001",
    )
    admission = _line_admission(
        accepted,
        fixture,
        landed_capture_inputs=(capture_input,),
    )
    pending = reserve_admission_material_body(
        bodies=fixture.bodies,
        instance_id=admission.instance_id,
        run_id=admission.run_id,
        admission_binding_digest=admission.admission_binding_digest,
        input_name="capture",
        plane="landed_capture",
        content=b'{"retained":"capture"}',
    )
    member = ProcedureAdmissionMaterialMemberV1(
        input_name="capture",
        plane="landed_capture",
        semantic_digest=capture_input.capture_digest,
        body_digest=pending.body_digest,
        retention_authority_digest=capture_input.capture_contract_digest,
        body_retention="optional",
    )
    manifest = ProcedureAdmissionMaterialManifestV1(members=(member,))
    payload = ProcedureAdmissionBoundPayloadV3(
        admission=admission,
        admission_material_manifest=manifest,
        admission_material_manifest_digest=procedure_admission_material_digest(manifest),
    )
    fixture.journal.activate_writer(
        admission.journal_stream,
        admission.journal_partition_id,
        fencing_token="writer",
        expected_head=fixture.journal.read_head(
            admission.journal_stream,
            admission.journal_partition_id,
        ),
    )
    ProcedureExhaustWriter(
        journal=fixture.journal,
        bodies=fixture.bodies,
        fencing_token="writer",
    ).append(
        stream=admission.journal_stream,
        partition_id=admission.journal_partition_id,
        event_kind="admission_bound",
        accepted_coordinate=admission.accepted_coordinate,
        definition_digest=admission.definition_digest,
        actor_context=admission.actor_context,
        recorded_at=NOW,
        payload=payload.model_dump(mode="json"),
        procedure_artifact_digest=admission.procedure_artifact_digest,
        run_id=admission.run_id,
        admission_binding_digest=admission.admission_binding_digest,
        line_spec_digest=admission.line_spec_digest,
        occurrence_id=admission.occurrence_id,
        attempt=admission.attempt,
    )
    reservations = ProcedureMaterialReservationStore(fixture.bodies.reservation_root)
    reservations.release(pending.reservation_id)
    records = fixture.journal.all_records(
        admission.journal_stream,
        admission.journal_partition_id,
    )

    reachable = reservations.reachable_body_digests(records, bodies=fixture.bodies)

    assert pending.body_digest in reachable
    assert records[0].record.payload_digest in reachable

    corrupt_payload = payload.model_dump(mode="json")
    corrupt_payload["admission_material_manifest_digest"] = _digest("corrupt-manifest")
    ProcedureExhaustWriter(
        journal=fixture.journal,
        bodies=fixture.bodies,
        fencing_token="writer",
    ).append(
        stream=admission.journal_stream,
        partition_id=admission.journal_partition_id,
        event_kind="admission_bound",
        accepted_coordinate=admission.accepted_coordinate,
        definition_digest=admission.definition_digest,
        actor_context=admission.actor_context,
        recorded_at=NOW,
        payload=corrupt_payload,
        procedure_artifact_digest=admission.procedure_artifact_digest,
        run_id=admission.run_id,
        admission_binding_digest=admission.admission_binding_digest,
        line_spec_digest=admission.line_spec_digest,
        occurrence_id=admission.occurrence_id,
        attempt=admission.attempt,
    )
    corrupt_record = fixture.journal.all_records(
        admission.journal_stream,
        admission.journal_partition_id,
    )[-1]
    with pytest.raises(
        ProcedureMaterialRecoveryRequired,
        match="reachability cannot be authenticated",
    ):
        reservations.reachable_body_digests((corrupt_record,), bodies=fixture.bodies)


def test_line_v3_admission_bound_persists_manifest_not_material_values(
    tmp_path,
    monkeypatch,
) -> None:
    fixture = _fixture(tmp_path)
    accepted = _exhaust_procedure()
    direct = _prepare(accepted, fixture, _StateReader())
    exhaust_value = {"rows": [{"id": "retained"}]}
    exhaust_input = ExhaustRunInputV1(
        input_name="prior_rows",
        journal_identity="upstream-journal",
        first_cursor="partition:0001",
        last_cursor="partition:0001",
        reducer_or_query_digest=_digest("upstream-reducer"),
        result_digest=run_value_digest("exhaust-result", exhaust_value),
    )
    admission = _line_admission(accepted, fixture, exhaust_inputs=(exhaust_input,))
    pending = reserve_admission_material_body(
        bodies=fixture.bodies,
        instance_id=admission.instance_id,
        run_id=admission.run_id,
        admission_binding_digest=admission.admission_binding_digest,
        input_name=exhaust_input.input_name,
        plane="exhaust",
        content=canonical_bytes(exhaust_value),
    )
    member = ProcedureAdmissionMaterialMemberV1(
        input_name=exhaust_input.input_name,
        plane="exhaust",
        semantic_digest=exhaust_input.result_digest,
        body_digest=pending.body_digest,
        retention_authority_digest=exhaust_input.reducer_or_query_digest,
        body_retention="optional",
    )
    manifest = ProcedureAdmissionMaterialManifestV1(members=(member,))
    prepared = PreparedProcedureRunV3(
        admission=admission,
        accepted_state_materials=direct.accepted_state_materials,
        exhaust_materials=(ExhaustRunMaterialV1(input=exhaust_input, value=exhaust_value),),
        admission_material_manifest=manifest,
        admission_material_manifest_digest=procedure_admission_material_digest(manifest),
    )
    fixture.journal.activate_writer(
        fixture.stream,
        admission.journal_partition_id,
        fencing_token="writer",
        expected_head=fixture.journal.read_head(
            fixture.stream,
            admission.journal_partition_id,
        ),
    )
    reservations = ProcedureMaterialReservationStore(fixture.bodies.reservation_root)
    original_store = fixture.bodies.store

    def store_after_run_reservation(content: bytes):  # type: ignore[no-untyped-def]
        body_digest = fixture.bodies.digest_bytes(content).tagged
        assert any(
            isinstance(item, RunMaterialReservationV1) and item.body_digest == body_digest
            for item in reservations.active_locked()
        )
        return original_store(content)

    monkeypatch.setattr(fixture.bodies, "store", store_after_run_reservation)
    original_append = fixture.journal.append

    def append_while_leased(draft, **kwargs):  # type: ignore[no-untyped-def]
        active = reservations.active_locked()
        assert any(
            isinstance(item, RunMaterialReservationV1) and item.body_digest == draft.payload_digest
            for item in active
        )
        if draft.event_kind == "admission_bound":
            assert pending in active
        return original_append(draft, **kwargs)

    monkeypatch.setattr(fixture.journal, "append", append_while_leased)
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(prepared, accepted)

    assert result.status == "succeeded"
    admission_record = next(
        item
        for item in fixture.journal.all_records(
            fixture.stream,
            admission.journal_partition_id,
        )
        if item.record.event_kind == "admission_bound"
    )
    payload = parse_journal_payload(
        fixture.bodies.read(
            admission_record.record.payload_digest,
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
    )
    assert payload["tag"] == "playbill-procedure-admission-bound-payload-v3"
    assert "accepted_state_materials" not in payload
    assert payload["admission_material_manifest"]["members"] == [member.model_dump(mode="json")]
    assert ProcedureRunAdmissionV3.model_validate(payload["admission"]) == admission
    assert reservations.active() == ()

    records = fixture.journal.all_records(
        fixture.stream,
        admission.journal_partition_id,
    )
    monkeypatch.setattr(
        procedure_run_service,
        "_records_for_run",
        lambda _instance, _run_id: records,
    )

    class _Instance:
        def body_store(self):  # type: ignore[no-untyped-def]
            return fixture.bodies

    state = procedure_run_service._state_from_records(  # noqa: SLF001
        _Instance(),
        run_id=admission.run_id,
    )
    assert isinstance(state.receipt, ProcedureRunReceiptV4)
    assert state.receipt.line_identity == admission.line_identity
    assert state.receipt.admission_material_manifest.model_dump(mode="json") == manifest.model_dump(
        mode="json"
    )


def test_line_track_fold_reads_real_v2_and_v3_nested_admission_payloads(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    direct = _prepare(accepted, fixture, _StateReader())
    v3 = _line_admission(accepted, fixture)
    accepted_line = _accepted_line_for_admission(v3, accepted)
    v3 = v3.model_copy(update={"line_spec_digest": accepted_line.artifact_digest})
    v2_fields = {
        name: getattr(v3, name) for name in ProcedureRunAdmissionV2.model_fields if name != "tag"
    }
    v2_fields.update(
        {
            "run_id": "RUN-v2-line",
            "semantic_replay_key_digest": "sha256:" + "0" * 64,
            "admission_binding_digest": "sha256:" + "0" * 64,
        }
    )
    provisional_v2 = ProcedureRunAdmissionV2.model_construct(**v2_fields)
    replay_key = procedure_semantic_replay_key_digest(provisional_v2)
    provisional_v2 = provisional_v2.model_copy(update={"semantic_replay_key_digest": replay_key})
    v2 = ProcedureRunAdmissionV2.model_validate(
        {
            **provisional_v2.model_dump(mode="python"),
            "admission_binding_digest": procedure_admission_digest(provisional_v2),
        }
    )
    manifest = ProcedureAdmissionMaterialManifestV1(members=())
    payloads = (
        ProcedureAdmissionBoundPayloadV2(
            admission=v2,
            accepted_state_materials=direct.accepted_state_materials,
        ).model_dump(mode="json"),
        ProcedureAdmissionBoundPayloadV3(
            admission=v3,
            admission_material_manifest=manifest,
            admission_material_manifest_digest=procedure_admission_material_digest(manifest),
        ).model_dump(mode="json"),
    )
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
        update = {"admitted_at": first.admitted_at + timedelta(microseconds=1)}
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


_TRANSFORM_CASES = [
    (
        "adapter",
        {"arbitrary": ["bridge", {"value": 2}]},
        {"arbitrary": ["bridge", {"value": 2}]},
        None,
    ),
    (
        "shape_items",
        {
            "items": [{"id": "a", "keep": True}, {"id": "b", "keep": False}],
            "fields": {"identifier": "$item.id"},
            "include_input": True,
        },
        {
            "items": [
                {"id": "a", "identifier": "a", "keep": True},
                {"id": "b", "identifier": "b", "keep": False},
            ],
            "input_count": 2,
            "output_count": 2,
        },
        ((("items", 0),), (("items", 1),)),
    ),
    (
        "filter_items",
        {
            "items": [{"id": "a", "keep": True}, {"id": "b", "keep": False}],
            "where": {"keep": True},
        },
        {
            "items": [{"id": "a", "keep": True}],
            "input_count": 2,
            "output_count": 1,
        },
        ((("items", 0),),),
    ),
    (
        "dedupe_items",
        {
            "items": [{"id": "a", "v": 1}, {"id": "a", "v": 2}, {"id": "b"}],
            "keys": ["id"],
        },
        {
            "items": [{"id": "a", "v": 1}, {"id": "b"}],
            "input_count": 3,
            "output_count": 2,
        },
        ((("items", 0),), (("items", 2),)),
    ),
    (
        "join_items",
        {
            "left_items": [{"id": "a", "left": 1}, {"id": "b", "left": 2}],
            "right_items": [{"key": "b", "right": 3}, {"key": "a", "right": 4}],
            "left_key": "id",
            "right_key": "key",
            "fields": {"id": "$item.left.id", "value": "$item.right.right"},
        },
        {"items": [{"id": "a", "value": 4}, {"id": "b", "value": 3}], "output_count": 2},
        (
            (("left_items", 0), ("right_items", 1)),
            (("left_items", 1), ("right_items", 0)),
        ),
    ),
    (
        "aggregate_items",
        {"items": [{"id": "a"}, {"id": "b"}]},
        {"count": 2},
        None,
    ),
]


@pytest.mark.parametrize(("kind", "spec", "expected", "lineage"), _TRANSFORM_CASES)
def test_existing_transform_kernel_matrix_preserves_output_and_lineage(
    kind: str,
    spec: object,
    expected: object,
    lineage: object,
) -> None:
    assert _apply_transform(kind, spec) == (expected, lineage)


@pytest.mark.parametrize(("kind", "spec", "expected", "lineage"), _TRANSFORM_CASES)
def test_existing_transform_kernel_matrix_runs_through_procedure_executor(
    tmp_path,
    monkeypatch,
    kind: str,
    spec: object,
    expected: object,
    lineage: object,
) -> None:
    accepted = _transform_procedure(kind, spec)
    fixture = _fixture(tmp_path)
    prepared = _prepare(accepted, fixture, _StateReader())
    observed_lineage: list[object] = []
    existing_kernel = execution_module._apply_transform

    def record_kernel(*args, **kwargs):  # type: ignore[no-untyped-def]
        value, actual_lineage = existing_kernel(*args, **kwargs)
        observed_lineage.append(actual_lineage)
        return value, actual_lineage

    monkeypatch.setattr(execution_module, "_apply_transform", record_kernel)
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(prepared, accepted)

    assert result.status == "succeeded"
    assert result.output == expected
    assert observed_lineage == [lineage]


def test_repeat_body_dispatches_through_kernel_with_attempt_lineage(
    tmp_path,
    monkeypatch,
) -> None:
    accepted = _repeat_transform_procedure()
    fixture = _fixture(tmp_path)
    calls: list[str] = []
    existing_kernel = execution_module._apply_transform

    def record_kernel(kind, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(kind)
        return existing_kernel(kind, *args, **kwargs)

    monkeypatch.setattr(execution_module, "_apply_transform", record_kernel)
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(_prepare(accepted, fixture, _StateReader()), accepted)

    assert calls == ["shape_items"]
    assert result.status == "succeeded"
    assert result.output == {
        "attempts": [
            {
                "attempt": 1,
                "outputs": {
                    "shaped": {
                        "items": [{"identifier": "a"}, {"identifier": "b"}],
                        "input_count": 2,
                        "output_count": 2,
                    }
                },
                "until": True,
            }
        ],
        "final": {
            "shaped": {
                "items": [{"identifier": "a"}, {"identifier": "b"}],
                "input_count": 2,
                "output_count": 2,
            }
        },
    }
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
    assert branch["body_lineage"]["shaped"]["items"] == [[], []]  # type: ignore[index]


def test_repeat_body_does_not_charge_without_a_list_contract_path(tmp_path) -> None:
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

    assert result.status == "succeeded"
    assert result.refusal is None
    assert result.output is not None


@pytest.mark.parametrize(
    ("boundary", "expected_boundary"),
    (
        ("contract-in", "contract-in:repeat-list-input"),
        ("contract-out", "contract-out:repeat-list-output"),
    ),
)
def test_repeat_body_owned_contracts_enforce_item_budget(
    tmp_path: Path,
    boundary: str,
    expected_boundary: str,
) -> None:
    accepted = _owned_repeat_transform_boundary_procedure(boundary)
    fixture = _fixture(tmp_path)
    validator = OwnedProcedureContractValidator(accepted)
    if boundary == "contract-out":
        body = accepted.procedure.definition.nodes[0].body[0]  # type: ignore[union-attr]
        assert validator.unique_list_field_path(body.contract_out) is None

    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=validator,
    ).execute(
        _prepare(
            accepted,
            fixture,
            _StateReader(),
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


@pytest.mark.parametrize(
    ("boundary", "expected_boundary"),
    (
        ("contract-in", "contract-in:list-contract-in"),
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


@pytest.mark.parametrize(
    ("kind", "spec", "message"),
    [
        ("shape_items", {"items": "not-a-list", "fields": {}}, "requires a list"),
        ("filter_items", {"items": "not-a-list", "where": {}}, "requires a list"),
        ("dedupe_items", {"items": [], "keys": "id"}, "keys must be a string list"),
        (
            "join_items",
            {
                "left_items": [],
                "right_items": "not-a-list",
                "left_key": "id",
                "right_key": "id",
                "fields": {},
            },
            "requires a list",
        ),
        ("aggregate_items", {"items": "not-a-list"}, "requires a list"),
    ],
)
def test_existing_transform_kernel_matrix_refuses_malformed_inputs(
    kind: str,
    spec: object,
    message: str,
) -> None:
    with pytest.raises(PlaybillExecutionError, match=message):
        _apply_transform(kind, spec)


def test_existing_adapter_kernel_accepts_every_canonical_shape() -> None:
    for value in (None, True, 3, "text", [1, 2], {"items": [1, 2]}):
        assert _apply_transform("adapter", value) == (value, None)


@pytest.mark.parametrize(
    ("kind", "spec"),
    [
        ("adapter", "$item.id"),
        ("shape_items", {"items": "$item.rows", "fields": {}}),
        (
            "filter_items",
            {"items": [{"id": "a"}], "where": {"id": "$item.id"}},
        ),
        ("dedupe_items", {"items": "$item.rows", "keys": ["id"]}),
        (
            "join_items",
            {
                "left_items": "$item.left",
                "right_items": [],
                "left_key": "id",
                "right_key": "id",
                "fields": {},
            },
        ),
        ("aggregate_items", {"items": "$item.rows"}),
    ],
)
def test_item_references_outside_per_item_field_templates_fail_closed(
    tmp_path,
    kind: str,
    spec: object,
) -> None:
    accepted = _transform_procedure(kind, spec)
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
    assert result.refusal.code == "runtime_reference_unresolved"


def test_join_refuses_at_the_n_plus_one_emission(monkeypatch) -> None:
    spec = {
        "left_items": [{"id": "same", "left": index} for index in range(2)],
        "right_items": [{"id": "same", "right": index} for index in range(2)],
        "left_key": "id",
        "right_key": "id",
        "fields": {"left": "$item.left.left", "right": "$item.right.right"},
    }

    resolutions = 0
    existing_resolver = execution_module._resolve_template

    def count_resolutions(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal resolutions
        if kwargs.get("item") is not None:
            resolutions += 1
        return existing_resolver(*args, **kwargs)

    monkeypatch.setattr(execution_module, "_resolve_template", count_resolutions)
    with pytest.raises(_RunRefusal) as raised:
        _apply_transform(
            "join_items",
            spec,
            max_items=2,
            node_id="join",
            list_boundary=("contract-out:joined", "items"),
        )

    refusal = raised.value.refusal
    assert refusal.code == "budget_max_items_exceeded"
    assert refusal.node_id == "join"
    assert refusal.details == {
        "boundary": "contract-out:joined",
        "dimension": "max_items",
        "field_path": "items",
        "limit": 2,
        "observed": 3,
    }
    assert resolutions == 4


@pytest.mark.parametrize(
    ("kind", "spec"),
    [
        (
            "shape_items",
            {"items": [{"id": "a"}, {"id": "b"}], "fields": {"id": "$item.id"}},
        ),
        (
            "filter_items",
            {"items": [{"id": "a"}, {"id": "b"}], "where": {}},
        ),
        (
            "dedupe_items",
            {"items": [{"id": "a"}, {"id": "b"}], "keys": ["id"]},
        ),
    ],
)
def test_each_item_emitting_kernel_enforces_its_own_ceiling(
    monkeypatch,
    kind: str,
    spec: object,
) -> None:
    existing_extractor = execution_module._extract_items

    def extraction_without_ceiling(value, **kwargs):  # type: ignore[no-untyped-def]
        return existing_extractor(
            value,
            label=kwargs["label"],
            max_items=None,
            node_id=kwargs["node_id"],
        )

    monkeypatch.setattr(execution_module, "_extract_items", extraction_without_ceiling)
    with pytest.raises(_RunRefusal) as raised:
        _apply_transform(
            kind,
            spec,
            max_items=1,
            node_id=kind,
            list_boundary=(f"contract-out:{kind}", "items"),
        )

    assert raised.value.refusal.code == "budget_max_items_exceeded"
    assert raised.value.refusal.node_id == kind
    assert raised.value.refusal.details["observed"] == 2  # type: ignore[index]


def test_aggregate_kernel_does_not_charge_an_opaque_input_collection() -> None:
    assert _apply_transform(
        "aggregate_items",
        {"items": [{"id": "a"}, {"id": "b"}]},
        max_items=1,
        node_id="aggregate",
    ) == ({"count": 2}, None)


@pytest.mark.parametrize(
    "value",
    ([{"id": "a"}, {"id": "b"}], {"items": [{"id": "a"}, {"id": "b"}]}),
)
def test_extract_items_enforces_both_collection_shapes(value: object) -> None:
    with pytest.raises(_RunRefusal) as raised:
        _extract_items(
            value,
            label="items",
            max_items=1,
            node_id="extract",
            list_boundary=("contract-out:items", "items"),
        )

    assert raised.value.refusal.code == "budget_max_items_exceeded"
    assert raised.value.refusal.details["observed"] == 2  # type: ignore[index]


def test_max_items_is_rechecked_not_consumed_across_fanout() -> None:
    spec = {"items": [{"id": index} for index in range(80)], "where": {}}

    first, _lineage = _apply_transform("filter_items", spec, max_items=100, node_id="one")
    second, _lineage = _apply_transform("filter_items", spec, max_items=100, node_id="two")

    assert len(first["items"]) == 80  # type: ignore[index]
    assert second == first


def test_return_seam_counts_extractable_values_and_caps_all_result_bytes() -> None:
    with pytest.raises(_RunRefusal) as item_refusal:
        _check_return_budget({"items": [1, 2]}, max_items=1, node_id="return")
    assert item_refusal.value.refusal.code == "budget_max_items_exceeded"

    with pytest.raises(_RunRefusal) as byte_refusal:
        _check_return_budget(
            "x" * PROCEDURE_RESULT_MAX_BYTES,
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
    assert result.output == {"items": [{"id": "one"}], "status": "ok"}

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
    assert result.output == {"items": [{"id": "one"}], "status": "ok"}


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
        return 0 if self.calls == 1 else 2_000_000_000


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


def test_source_node_refuses_line_binding_until_pc_e2(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    contract_in = _pin("contract-in", "Contract", "input")
    contract_out = _pin("contract-out", "Contract", "output")
    capture = _pin("capture-contract", "CaptureContract", "external")
    provider = _pin("provider", "Provider", "source")
    definition = ProcedureDefinitionV3(
        name="source-procedure",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            SourceNodeV3(
                node_id="source",
                capture_contract=capture,
                provider=provider,
                request={"resource": "orders"},
                as_="result",
            ),
        ),
        returns="result",
        budget=_budget(),
        hard_caps=_hard_caps(),
        terminal_capability=1,
    )
    accepted = _accepted(definition, pins=(contract_in, contract_out, capture, provider))
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(_prepare(accepted, fixture, _StateReader()), accepted)
    assert result.status == "refused"
    assert result.refusal is not None and result.refusal.code == "line_binding_required"


def test_effect_intent_is_durable_before_dispatch_and_result_follows(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _provider_procedure(effectful=True)
    provider = _Provider(fixture.journal, fixture.stream)
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_executor=provider,
    ).execute(_prepare(accepted, fixture, _StateReader()), accepted)
    assert result.status == "succeeded"
    assert provider.calls == 1
    kinds = [item.record.event_kind for item in fixture.journal.all_records(fixture.stream, "runs")]
    assert kinds.index("effect_intent") < kinds.index("effect_result")


def test_provider_budget_refuses_before_effect_intent_or_dispatch(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _provider_procedure(effectful=True, max_provider_calls=0)
    provider = _Provider(fixture.journal, fixture.stream)
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_executor=provider,
    ).execute(_prepare(accepted, fixture, _StateReader()), accepted)

    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code == "budget_exhausted"
    assert result.refusal.budget is not None
    assert result.refusal.budget.model_dump(mode="json") == {
        "tag": "playbill-procedure-budget-refusal-detail-v1",
        "budget_kind": "max_provider_calls",
        "limit": 0,
        "observed": 1,
    }
    assert provider.calls == 0
    assert "effect_intent" not in {
        item.record.event_kind for item in fixture.journal.all_records(fixture.stream, "runs")
    }


def test_epoch_check_refuses_superseded_effect_before_intent(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _provider_procedure(effectful=True, activation_policy="epoch-check")
    provider = _Provider(fixture.journal, fixture.stream)
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_ChangingAuthority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_executor=provider,
    ).execute(_prepare(accepted, fixture, _StateReader()), accepted)
    assert result.status == "failed"
    assert provider.calls == 0
    assert "effect_intent" not in {
        item.record.event_kind for item in fixture.journal.all_records(fixture.stream, "runs")
    }


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
    definition = ProcedureDefinitionV3(
        name="json-escape-hatch",
        contract_in=entry_pin,
        contract_out=output_pin,
        nodes=(
            StateTapNodeV3(
                node_id="read",
                query=query,
                parameters={},
                as_="rows",
                next="project",
            ),
            ProjectNodeV3(
                node_id="project",
                fields={"items": "$steps.rows.items", "status": "ok"},
                contract_out=output_pin,
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
        "items": [{"id": "one"}, {"id": "two"}],
        "status": "ok",
    }


def test_noncurrent_procedure_refuses_before_any_journal_record(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    prepared = _prepare(accepted, fixture, _StateReader())
    with pytest.raises(PlaybillExecutionError, match="not current"):
        ProcedureExecutor(
            journal=fixture.journal,
            bodies=fixture.bodies,
            run_index=fixture.run_index,
            fencing_token="writer",
            activation_authority=_Authority(_digest("superseded")),
            contract_validator=_Contracts(),
        ).execute(prepared, accepted)
    assert fixture.journal.all_records(fixture.stream, "runs") == ()


def test_completed_retry_replays_result_without_redispatch_or_append(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _provider_procedure(effectful=True)
    provider = _Provider(fixture.journal, fixture.stream)
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_executor=provider,
    )
    prepared = _prepare(accepted, fixture, _StateReader())
    first = executor.execute(prepared, accepted)
    record_count = len(fixture.journal.all_records(fixture.stream, "runs"))

    retried = executor.execute(prepared, accepted)

    assert retried == first
    assert provider.calls == 1
    assert len(fixture.journal.all_records(fixture.stream, "runs")) == record_count


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

    with pytest.raises(PlaybillExecutionError, match="run_recovery_required"):
        ProcedureExecutor(
            journal=fixture.journal,
            bodies=fixture.bodies,
            run_index=fixture.run_index,
            fencing_token="writer",
            activation_authority=_Authority(accepted.artifact_digest),
            contract_validator=_Contracts(),
        ).execute(prepared, accepted)


class _CrashingProvider(_Provider):
    def execute_provider(self, **kwargs):
        super().execute_provider(**kwargs)
        raise KeyboardInterrupt("simulated effect dispatch crash")


def test_unmatched_effect_intent_never_redispatches_on_retry(tmp_path) -> None:
    fixture = _fixture(tmp_path)
    accepted = _provider_procedure(effectful=True)
    provider = _CrashingProvider(fixture.journal, fixture.stream)
    prepared = _prepare(accepted, fixture, _StateReader())
    with pytest.raises(KeyboardInterrupt, match="effect dispatch crash"):
        ProcedureExecutor(
            journal=fixture.journal,
            bodies=fixture.bodies,
            run_index=fixture.run_index,
            fencing_token="writer",
            activation_authority=_Authority(accepted.artifact_digest),
            contract_validator=_Contracts(),
            provider_executor=provider,
        ).execute(prepared, accepted)
    assert provider.calls == 1

    with pytest.raises(PlaybillExecutionError, match="unresolved durable effect intent"):
        ProcedureExecutor(
            journal=fixture.journal,
            bodies=fixture.bodies,
            run_index=fixture.run_index,
            fencing_token="writer",
            activation_authority=_Authority(accepted.artifact_digest),
            contract_validator=_Contracts(),
            provider_executor=provider,
        ).execute(prepared, accepted)
    assert provider.calls == 1
