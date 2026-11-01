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
    ProcedureDefinition,
    ProcedureHardCaps,
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
    MAX_WALL_CLOCK,
    bind_fixture,
    boundary_contract,
    contract_files,
    contract_pin,
    contract_resolver,
    named,
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
    definition = ProcedureDefinition(
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
        returns="result",
        hard_caps=ProcedureHardCaps(
            max_wall_clock=MAX_WALL_CLOCK,
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=200,
            max_repeat_attempts=1,
        ),
        terminal_capability=1,
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
