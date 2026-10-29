"""A Contract is authored, accepted, versioned and refused through ordinary authoring."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.authoring.examples import query_claims_by_type_example
from cruxible_client.contracts.authoring.inputs import (
    AuthoringInputError,
    ChangeSetInput,
    ContractInput,
    QueryDefinitionInput,
    lower_authoring_input,
)
from cruxible_client.contracts.query.definitions import (
    parse_query_definition,
    query_definition_path,
)
from cruxible_client.contracts.shapes import (
    contract_digest,
    contract_path,
    parse_contract,
)
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.lowering import AuthoringLoweringError
from cruxible_core.compiler.compiler import artifact_kinds_for_compiler, current_compiler_coordinate
from cruxible_core.compiler.projection_artifacts import registered_path_kind
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from tests.core_support._support import initialize_local
from tests.test_authoring.test_authoring_preflight import TIMESTAMP, _seed_claim_surface
from tests.test_ledger.test_activation import _sign

ACTOR = AuthenticatedActor(actor_id="owner")


def _contract(version: int = 1, **fields: object) -> ContractInput:
    return ContractInput(
        kind="contract",
        name="security.epss-row",
        version=version,
        description="One EPSS score row.",
        fields=fields or {"cve_id": "string", "epss": "decimal{0..1}", "percentile": "decimal?"},  # type: ignore[arg-type]
    )


def _refusal(
    instance: PlaybillInstance, value: ContractInput | ChangeSetInput | QueryDefinitionInput
) -> str:
    """The refusal one authoring earns, from lowering or from its preflight frontier."""

    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    payload = lower_authoring_input(value)
    try:
        compiled = coordinator.compile(actor=ACTOR, payload=payload, canonical_timestamp=TIMESTAMP)
    except AuthoringLoweringError as exc:
        return exc.code
    assert compiled.verdict == "refused", compiled
    return compiled.model_dump_json()


def _accept(
    instance: PlaybillInstance,
    owner: object,
    value: ContractInput | ChangeSetInput | QueryDefinitionInput,
) -> None:
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    submitted = coordinator.compile_and_submit(
        actor=ACTOR, payload=lower_authoring_input(value), canonical_timestamp=TIMESTAMP
    )
    assert submitted.status.proposal_id is not None, submitted.status.model_dump(mode="json")
    assert submitted.status.candidate_digest is not None
    approval = _sign(
        owner, submitted.status.candidate_digest, instance.accepted_coordinate().semantic_root
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=submitted.status.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    activated = service_activate_playbill_proposal(
        instance, proposal_id=submitted.status.proposal_id, activated_by="owner"
    )
    assert activated.status == "accepted"


def _accepted_tree(instance: PlaybillInstance) -> dict[str, bytes]:
    return dict(instance.tree_at(instance.accepted_coordinate().git_oid))


def test_the_contract_path_is_registered() -> None:
    registry = artifact_kinds_for_compiler(current_compiler_coordinate())
    assert (
        registered_path_kind("contracts/security.epss-row/v1.json", artifact_kinds=registry)
        == "contract"
    )


def test_a_contract_and_its_successor_are_accepted(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _accept(instance, owner, _contract())
    tree = _accepted_tree(instance)
    first_path = contract_path("security.epss-row", 1)
    first_bytes = tree[first_path]
    first = parse_contract(first_bytes, path=first_path)
    assert first.predecessor_digest is None
    _accept(
        instance,
        owner,
        _contract(2, cve_id="string", epss="decimal{0..1}", percentile="decimal{0..1}?"),
    )
    tree = _accepted_tree(instance)
    second_path = contract_path("security.epss-row", 2)
    second = parse_contract(tree[second_path], path=second_path)
    assert second.predecessor_digest == contract_digest(first).tagged
    assert tree[first_path] == first_bytes


def test_a_nested_contract_is_pinned_to_the_accepted_version(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _accept(instance, owner, _contract())
    _accept(
        instance,
        owner,
        ContractInput(
            kind="contract",
            name="security.epss-page",
            version=1,
            fields={"rows": "list<security.epss-row@1>{max_items: 1000}"},  # type: ignore[dict-item]
        ),
    )
    tree = _accepted_tree(instance)
    row_path = contract_path("security.epss-row", 1)
    page_path = contract_path("security.epss-page", 1)
    page = parse_contract(tree[page_path], path=page_path)
    (ref,) = page.refs()
    assert ref.digest == contract_digest(parse_contract(tree[row_path], path=row_path)).tagged


@pytest.mark.parametrize("field", ["json", "list<string>"])
def test_a_json_field_or_an_unbounded_list_is_refused(field: str) -> None:
    with pytest.raises(AuthoringInputError) as caught:
        lower_authoring_input(_contract(1, payload=field))
    assert caught.value.code == "cruxible.contract.contract_type_refused"


def test_a_version_without_its_predecessor_is_refused(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    assert "cruxible.contract.predecessor_missing" in _refusal(instance, _contract(2))


def test_an_accepted_version_cannot_change(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _accept(instance, owner, _contract())
    assert "cruxible.contract.immutable" in _refusal(instance, _contract(1, cve_id="string"))


def test_an_unresolved_nested_pin_is_refused(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    page = ContractInput(
        kind="contract",
        name="security.epss-page",
        version=1,
        fields={"row": "security.epss-row@1"},  # type: ignore[dict-item]
    )
    assert "cruxible.contract.pin_unresolved" in _refusal(instance, page)


def _row_contract(name: str, **fields: str) -> ContractInput:
    return ContractInput(kind="contract", name=name, version=1, fields=fields)  # type: ignore[arg-type]


def _status_query(row: str) -> QueryDefinitionInput:
    example = query_claims_by_type_example()
    data = example.query_definition.model_dump(mode="json")
    return QueryDefinitionInput.model_validate(
        {"kind": "query_definition", "query_definition": {**data, "row_contract": row}}
    )


def test_a_query_whose_projection_produces_its_row_contract_is_accepted(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    _accept(instance, owner, _row_contract("project.status-row", item_id="string", status="string"))
    _accept(instance, owner, _status_query("project.status-row@1"))
    tree = _accepted_tree(instance)
    path = query_definition_path("project.work_items_by_status")
    stored = parse_query_definition(tree[path], path=path)
    assert stored.row_contract is not None
    assert stored.row_contract.spelling == "project.status-row@1"


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"item_id": "string"}, "cruxible.query_definition.row_disagrees"),
        ({"item_id": "int", "status": "string"}, "cruxible.query_definition.row_disagrees"),
    ],
)
def test_a_query_whose_row_contract_disagrees_with_its_projection_is_refused(
    tmp_path: Path, fields: dict[str, str], code: str
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    _accept(instance, owner, _row_contract("project.status-row", **fields))
    assert code in _refusal(instance, _status_query("project.status-row@1"))


def test_a_query_naming_an_unaccepted_row_contract_is_refused(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    assert "cruxible.query_definition.contract_unresolved" in _refusal(
        instance, _status_query("project.status-row@1")
    )


def test_query_parameters_must_be_the_fields_of_their_contract() -> None:
    from cruxible_client.contracts.query.definitions import (
        QueryDefinitionSpec,
        query_contract_problems,
    )
    from cruxible_client.contracts.shapes import validate_contract

    data = query_claims_by_type_example().query_definition.model_dump(mode="json")
    params = validate_contract(
        {"name": "project.params", "version": 1, "fields": {"status": "enum[ready, done]"}}
    )

    def query(*declared: dict[str, object]) -> QueryDefinitionSpec:
        return QueryDefinitionSpec.model_validate(
            {**data, "parameters": list(declared), "parameter_contract": "project.params@1"}
        )

    def problems(spec: QueryDefinitionSpec) -> tuple[str, ...]:
        return tuple(
            code for code, _message in query_contract_problems(spec, resolve=lambda _ref: params)
        )

    assert problems(query({"name": "status", "value_type": "string"})) == ()
    assert problems(query({"name": "status", "value_type": "integer"})) == (
        "cruxible.query_definition.parameters_disagree",
    )
    assert problems(query({"name": "other", "value_type": "string"})) == (
        "cruxible.query_definition.parameters_disagree",
    )
    with pytest.raises(ValueError, match="requires declared parameters"):
        query()


def accept_contract_world(tmp_path: Path) -> PlaybillInstance:
    """An instance holding security.epss-row@1 and @2 and security.epss-page@1."""

    instance, owner = initialize_local(tmp_path)
    _accept(instance, owner, _contract())
    _accept(
        instance,
        owner,
        _contract(2, cve_id="string", epss="decimal{0..1}", percentile="decimal{0..1}?"),
    )
    _accept(
        instance,
        owner,
        _row_contract("security.epss-page", rows="list<security.epss-row@2>{max_items: 1000}"),
    )
    return instance


def test_get_reads_a_contract_card_by_version_or_newest(tmp_path: Path) -> None:
    from cruxible_client.contracts.get_reads import GetContractCard, GetRequest
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.storage.cas import BodyAccessContext

    instance = accept_contract_world(tmp_path)
    access = BodyAccessContext(principal_id="reader", can_read_body=False)

    def card(ref: str) -> GetContractCard:
        result = service_playbill_get(instance, request=GetRequest(ref=ref), access=access)
        assert result.kind == "contract"
        assert isinstance(result.card, GetContractCard)
        return result.card

    first = card("Contract:security.epss-row@1")
    assert first.contract == "Contract:security.epss-row@1"
    assert first.predecessor is None
    assert [(item.name, item.type, item.optional) for item in first.fields] == [
        ("cve_id", "string", False),
        ("epss", "decimal{0..1}", False),
        ("percentile", "decimal", True),
    ]
    newest = card("Contract:security.epss-row")
    assert (newest.contract, newest.predecessor) == (
        "Contract:security.epss-row@2",
        "security.epss-row@1",
    )
    assert card("Contract:security.epss-page/v1").pins == ("security.epss-row@2",)
    for detail in ("history", "proof"):
        answered = service_playbill_get(
            instance,
            request=GetRequest(ref="Contract:security.epss-row@1", detail=detail),
            access=access,
        )
        assert (answered.kind, answered.detail) == ("contract", detail)


def test_orient_pages_the_contracts_section(tmp_path: Path) -> None:
    from cruxible_core.service.discovery.orient import service_playbill_orient

    instance = accept_contract_world(tmp_path)
    first = service_playbill_orient(instance, section="contracts", limit=2)
    assert first.contracts is not None
    assert [row.contract for row in first.contracts] == [
        "security.epss-page@1",
        "security.epss-row@1",
    ]
    assert first.contracts[0].pins == ("security.epss-row@2",)
    assert first.truncated and first.next_cursor is not None
    rest = service_playbill_orient(instance, section="contracts", cursor=first.next_cursor)
    assert [row.contract for row in rest.contracts or ()] == ["security.epss-row@2"]
