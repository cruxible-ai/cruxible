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
from cruxible_client.contracts.shapes import (
    contract_digest,
    contract_path,
    parse_contract,
)
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.lowering import AuthoringLoweringError
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
    assert registered_path_kind("contracts/security.epss-row/v1.json") == "contract"


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


def test_a_zero_field_contract_is_accepted_and_nests(tmp_path: Path) -> None:
    """``fields: {}`` is the closed empty record; a nested pin to it is digested."""

    instance, owner = initialize_local(tmp_path)
    _accept(
        instance,
        owner,
        ChangeSetInput(
            kind="change_set",
            members=(
                ContractInput(kind="contract", name="security.empty", version=1, fields={}),
                ContractInput(
                    kind="contract",
                    name="security.selected",
                    version=1,
                    fields={"selector": "security.empty@1"},  # type: ignore[dict-item]
                ),
            ),
        ),
    )
    tree = _accepted_tree(instance)
    empty_path = contract_path("security.empty", 1)
    empty = parse_contract(tree[empty_path], path=empty_path)
    assert empty.fields == {}
    selected_path = contract_path("security.selected", 1)
    (ref,) = parse_contract(tree[selected_path], path=selected_path).refs()
    assert ref.digest == contract_digest(empty).tagged


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


def test_intra_change_set_contract_pins_resolve_in_dependency_order(tmp_path: Path) -> None:
    """``security.a@1`` pins ``security.z@1`` in the same set, even listing ``z`` first.

    Members are canonically byte-sorted by semantic identity before staging,
    so ``security.a@1`` stages before ``security.z@1`` by identity order --
    the wrong order for ``a``'s pin on ``z``. Staging must still resolve the
    dependency, not refuse ``pin_unresolved``.
    """

    instance, owner = initialize_local(tmp_path)
    z = ContractInput(kind="contract", name="security.z", version=1, fields={"x": "string"})
    a = ContractInput(
        kind="contract",
        name="security.a",
        version=1,
        fields={"z": "security.z@1"},  # type: ignore[dict-item]
    )
    change_set = ChangeSetInput(kind="change_set", members=(z, a))
    _accept(instance, owner, change_set)
    tree = _accepted_tree(instance)
    z_path = contract_path("security.z", 1)
    a_path = contract_path("security.a", 1)
    z_contract = parse_contract(tree[z_path], path=z_path)
    a_contract = parse_contract(tree[a_path], path=a_path)
    (ref,) = a_contract.refs()
    assert ref.digest == contract_digest(z_contract).tagged


