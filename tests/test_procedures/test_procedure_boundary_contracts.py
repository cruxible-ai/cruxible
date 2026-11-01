"""A Procedure's governed boundary Contracts, checked at its boundaries only."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import ArtifactDigest, typed_digest
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    ProcedureArtifact,
    procedure_artifact_digest,
    procedure_path,
    procedure_runnability,
)
from cruxible_client.contracts.procedures.contract_schema import PropertySchema
from cruxible_client.contracts.procedures.contracts import (
    AcceptedProcedureContractValidator,
    ProcedureContractItemBudgetExceeded,
    ProcedureContractValidationError,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.models import (
    ProjectNode,
    ReadNode,
)
from cruxible_client.contracts.query.definitions import query_definition_digest
from cruxible_client.contracts.shapes import Contract
from cruxible_core.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    LocalJournalBackend,
)
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.procedures.execution import (
    _RunRefusal,
    _RunState,
    _validate_node_contract,
    prepare_direct_procedure_run,
)
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.proposals.settlement import ChangeActorBinding
from cruxible_core.service.procedures.procedures import (
    PlaybillProcedureStateTapReader,
    service_execute_direct_procedure,
)
from cruxible_core.storage.cas import ContentAddressedBodyStore
from tests.core_support._candidate_support import submit_query_definition_candidate
from tests.core_support._knowledge_loop_support import (
    QUERY_NAME,
    TIMESTAMP,
    accept_proposal,
    seed_claims,
    work_item_query,
)
from tests.core_support._support import client_material
from tests.support.procedures import (
    bind_fixture,
    boundary_contract,
    contract_files,
    contract_pin,
    contract_resolver,
    named,
    procedure_definition,
    procedure_members,
)
from tests.test_ledger.test_activation import _sign

READ_TIME = datetime(2026, 8, 16, 21, 0, tzinfo=UTC)


def test_list_contract_schema_is_recursive_without_changing_scalar_bytes() -> None:
    scalar = PropertySchema(type="string")
    assert "item_fields" not in scalar.model_dump(mode="json")

    schema = PropertySchema(
        type="list",
        item_fields={
            "id": PropertySchema(type="string"),
            "parts": PropertySchema(
                type="list",
                item_fields={"value": PropertySchema(type="int")},
            ),
        },
    )
    assert schema.item_fields is not None
    assert schema.item_fields["parts"].item_fields is not None
    with pytest.raises(ValueError, match="require item_fields"):
        PropertySchema(type="list")
    with pytest.raises(ValueError, match="only allowed"):
        PropertySchema(type="string", item_fields={})
    with pytest.raises(ValueError, match="may not be primary_key"):
        PropertySchema(type="list", item_fields={}, primary_key=True)


#: One work item as a Query row: what a read of the work-item Query yields.
WORK_ROW = boundary_contract("test.work-row", {"item_id": "string", "status": "string?"})
_PART = boundary_contract("test.part", {"value": "int"})
_NESTED_ROW = boundary_contract(
    "test.nested-row", {"parts": "list<test.part@1>{max_items: 10}"}, _PART
)


def _validator(*contracts: Contract) -> AcceptedProcedureContractValidator:
    return AcceptedProcedureContractValidator(contract_resolver(*contracts))


def test_a_boundary_value_is_checked_exactly_and_names_the_nested_field() -> None:
    rows = boundary_contract(
        "rows", {"rows": "list<test.nested-row@1>{max_items: 10}"}, _NESTED_ROW
    )
    validator = _validator(rows, _NESTED_ROW, _PART)
    pin = contract_pin("contract-out", rows)

    assert validator.validate_contract(
        contract=pin, payload={"rows": [{"parts": [{"value": 3}]}]}, direction="output"
    ) == {"rows": [{"parts": [{"value": 3}]}]}
    with pytest.raises(ProcedureContractValidationError) as caught:
        validator.validate_contract(
            contract=pin,
            payload={"rows": [{"parts": [{"value": "not-an-int"}]}]},
            direction="output",
        )
    assert caught.value.field_path == "rows[0].parts[0].value"
    with pytest.raises(ProcedureContractValidationError, match="undeclared"):
        validator.validate_contract(
            contract=pin, payload={"rows": [], "extra": 1}, direction="output"
        )


def test_a_list_at_a_boundary_refuses_past_the_run_item_budget_without_clipping() -> None:
    rows = boundary_contract("rows", {"rows": "list<test.work-row@1>{max_items: 100}"}, WORK_ROW)
    validator = _validator(rows, WORK_ROW)
    payload = {"rows": [{"item_id": "a"}, {"item_id": "b"}]}

    with pytest.raises(ProcedureContractItemBudgetExceeded) as caught:
        validator.validate_contract_with_budget(
            contract=contract_pin("contract-out", rows),
            payload=payload,
            direction="output",
            max_items=1,
        )
    assert caught.value.field_path == "rows"
    assert caught.value.limit == 1
    assert caught.value.observed == 2
    assert payload["rows"] == [{"item_id": "a"}, {"item_id": "b"}]


def test_a_boundary_contract_not_accepted_at_its_pinned_digest_refuses() -> None:
    rows = boundary_contract("rows", {"count": "int"})
    other = boundary_contract("rows", {"count": "string"})
    with pytest.raises(ProcedureContractValidationError, match="not accepted"):
        _validator(other).validate_contract(
            contract=contract_pin("contract-out", rows), payload={"count": 1}, direction="output"
        )


def test_boundary_budget_refusal_and_high_water_tie_are_deterministic() -> None:
    class _BudgetValidator:
        def validate_contract_with_budget(self, **_kwargs):  # type: ignore[no-untyped-def]
            raise ProcedureContractItemBudgetExceeded(
                field_path="rows",
                limit=1,
                observed=2,
            )

    state = _RunState(outputs={}, input_payload={}, parameters={})
    pin = ArtifactPin(
        role="contract-out",
        target=ArtifactIdentity(kind="Contract", name="rows"),
        artifact_digest=typed_digest(
            ArtifactDigest,
            "playbill-list-boundary-test-v1",
            {"contract": "rows"},
        ).tagged,
    )
    with pytest.raises(_RunRefusal) as caught:
        _validate_node_contract(
            _BudgetValidator(),  # type: ignore[arg-type]
            contract=pin,
            payload={"rows": [{"id": "a"}, {"id": "b"}]},
            direction="output",
            node_id="shape",
            max_items=1,
            observe_items=state.observe_items,
        )
    assert caught.value.refusal.code == "budget_max_items_exceeded"
    assert caught.value.refusal.details == {
        "boundary": "contract-out:rows",
        "dimension": "max_items",
        "field_path": "rows",
        "limit": 1,
        "observed": 2,
    }

    state.observe_items(2, "contract-out:z", "rows")
    state.observe_items(2, "contract-out:a", "rows")
    assert state.max_items_high_water == 2
    assert state.max_items_boundary == "contract-out:a"
    assert state.max_items_field_path == "rows"


class _Authority:
    def __init__(self, digest: str) -> None:
        self.digest = digest

    def current_procedure_digest(self, identity, *, coordinate):  # type: ignore[no-untyped-def]
        return self.digest


#: The query Procedure's governed boundaries (accepted beside it by ``_activate_procedure``).
EMPTY_INPUT = boundary_contract("test.empty-input")
QUERY_ROWS = boundary_contract(
    "test.query-rows", {"rows": "list<test.work-row@1>{max_items: 200}"}, WORK_ROW
)
QUERY_CONTRACTS = (EMPTY_INPUT, QUERY_ROWS, WORK_ROW)


def _contract(name: str, fields: dict[str, str]) -> Contract:
    return boundary_contract(name, fields)


def _accepted_query_procedure(query_digest: str) -> AcceptedProcedure:
    contract_in = contract_pin("contract-in", EMPTY_INPUT)
    contract_out = contract_pin("contract-out", QUERY_ROWS)
    query = ArtifactPin(
        role="query",
        target=ArtifactIdentity(kind="QueryDefinition", name=QUERY_NAME),
        artifact_digest=query_digest,
    )
    definition = procedure_definition(
        name="query-work-items",
        contract_in=named(contract_in),
        contract_out=named(contract_out),
        nodes=(
            ReadNode(
                node_id="read",
                query=named(query),
                parameters={},
                as_="query",
                next="project",
            ),
            ProjectNode(
                node_id="project",
                fields={"rows": "$steps.query.rows"},
                as_="result",
            ),
        ),
    )
    procedure = ProcedureArtifact(
        identity=ArtifactIdentity(kind="Procedure", name=definition.name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest(definition).tagged,
        activation_policy="abort",
    )
    return bind_fixture(
        AcceptedProcedure(
            path=procedure_path(definition.name),
            procedure=procedure,
            artifact_digest=procedure_artifact_digest(procedure).tagged,
        ),
        extra_pins=(query, contract_in, contract_out),
    )


def test_query_only_procedure_runs_through_daemon_query_without_provider(
    tmp_path: Path,
) -> None:
    instance, owner = seed_claims(tmp_path)
    query = work_item_query()
    inspection = submit_query_definition_candidate(
        instance,
        query=query,
        actor_id="owner",
        proposal_name="procedure-query",
        timestamp=TIMESTAMP,
    )
    accept_proposal(instance, owner, inspection)
    accepted = _accepted_query_procedure(query_definition_digest(query).tagged)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())

    journal_root = tmp_path / "procedure-journal"
    cas_root = tmp_path / "procedure-cas"
    journal_root.mkdir(mode=0o700)
    cas_root.mkdir(mode=0o700)
    journal = LocalJournalBackend(journal_root)
    stream = JournalStreamIdentityV1(
        instance_id=instance.descriptor.instance_id,
        journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
        stream_id="procedures",
    )
    journal.activate_writer(
        stream,
        "runs",
        fencing_token="writer",
        expected_head=journal.read_head(stream, "runs"),
    )
    prepared = prepare_direct_procedure_run(
        accepted,
        instance_id=instance.descriptor.instance_id,
        accepted_coordinate=coordinate,
        invocation_input={},
        actor_context=GovernedActorContext(
            actor_type="human_user",
            actor_id="owner",
            org_id=instance.descriptor.instance_id,
            operation_id="query-only-procedure",
            timestamp=READ_TIME,
        ),
        state_reader=PlaybillProcedureStateTapReader(
            instance=instance,
            evaluation_time=READ_TIME,
        ),
        bodies=ContentAddressedBodyStore(cas_root),
        journal_stream=stream,
        journal_partition_id="runs",
        admitted_at=READ_TIME,
    )
    result = service_execute_direct_procedure(
        prepared,
        accepted,
        journal=journal,
        bodies=ContentAddressedBodyStore(cas_root),
        run_index_path=tmp_path / "procedure-run-index.sqlite",
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_resolver=contract_resolver(*QUERY_CONTRACTS),
    )

    assert procedure_runnability(accepted.procedure.definition) == ("direct", ())
    assert result.status == "succeeded", result.refusal
    assert result.output is not None
    assert len(result.output["rows"]) == 2  # type: ignore[index]


def _activate_procedure(  # type: ignore[no-untyped-def]
    instance, owner, procedure, *, sequence: int, timestamp: str, extra_contracts=()
):
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    procedure_path(procedure.identity.name)
    tree.update(procedure_members(procedure))
    # Its governed boundary Contracts are accepted beside it (unchanged if present).
    tree.update(contract_files(*QUERY_CONTRACTS, *extra_contracts))
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/procedure-{sequence}",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp=timestamp,
    )
    assert result.candidate is not None, result.evaluation.diagnostics
    assert result.evaluation.evaluated_tree_oid is not None
    bundle = instance.prepare_generation(
        base=base,
        candidate_tree=instance.proposal_tree(result.evaluation.evaluated_tree_oid),
        candidate=result.candidate,
        approvals=(
            _sign(
                client_material(instance.root.parent, instance),
                result.candidate.candidate_digest,
                base.semantic_root,
            ),
        ),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        proposal_actor_id="owner",
        sequence=len(instance.accepted_history()),
    )
    publisher = instance.activation_publisher()
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"
    instance.refresh()
    return result, base


@pytest.mark.parametrize(
    ("payload", "field_path"),
    (
        ({"x": 1}, "x"),  # an unexpected root field names itself, not the empty root
        ({"count": 1, "x": 1}, "x"),
        ({}, "count"),  # a missing required field names itself
    ),
)
def test_a_contract_refusal_names_the_offending_field_even_at_the_root(
    payload: dict[str, object], field_path: str
) -> None:
    contract = boundary_contract("counts", {"count": "int"})
    with pytest.raises(ProcedureContractValidationError) as refused:
        _validator(contract).validate_contract(
            contract=contract_pin("contract-out", contract), payload=payload, direction="output"
        )
    assert refused.value.field_path == field_path


@pytest.mark.parametrize("direction", ["input", "output"])
def test_nested_list_budget_and_observations(direction):
    from cruxible_client.contracts.provider_operations import (
        OperationItemBudgetExceeded,
        validate_operation_value,
    )

    rows = boundary_contract(
        "rows", {"rows": "list<test.nested-row@1>{max_items: 10}"}, _NESTED_ROW
    )
    resolve = contract_resolver(rows, _NESTED_ROW, _PART)
    validator = _validator(rows, _NESTED_ROW, _PART)
    payload = {"rows": [{"parts": [{"value": 1}, {"value": 2}]}]}
    with pytest.raises(ProcedureContractItemBudgetExceeded) as caught:
        validator.validate_contract_with_budget(
            contract=contract_pin(f"contract-{'in' if direction == 'input' else 'out'}", rows),
            payload=payload,
            direction=direction,
            max_items=1,
        )
    assert (caught.value.field_path, caught.value.observed) == ("rows[0].parts", 2)
    observations = []
    with pytest.raises(OperationItemBudgetExceeded) as caught_operation:
        validate_operation_value(
            payload,
            rows,
            resolve=resolve,
            max_items=1,
            observe_items=lambda count, path: observations.append((path, count)),
        )
    assert caught_operation.value.field_path == "rows[0].parts"
    assert observations == [("rows", 1), ("rows[0].parts", 2)]


@pytest.mark.parametrize("direction", ["input", "output"])
@pytest.mark.parametrize("nested", [False, True])
def test_boundaries_require_canonical_number_wire_forms(direction, nested):
    from cruxible_client.contracts.canonical import canonical_bytes
    from cruxible_client.contracts.provider_operations import (
        OperationValueRefused,
        validate_operation_value,
    )
    from cruxible_client.contracts.shapes import canonical_number

    inner = boundary_contract("test.numbers", {"value": "number"})
    contract = (
        boundary_contract("test.number-boundary", {"value": "test.numbers@1"}, inner)
        if nested
        else inner
    )
    resolve = contract_resolver(inner, contract)
    validator = AcceptedProcedureContractValidator(resolve)
    pin = contract_pin("contract-in" if direction == "input" else "contract-out", contract)
    canonical = {"value": {"value": "0.5"}} if nested else {"value": "0.5"}
    bad = {"value": {"value": "0.50"}} if nested else {"value": "0.50"}
    assert (
        validator.validate_contract(contract=pin, payload=canonical, direction=direction)
        == canonical
    )
    checked = validate_operation_value(canonical, contract, resolve=resolve)
    assert canonical_bytes(checked) == canonical_bytes(canonical)
    assert typed_digest(ArtifactDigest, "test-number-wire", checked) == typed_digest(
        ArtifactDigest, "test-number-wire", canonical
    )
    with pytest.raises(ProcedureContractValidationError, match="noncanonical number"):
        validator.validate_contract(contract=pin, payload=bad, direction=direction)
    with pytest.raises(OperationValueRefused, match="noncanonical number"):
        validate_operation_value(bad, contract, resolve=resolve)
    integral = {"value": {"value": "1.0"}} if nested else {"value": "1.0"}
    with pytest.raises(ProcedureContractValidationError, match="noncanonical number"):
        validator.validate_contract(contract=pin, payload=integral, direction=direction)
    assert canonical_number("1.0") == 1


@pytest.mark.parametrize("size", [10, 1000])
def test_run_enum_vocabulary_sql_is_constant_across_boundaries(tmp_path, monkeypatch, size):
    from cruxible_client.contracts.provider_operations import validate_operation_value
    from cruxible_core.claims.enum_resolution import accepted_enum_resolver, tree_enum_resolver
    from cruxible_core.indexes.typed_state import TypedStateReader

    instance, _ = seed_claims(tmp_path)
    original = TypedStateReader.source
    sources, statements = [], []

    def counted(reader, identity):
        sources.append(identity)
        reader.connection.set_trace_callback(statements.append)
        try:
            return original(reader, identity)
        finally:
            reader.connection.set_trace_callback(None)

    monkeypatch.setattr(TypedStateReader, "source", counted)
    resolver = accepted_enum_resolver(instance, instance.accepted_coordinate())
    row = boundary_contract("test.enum-row", {"status": "enum_of:project.work_item.status"})
    rows = boundary_contract(
        "test.enum-rows", {"rows": "list<test.enum-row@1>{max_items: 2000}"}, row
    )
    contracts = contract_resolver(row, rows)
    validator = AcceptedProcedureContractValidator(contracts, resolver)
    payload = {"rows": [{"status": "ready"} for _ in range(size)]}
    for direction in ("input", "output"):
        validator.validate_contract(
            contract=contract_pin("contract-in" if direction == "input" else "contract-out", rows),
            payload=payload,
            direction=direction,
        )
        validate_operation_value(payload, rows, resolve=contracts, enum_members=resolver)
    assert sources == ["ClaimType:project.work_item.status"]
    envelope_sql = [
        statement
        for statement in statements
        if " WHERE identity=" in statement and "ClaimType:project.work_item.status" in statement
    ]
    assert len(envelope_sql) == 1
    # Missing vocabulary is cached too; a fresh coordinate resolver reads afresh.
    assert resolver("test.missing") is None and resolver("test.missing") is None
    assert sources.count("ClaimType:test.missing") == 1
    fresh = accepted_enum_resolver(instance, instance.accepted_coordinate())
    assert fresh("project.work_item.status") == ("blocked", "done", "ready")
    assert sources.count("ClaimType:project.work_item.status") == 2
    candidate = tree_enum_resolver(
        instance.immutable_tree_at(instance.accepted_coordinate().git_oid)
    )
    assert candidate("project.work_item.status") == fresh("project.work_item.status")
    assert candidate("test.missing") is None


@pytest.mark.parametrize("direction", ["in", "out"])
@pytest.mark.parametrize("violation", ["budget", "number"])
def test_operation_nested_boundaries_preserve_budget_and_number_laws(
    tmp_path, direction, violation
):
    from types import SimpleNamespace

    from cruxible_client.contracts.shapes import ContractRef
    from cruxible_core.procedures.execution import ProcedureExecutor
    from tests.test_procedures.test_procedure_execution import (
        _Authority,
        _Contracts,
        _fixture,
        _prepare,
        _state_procedure,
        _StateReader,
    )

    fixture = _fixture(tmp_path)
    accepted = _state_procedure()
    prepared = _prepare(accepted, fixture, _StateReader())
    admission = prepared.admission.model_copy(
        update={"budget": prepared.admission.budget.model_copy(update={"max_items": 1})}
    )
    row = boundary_contract("test.number-parts", {"parts": "list<number>{max_items: 10}"})
    contract = boundary_contract(
        "test.operation-rows", {"rows": "list<test.number-parts@1>{max_items: 10}"}, row
    )
    occurrence = SimpleNamespace(
        contracts=(row, contract),
        operation=SimpleNamespace(
            in_=ContractRef(name=contract.name, version=1),
            out=ContractRef(name=contract.name, version=1),
            name="compute",
        ),
    )
    executor = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    )
    state = _RunState(outputs={}, input_payload={}, parameters={})
    payload = {"rows": [{"parts": [1, 2] if violation == "budget" else ["1.0"]}]}
    with pytest.raises(_RunRefusal) as caught:
        executor._validate_operation_value(
            occurrence,
            payload,
            direction=direction,
            admission=admission,
            state=state,
            node_id="call",
        )
    if violation == "budget":
        assert caught.value.refusal.code == "budget_max_items_exceeded"
        assert caught.value.refusal.details["field_path"] == "rows[0].parts"
        assert state.max_items_high_water == 2
        assert state.max_items_boundary == f"operation-{direction}:compute"
    else:
        assert (
            caught.value.refusal.code
            == f"contract_{'input' if direction == 'in' else 'output'}_refused"
        )
        assert "noncanonical number" in caught.value.refusal.message


def test_number_problems_accumulate_with_other_problems_in_one_list_walk(monkeypatch):
    from cruxible_client.contracts import provider_operations as operations

    contract = boundary_contract(
        "test.multiple-problems", {"required": "string", "numbers": "list<number>{max_items: 10}"}
    )
    visited, observations = [], []
    walk = operations._typed_members

    def counted(value, field_type, path, resolve):
        visited.append(path)
        yield from walk(value, field_type, path, resolve)

    monkeypatch.setattr(operations, "_typed_members", counted)
    with pytest.raises(operations.OperationValueRefused) as caught:
        operations.validate_operation_value(
            {"numbers": ["1.0", "2e0"]},
            contract,
            resolve=contract_resolver(contract),
            observe_items=lambda count, path: observations.append((path, count)),
        )
    assert caught.value.problems == (
        "required: absent_required",
        "numbers[0]: noncanonical number; use 1",
        "numbers[1]: noncanonical number; use 2",
    )
    assert visited == ["numbers", "numbers[0]", "numbers[1]"]
    assert observations == [("numbers", 2)]
