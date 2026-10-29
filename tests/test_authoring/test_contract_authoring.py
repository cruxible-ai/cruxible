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


def test_a_query_whose_projection_produces_its_row_contract_is_accepted(tmp_path: Path) -> None:
    """``status`` must be optional: a Claim value is never guaranteed present (F3b)."""

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    _accept(
        instance, owner, _row_contract("project.status-row", item_id="string", status="string?")
    )
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
    # A plain string field: this test is about name/type agreement, not the
    # separate structural-refinement rule (an enum field is a refinement and
    # would be refused outright regardless of agreement; see F3a).
    params = validate_contract(
        {"name": "project.params", "version": 1, "fields": {"status": "string"}}
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


def test_a_null_literal_never_satisfies_a_row_contract_field() -> None:
    """``T?`` means the key is absent; a projected literal null is never a match."""

    from cruxible_client.contracts.query.definitions import (
        QueryDefinitionSpec,
        query_contract_problems,
    )
    from cruxible_client.contracts.shapes import validate_contract

    data = query_claims_by_type_example().query_definition.model_dump(mode="json")
    row = validate_contract(
        {
            "name": "project.status-row",
            "version": 1,
            "fields": {"item_id": "string", "status": "string?"},
        }
    )
    spec = QueryDefinitionSpec.model_validate(
        {
            **data,
            "row_contract": "project.status-row@1",
            "projection": {
                "fields": [
                    {
                        "name": "item_id",
                        "value": {
                            "kind": "subject_field",
                            "binding": "item",
                            "field": "subject_id",
                        },
                    },
                    {"name": "status", "value": {"kind": "literal", "value": None}},
                ]
            },
        }
    )
    problems = tuple(
        code for code, _message in query_contract_problems(spec, resolve=lambda _ref: row)
    )
    assert "cruxible.query_definition.row_disagrees" in problems


def test_an_unbounded_parameter_cannot_satisfy_a_refined_row_field() -> None:
    """A bare ``integer`` parameter declaration carries no bound to verify against."""

    from cruxible_client.contracts.query.definitions import (
        QueryDefinitionSpec,
        query_contract_problems,
    )
    from cruxible_client.contracts.shapes import validate_contract

    data = query_claims_by_type_example().query_definition.model_dump(mode="json")
    row = validate_contract(
        {
            "name": "project.bounded-row",
            "version": 1,
            "fields": {"item_id": "string", "rank": "int{0..1}"},
        }
    )
    spec = QueryDefinitionSpec.model_validate(
        {
            **data,
            "parameters": [{"name": "rank", "value_type": "integer"}],
            "row_contract": "project.bounded-row@1",
            "projection": {
                "fields": [
                    {
                        "name": "item_id",
                        "value": {
                            "kind": "subject_field",
                            "binding": "item",
                            "field": "subject_id",
                        },
                    },
                    {"name": "rank", "value": {"kind": "parameter", "parameter": "rank"}},
                ]
            },
        }
    )
    problems = tuple(
        code for code, _message in query_contract_problems(spec, resolve=lambda _ref: row)
    )
    assert "cruxible.query_definition.row_disagrees" in problems


def test_a_matching_parameter_contract_still_cannot_satisfy_a_refined_row_field() -> None:
    """A parameter_contract's refinement is a paper guarantee: execution never enforces it.

    resolve_query_parameters only checks a supplied value's bare value_type,
    never a Contract refinement, so trusting the parameter_contract field's
    agreement with the row field (even when both declare int{0..1}) would
    let an out-of-bound caller value (99) through unchecked at run time.
    """

    from cruxible_client.contracts.query.definitions import (
        QueryDefinitionSpec,
        query_contract_problems,
    )
    from cruxible_client.contracts.shapes import validate_contract

    data = query_claims_by_type_example().query_definition.model_dump(mode="json")
    row = validate_contract(
        {
            "name": "project.bounded-row2",
            "version": 1,
            "fields": {"item_id": "string", "rank": "int{0..1}"},
        }
    )
    params = validate_contract(
        {"name": "project.bounded-params", "version": 1, "fields": {"rank": "int{0..1}"}}
    )

    def resolve(ref: object) -> object:
        return row if ref.name == "project.bounded-row2" else params  # type: ignore[attr-defined]

    spec = QueryDefinitionSpec.model_validate(
        {
            **data,
            "parameters": [{"name": "rank", "value_type": "integer"}],
            "parameter_contract": "project.bounded-params@1",
            "row_contract": "project.bounded-row2@1",
            "projection": {
                "fields": [
                    {
                        "name": "item_id",
                        "value": {
                            "kind": "subject_field",
                            "binding": "item",
                            "field": "subject_id",
                        },
                    },
                    {"name": "rank", "value": {"kind": "parameter", "parameter": "rank"}},
                ]
            },
        }
    )
    problems = tuple(code for code, _message in query_contract_problems(spec, resolve=resolve))
    assert "cruxible.query_definition.row_disagrees" in problems


def test_an_optional_parameter_cannot_satisfy_a_required_row_field() -> None:
    """An optional parameter may be absent at call time; a required field never is."""

    from cruxible_client.contracts.query.definitions import (
        QueryDefinitionSpec,
        query_contract_problems,
    )
    from cruxible_client.contracts.shapes import validate_contract

    data = query_claims_by_type_example().query_definition.model_dump(mode="json")
    row = validate_contract(
        {
            "name": "project.required-row",
            "version": 1,
            "fields": {"item_id": "string", "status": "string"},
        }
    )
    spec = QueryDefinitionSpec.model_validate(
        {
            **data,
            "parameters": [{"name": "status", "value_type": "string", "required": False}],
            "row_contract": "project.required-row@1",
            "projection": {
                "fields": [
                    {
                        "name": "item_id",
                        "value": {
                            "kind": "subject_field",
                            "binding": "item",
                            "field": "subject_id",
                        },
                    },
                    {"name": "status", "value": {"kind": "parameter", "parameter": "status"}},
                ]
            },
        }
    )
    problems = tuple(
        code for code, _message in query_contract_problems(spec, resolve=lambda _ref: row)
    )
    assert "cruxible.query_definition.row_disagrees" in problems


def test_a_subject_id_cannot_satisfy_a_length_bounded_row_field() -> None:
    """A Subject ID's length is not statically known; a bounded string field cannot verify it."""

    from cruxible_client.contracts.query.definitions import (
        QueryDefinitionSpec,
        query_contract_problems,
    )
    from cruxible_client.contracts.shapes import validate_contract

    data = query_claims_by_type_example().query_definition.model_dump(mode="json")
    row = validate_contract(
        {
            "name": "project.short-id-row",
            "version": 1,
            "fields": {"item_id": "string{max_len: 1}"},
        }
    )
    spec = QueryDefinitionSpec.model_validate(
        {
            **data,
            "row_contract": "project.short-id-row@1",
            "projection": {
                "fields": [
                    {
                        "name": "item_id",
                        "value": {
                            "kind": "subject_field",
                            "binding": "item",
                            "field": "subject_id",
                        },
                    },
                ]
            },
        }
    )
    problems = tuple(
        code for code, _message in query_contract_problems(spec, resolve=lambda _ref: row)
    )
    assert "cruxible.query_definition.row_disagrees" in problems


# -- F3a: enum/enum_of row and parameter fields are refused structurally ----------------


def test_a_parameter_cannot_satisfy_an_enum_row_field() -> None:
    """Row Contracts are structural only in B1: an enum field is itself a refinement.

    ``_has_refinement`` previously recognized only bounded strings/numbers; an
    ``enum[...]`` field restricts membership just as much as a bound does, and
    no dynamic source can prove membership statically, so the whole Contract
    is now refused at acceptance rather than judged per source.
    """

    from cruxible_client.contracts.query.definitions import (
        QueryDefinitionSpec,
        query_contract_problems,
    )
    from cruxible_client.contracts.shapes import validate_contract

    data = query_claims_by_type_example().query_definition.model_dump(mode="json")
    row = validate_contract(
        {"name": "project.enum-row", "version": 1, "fields": {"status": "enum[allowed]"}}
    )
    spec = QueryDefinitionSpec.model_validate(
        {
            **data,
            "parameters": [{"name": "status", "value_type": "string"}],
            "row_contract": "project.enum-row@1",
            "projection": {
                "fields": [
                    {"name": "status", "value": {"kind": "parameter", "parameter": "status"}},
                ]
            },
        }
    )
    problems = tuple(
        code for code, _message in query_contract_problems(spec, resolve=lambda _ref: row)
    )
    assert "cruxible.query_definition.row_disagrees" in problems


def test_a_subject_id_cannot_satisfy_an_enum_row_field() -> None:
    from cruxible_client.contracts.query.definitions import (
        QueryDefinitionSpec,
        query_contract_problems,
    )
    from cruxible_client.contracts.shapes import validate_contract

    data = query_claims_by_type_example().query_definition.model_dump(mode="json")
    row = validate_contract(
        {"name": "project.enum-row2", "version": 1, "fields": {"item_id": "enum[wi-42]"}}
    )
    spec = QueryDefinitionSpec.model_validate(
        {
            **data,
            "row_contract": "project.enum-row2@1",
            "projection": {
                "fields": [
                    {
                        "name": "item_id",
                        "value": {
                            "kind": "subject_field",
                            "binding": "item",
                            "field": "subject_id",
                        },
                    },
                ]
            },
        }
    )
    problems = tuple(
        code for code, _message in query_contract_problems(spec, resolve=lambda _ref: row)
    )
    assert "cruxible.query_definition.row_disagrees" in problems


def test_a_parameter_cannot_satisfy_an_enum_of_row_field() -> None:
    from cruxible_client.contracts.query.definitions import (
        QueryDefinitionSpec,
        query_contract_problems,
    )
    from cruxible_client.contracts.shapes import validate_contract

    data = query_claims_by_type_example().query_definition.model_dump(mode="json")
    row = validate_contract(
        {
            "name": "project.enum-of-row",
            "version": 1,
            "fields": {"status": "enum_of project.work_item.status"},
        }
    )
    spec = QueryDefinitionSpec.model_validate(
        {
            **data,
            "parameters": [{"name": "status", "value_type": "string"}],
            "row_contract": "project.enum-of-row@1",
            "projection": {
                "fields": [
                    {"name": "status", "value": {"kind": "parameter", "parameter": "status"}},
                ]
            },
        }
    )
    problems = tuple(
        code for code, _message in query_contract_problems(spec, resolve=lambda _ref: row)
    )
    assert "cruxible.query_definition.row_disagrees" in problems


def test_a_refined_parameter_contract_field_is_refused_structurally() -> None:
    """Parameter Contracts are structural only too: their refinements are not enforced."""

    from cruxible_client.contracts.query.definitions import (
        QueryDefinitionSpec,
        query_contract_problems,
    )
    from cruxible_client.contracts.shapes import validate_contract

    data = query_claims_by_type_example().query_definition.model_dump(mode="json")
    params = validate_contract(
        {"name": "project.refined-params", "version": 1, "fields": {"rank": "int{0..1}"}}
    )
    spec = QueryDefinitionSpec.model_validate(
        {
            **data,
            "parameters": [{"name": "rank", "value_type": "integer"}],
            "parameter_contract": "project.refined-params@1",
        }
    )
    problems = tuple(
        code for code, _message in query_contract_problems(spec, resolve=lambda _ref: params)
    )
    assert "cruxible.query_definition.parameters_disagree" in problems


# -- F3b: Claim-sourced row fields are never guaranteed present -------------------------


def test_a_claim_field_cannot_satisfy_a_required_row_field() -> None:
    """A Claim role/verdict is never guaranteed present: no Claim need exist for a predicate."""

    from cruxible_client.contracts.query.definitions import (
        QueryDefinitionSpec,
        query_contract_problems,
    )
    from cruxible_client.contracts.shapes import validate_contract

    data = query_claims_by_type_example().query_definition.model_dump(mode="json")
    row = validate_contract(
        {"name": "project.role-row", "version": 1, "fields": {"role": "string"}}
    )
    spec = QueryDefinitionSpec.model_validate(
        {
            **data,
            "row_contract": "project.role-row@1",
            "projection": {
                "fields": [
                    {
                        "name": "role",
                        "value": {
                            "kind": "claim_field",
                            "binding": "item",
                            "predicate": "project.work_item.status",
                            "field": "role",
                        },
                    },
                ]
            },
        }
    )
    problems = tuple(
        code for code, _message in query_contract_problems(spec, resolve=lambda _ref: row)
    )
    assert "cruxible.query_definition.row_disagrees" in problems


def test_a_claim_value_cannot_satisfy_a_required_row_field() -> None:
    """A Claim's value was unchecked entirely (fell through to success); it is never guaranteed
    present either, since no Claim need exist for the predicate."""

    from cruxible_client.contracts.query.definitions import (
        QueryDefinitionSpec,
        query_contract_problems,
    )
    from cruxible_client.contracts.shapes import validate_contract

    data = query_claims_by_type_example().query_definition.model_dump(mode="json")
    row = validate_contract(
        {
            "name": "project.value-row",
            "version": 1,
            "fields": {"item_id": "string", "status": "string"},
        }
    )
    spec = QueryDefinitionSpec.model_validate({**data, "row_contract": "project.value-row@1"})
    problems = tuple(
        code for code, _message in query_contract_problems(spec, resolve=lambda _ref: row)
    )
    assert "cruxible.query_definition.row_disagrees" in problems


def test_a_query_definitions_row_contract_pins_an_exact_digest(tmp_path: Path) -> None:
    """Lowering pins row_contract/parameter_contract the same way a Contract pins a nested one."""

    from cruxible_client.contracts.query.definitions import (
        parse_query_definition,
        query_definition_path,
    )
    from cruxible_client.contracts.shapes import contract_digest, contract_path, parse_contract

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    _accept(
        instance, owner, _row_contract("project.status-row", item_id="string", status="string?")
    )
    _accept(instance, owner, _status_query("project.status-row@1"))
    tree = _accepted_tree(instance)
    row_path = contract_path("project.status-row", 1)
    expected_digest = contract_digest(parse_contract(tree[row_path], path=row_path)).tagged
    path = query_definition_path("project.work_items_by_status")
    stored = parse_query_definition(tree[path], path=path)
    assert stored.row_contract is not None
    assert stored.row_contract.digest == expected_digest


def test_a_query_definitions_contract_pins_are_dependencies() -> None:
    """dependency_pins recognizes row_contract/parameter_contract, not only pins{}."""

    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.dependencies import dependency_pins
    from cruxible_client.contracts.query.definitions import QueryDefinitionSpec

    data = query_claims_by_type_example().query_definition.model_dump(mode="json")
    spec = QueryDefinitionSpec.model_validate({**data, "row_contract": "project.status-row@1"})
    targets = {pin.target for pin in dependency_pins(spec)}
    assert ArtifactIdentity(kind="Contract", name="project.status-row/v1") in targets


def test_acceptance_refuses_a_row_contract_pin_without_its_exact_digest() -> None:
    """The stored-artifact acceptance law requires an exact digest, not an unpinned name@N.

    Ordinary authoring (lowering) always fills the digest before a Query
    reaches a candidate tree, so this exercises the law directly: a raw
    candidate that bypasses lowering must still be refused, not merely
    accepted with an unresolved pin.
    """

    from cruxible_client.contracts.query.definitions import (
        NamedQueryDefinition,
        evaluate_query_definition_law,
        query_definition_path,
    )
    from cruxible_client.contracts.shapes import validate_contract

    row = validate_contract(
        {
            "name": "project.status-row",
            "version": 1,
            "fields": {"item_id": "string", "status": "string"},
        }
    )
    data = query_claims_by_type_example().query_definition.model_dump(
        mode="json", exclude={"pins", "artifact_format"}
    )
    data["artifact_format"] = "cruxible-query-definition-v3"
    data["row_contract"] = {"name": "project.status-row", "version": 1}  # no digest
    query = NamedQueryDefinition.model_validate(data)
    result = evaluate_query_definition_law(
        query,
        path=query_definition_path(query.identity.name),
        predecessor=None,
        contracts=lambda _ref: row,
    )
    assert result.verdict == "refused"
    assert any(
        diagnostic.code == "cruxible.query_definition.contract_unresolved"
        for diagnostic in result.diagnostics
    )


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