def test_a_contract_dependency_cycle_in_one_change_set_is_refused(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    left = ContractInput(
        kind="contract",
        name="security.left",
        version=1,
        fields={"right": "security.right@1"},  # type: ignore[dict-item]
    )
    right = ContractInput(
        kind="contract",
        name="security.right",
        version=1,
        fields={"left": "security.left@1"},  # type: ignore[dict-item]
    )
    change_set = ChangeSetInput(kind="change_set", members=(left, right))
    assert "cruxible.authoring.contract_dependency_cycle" in _refusal(instance, change_set)


def test_a_version_chain_stages_in_numeric_not_lexical_order(tmp_path: Path) -> None:
    """Versions 1..10 of one Contract, submitted together: v10 must stage after v9.

    Canonical member ordering byte-sorts by identity string, and
    "Contract:security.chain/v10" sorts before ".../v2" lexically. Without
    an implicit predecessor edge, v10 would stage before v2..v9 exist,
    refusing predecessor_missing even though the whole chain is valid.
    """

    instance, owner = initialize_local(tmp_path)
    members = tuple(
        ContractInput(
            kind="contract", name="security.chain", version=version, fields={"x": "string"}
        )
        for version in range(1, 11)
    )
    change_set = ChangeSetInput(kind="change_set", members=members)
    _accept(instance, owner, change_set)
    tree = _accepted_tree(instance)
    previous_digest: str | None = None
    for version in range(1, 11):
        path = contract_path("security.chain", version)
        contract = parse_contract(tree[path], path=path)
        assert contract.predecessor_digest == previous_digest
        previous_digest = contract_digest(contract).tagged


def _row_contract(name: str, **fields: str) -> ContractInput:
    return ContractInput(kind="contract", name=name, version=1, fields=fields)  # type: ignore[arg-type]


def _status_query(row: str) -> QueryDefinitionInput:
    example = query_claims_by_type_example()
    data = example.query_definition.model_dump(mode="json")
    return QueryDefinitionInput.model_validate(
        {"kind": "query_definition", "query_definition": {**data, "row_contract": row}}
    )


def test_a_query_definitions_contract_pins_are_dependencies() -> None:
    """dependency_pins recognizes row_contract/parameter_contract, not only pins{}.

    Dependency plumbing stays built per the coordinator's decision (B3 will
    enable runtime Contract validation); this is pure structural coverage,
    independent of acceptance.
    """

    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.dependencies import dependency_pins
    from cruxible_client.contracts.query.definitions import QueryDefinitionSpec

    data = query_claims_by_type_example().query_definition.model_dump(mode="json")
    spec = QueryDefinitionSpec.model_validate({**data, "row_contract": "project.status-row@1"})
    targets = {pin.target for pin in dependency_pins(spec)}
    assert ArtifactIdentity(kind="Contract", name="project.status-row/v1") in targets


# -- Query Contracts are not supported in B1 (coordinator decision) ---------------------
#
# Acceptance-time proof of a Query's parameter/row Contract (agreement,
# refinement, presence) repeatedly leaked: nested refinements, Claim-sourced
# kinds/cardinality and parameter-vs-Contract bare-type gaps each let a
# signed, accepted, executed Query violate its own declared Contract. Rather
# than keep chasing a sound acceptance-time proof, a QueryDefinition may not
# declare parameter_contract or row_contract at all in B1: the model fields,
# lowering's exact-digest pin and the dependency_pins edge above all stay
# built (tested), but evaluate_query_definition_law refuses either field
# outright, before ever resolving or judging it. B3 owns runtime validation
# of supplied parameters and emitted rows.


def test_a_raw_candidate_declaring_a_row_contract_is_refused() -> None:
    """The law refuses row_contract outright, before resolving or judging it."""

    from cruxible_client.contracts.query.definitions import (
        NamedQueryDefinition,
        evaluate_query_definition_law,
        query_definition_path,
    )

    data = query_claims_by_type_example().query_definition.model_dump(
        mode="json", exclude={"pins", "artifact_format"}
    )
    data["artifact_format"] = "cruxible-query-definition-v3"
    # An unresolvable Contract name: the refusal must fire before any
    # resolution is even attempted.
    data["row_contract"] = "project.unresolvable-row@1"
    query = NamedQueryDefinition.model_validate(data)
    result = evaluate_query_definition_law(
        query, path=query_definition_path(query.identity.name), predecessor=None
    )
    assert result.verdict == "refused"
    assert any(
        diagnostic.code == "cruxible.query_definition.contract_unsupported"
        for diagnostic in result.diagnostics
    )


def test_a_raw_candidate_declaring_a_parameter_contract_is_refused() -> None:
    from cruxible_client.contracts.query.definitions import (
        NamedQueryDefinition,
        evaluate_query_definition_law,
        query_definition_path,
    )

    data = query_claims_by_type_example().query_definition.model_dump(
        mode="json", exclude={"pins", "artifact_format"}
    )
    data["artifact_format"] = "cruxible-query-definition-v3"
    data["parameters"] = [{"name": "status", "value_type": "string"}]
    data["parameter_contract"] = "project.unresolvable-params@1"
    query = NamedQueryDefinition.model_validate(data)
    result = evaluate_query_definition_law(
        query, path=query_definition_path(query.identity.name), predecessor=None
    )
    assert result.verdict == "refused"
    assert any(
        diagnostic.code == "cruxible.query_definition.contract_unsupported"
        for diagnostic in result.diagnostics
    )


def test_authoring_a_query_with_a_row_contract_is_refused(tmp_path: Path) -> None:
    """Ordinary authoring (signed preflight) refuses a declared row_contract.

    The Contract itself is accepted first, so lowering succeeds in pinning
    the digest (that plumbing stays built); the law refuses the Query
    regardless of a perfectly resolvable row_contract.
    """

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    _accept(
        instance, owner, _row_contract("project.status-row", item_id="string", status="string?")
    )
    assert "cruxible.query_definition.contract_unsupported" in _refusal(
        instance, _status_query("project.status-row@1")
    )


def test_authoring_a_query_with_a_parameter_contract_is_refused(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    _accept(instance, owner, _row_contract("project.params", status="string"))
    example = query_claims_by_type_example()
    data = example.query_definition.model_dump(mode="json")
    query = QueryDefinitionInput.model_validate(
        {
            "kind": "query_definition",
            "query_definition": {
                **data,
                "parameters": [{"name": "status", "value_type": "string"}],
                "parameter_contract": "project.params@1",
            },
        }
    )
    assert "cruxible.query_definition.contract_unsupported" in _refusal(instance, query)


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
    from cruxible_client.contracts.shapes import contract_path_parts
    from cruxible_core.ledger.bootstrap import GENESIS_SEEDS

    # The rest of the section: the last authored version, then the Contracts
    # every new instance is seeded with at genesis (workspace.file's signature).
    seeded = sorted(
        f"{parts[0]}@{parts[1]}"
        for path, _digest in GENESIS_SEEDS
        if (parts := contract_path_parts(path)) is not None
    )
    assert seeded
    assert [row.contract for row in rest.contracts or ()] == ["security.epss-row@2", *seeded]
