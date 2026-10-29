"""A Contract is authored, accepted, versioned and refused through ordinary authoring."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.authoring.inputs import (
    AuthoringInputError,
    ChangeSetInput,
    ContractInput,
    lower_authoring_input,
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
from tests.test_authoring.test_authoring_preflight import TIMESTAMP
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


def _refusal(instance: PlaybillInstance, value: ContractInput | ChangeSetInput) -> str:
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
    instance: PlaybillInstance, owner: object, value: ContractInput | ChangeSetInput
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
