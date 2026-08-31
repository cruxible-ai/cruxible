"""Authenticated graph-v3 execution and log-sufficiency laws."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import TypeAdapter

import cruxible_core.playbill.procedures.execution as execution_module
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
from cruxible_client.contracts.captures import CanonicalDurationV1
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
from cruxible_client.contracts.procedures.models import (
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
from cruxible_client.contracts.query.grammar import QueryBudgetsV1
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.playbill.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    LocalJournalBackend,
    parse_journal_payload,
)
from cruxible_core.playbill.procedures.execution import (
    PROCEDURE_RESULT_MAX_BYTES,
    ProcedureExecutor,
    ProcedureRunRefusalV1,
    ProviderInvocationResultV1,
    StateTapReadResultV1,
    _apply_transform,
    _check_return_budget,
    _extract_items,
    _RunRefusal,
    prepare_direct_procedure_run,
    procedure_semantic_replay_key_digest,
    procedure_semantic_result_digest,
)
from cruxible_core.playbill.procedures.run_index import ProcedureRunIndex
from cruxible_core.playbill.projection import AcceptedCoordinate

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
                "items": PropertySchema(
                    type="list",
                    item_fields={"identifier": PropertySchema(type="string")},
                )
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
                        transform_kind="shape_items",
                        contract_in=body_input,
                        contract_out=body_output,
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
