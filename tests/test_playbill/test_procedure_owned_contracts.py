"""Owner-carried Procedure contracts and daemon-query execution boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import ArtifactDigest, canonical_bytes, typed_digest
from cruxible_client.contracts.captures import CanonicalDurationV1
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedureV1,
    ProcedureArtifactV1,
    ProcedureArtifactV2,
    ProcedureOwnedContractV1,
    evaluate_procedure_law,
    parse_procedure,
    procedure_artifact_digest,
    procedure_owned_contract_digest,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_client.contracts.procedures.contracts import (
    ProcedureContractValidationError,
    _validate_payload,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest_v3
from cruxible_client.contracts.procedures.models import (
    ProcedureBudgetV3,
    ProcedureDefinitionV3,
    ProcedureHardCapsV3,
    ProjectNodeV3,
    StateTapNodeV3,
)
from cruxible_client.contracts.query.definitions import query_definition_digest
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.cas import ContentAddressedBodyStore
from cruxible_core.playbill.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    LocalJournalBackend,
)
from cruxible_core.playbill.procedures.execution import (
    prepare_direct_procedure_run,
)
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.service.query_definitions import (
    service_propose_playbill_query_definition,
)
from cruxible_core.playbill.settlement import ChangeActorBinding
from cruxible_core.service.playbill_procedures import (
    PlaybillProcedureStateTapReader,
    service_execute_direct_procedure,
)
from tests.test_playbill._knowledge_loop_support import (
    QUERY_NAME,
    TIMESTAMP,
    accept_proposal,
    seed_claims,
    work_item_query,
)
from tests.test_playbill._support import client_material, initialize_local
from tests.test_playbill.test_activation import _sign

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


def test_list_contract_validation_names_nested_element_and_path() -> None:
    contract = _contract(
        "rows",
        {
            "rows": PropertySchema(
                type="list",
                item_fields={
                    "parts": PropertySchema(
                        type="list",
                        item_fields={"value": PropertySchema(type="int")},
                    )
                },
            )
        },
    )

    assert _validate_payload(
        contract,
        {"rows": [{"parts": [{"value": 3}]}]},
    ) == {"rows": [{"parts": [{"value": 3}]}]}
    with pytest.raises(ProcedureContractValidationError) as caught:
        _validate_payload(
            contract,
            {"rows": [{"parts": [{"value": "not-an-int"}]}]},
        )
    assert caught.value.field_path == "rows[0].parts[0].value"
    assert caught.value.element_index == 0


class _Authority:
    def __init__(self, digest: str) -> None:
        self.digest = digest

    def current_procedure_digest(self, identity, *, coordinate):  # type: ignore[no-untyped-def]
        return self.digest


def _contract(name: str, fields: dict[str, PropertySchema]) -> ProcedureOwnedContractV1:
    return ProcedureOwnedContractV1(
        identity=ArtifactIdentity(kind="Contract", name=name),
        schema=ContractSchema(fields=fields),
    )


def _accepted_query_procedure(query_digest: str) -> AcceptedProcedureV1:
    input_contract = _contract("empty-input", {})
    output_contract = _contract("query-rows", {"rows": PropertySchema(type="json")})
    contract_in = ArtifactPin(
        role="contract-in",
        target=input_contract.identity,
        artifact_digest=procedure_owned_contract_digest(input_contract).tagged,
    )
    contract_out = ArtifactPin(
        role="contract-out",
        target=output_contract.identity,
        artifact_digest=procedure_owned_contract_digest(output_contract).tagged,
    )
    query = ArtifactPin(
        role="query",
        target=ArtifactIdentity(kind="QueryDefinition", name=QUERY_NAME),
        artifact_digest=query_digest,
    )
    definition = ProcedureDefinitionV3(
        name="query-work-items",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            StateTapNodeV3(
                node_id="read",
                query=query,
                parameters={},
                as_="query",
                next="project",
            ),
            ProjectNodeV3(
                node_id="project",
                fields={"rows": "$steps.query.rows"},
                contract_out=contract_out,
                as_="result",
            ),
        ),
        returns="result",
        budget=ProcedureBudgetV3(
            wall_clock=CanonicalDurationV1(microseconds=2_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=100,
        ),
        hard_caps=ProcedureHardCapsV3(
            max_wall_clock=CanonicalDurationV1(microseconds=4_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=200,
            max_repeat_attempts=1,
        ),
        terminal_capability=1,
    )
    procedure = ProcedureArtifactV2(
        identity=ArtifactIdentity(kind="Procedure", name=definition.name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest_v3(definition).tagged,
        pins=tuple(
            sorted(
                (contract_in, contract_out, query),
                key=lambda pin: (
                    pin.role.encode(),
                    pin.target.qualified.encode(),
                    pin.artifact_digest.encode(),
                ),
            )
        ),
        owned_contracts=tuple(
            sorted(
                (input_contract, output_contract),
                key=lambda contract: canonical_bytes(
                    contract.model_dump(mode="json", by_alias=True)
                ),
            )
        ),
        activation_policy="abort",
    )
    return AcceptedProcedureV1(
        path=procedure_path(definition.name),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )


def test_query_only_procedure_runs_through_daemon_query_without_provider(
    tmp_path: Path,
) -> None:
    instance, owner = seed_claims(tmp_path)
    query = work_item_query()
    inspection = service_propose_playbill_query_definition(
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
        run_id="query-run",
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
        provider_executor=None,
    )

    assert accepted.procedure.directly_runnable is True
    assert result.status == "succeeded"
    assert result.output is not None
    assert len(result.output["rows"]) == 2  # type: ignore[index]


def _activate_procedure(instance, owner, procedure, *, sequence: int, timestamp: str):  # type: ignore[no-untyped-def]
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    path = procedure_path(procedure.identity.name)
    tree[path] = render_procedure(procedure)
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/procedure-{sequence}",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp=timestamp,
    )
    assert result.candidate is not None
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


def test_mixed_procedure_v1_v2_ledger_replays_each_historical_wire(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    placeholder_query_digest = typed_digest(
        ArtifactDigest,
        "playbill-procedure-succession-test-v1",
        {"query": "placeholder"},
    ).tagged
    v2 = _accepted_query_procedure(placeholder_query_digest).procedure
    assert isinstance(v2, ProcedureArtifactV2)
    v1 = ProcedureArtifactV1(
        identity=v2.identity,
        definition=v2.definition,
        definition_digest=v2.definition_digest,
        pins=v2.pins,
        activation_policy=v2.activation_policy,
    )
    first, genesis = _activate_procedure(
        instance,
        owner,
        v1,
        sequence=1,
        timestamp="2026-08-21T12:00:00.000000Z",
    )
    v1_coordinate = instance.accepted_coordinate()
    successor = v2.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(predecessor_digest=procedure_artifact_digest(v1).tagged)
        }
    )
    second, _base = _activate_procedure(
        instance,
        owner,
        successor,
        sequence=2,
        timestamp="2026-08-21T13:00:00.000000Z",
    )

    path = procedure_path(v1.identity.name)
    assert isinstance(
        parse_procedure(instance.tree_at(v1_coordinate.git_oid)[path], path=path),
        ProcedureArtifactV1,
    )
    assert isinstance(
        parse_procedure(instance.tree_at(instance.accepted_coordinate().git_oid)[path], path=path),
        ProcedureArtifactV2,
    )
    assert first.candidate is not None
    assert second.candidate is not None
    assert first.candidate.law_evidence[0].law_identifier == "playbill.procedure.v1"
    assert second.candidate.law_evidence[0].law_identifier == "playbill.procedure.v2"
    assert genesis.git_oid != v1_coordinate.git_oid


def test_procedure_v2_lineage_cannot_drop_its_owned_contract_closure() -> None:
    v2 = _accepted_query_procedure(
        typed_digest(
            ArtifactDigest,
            "playbill-procedure-succession-test-v1",
            {"query": "placeholder"},
        ).tagged
    )
    legacy_successor = ProcedureArtifactV1(
        identity=v2.procedure.identity,
        definition=v2.procedure.definition,
        definition_digest=v2.procedure.definition_digest,
        pins=v2.procedure.pins,
        activation_policy=v2.procedure.activation_policy,
        lifecycle=ArtifactLifecycle(predecessor_digest=v2.artifact_digest),
    )

    result = evaluate_procedure_law(
        legacy_successor,
        path=v2.path,
        predecessor=v2,
    )
    assert result.verdict == "refused"
    assert result.diagnostics[0].code == "playbill.procedure.wire_downgrade"
