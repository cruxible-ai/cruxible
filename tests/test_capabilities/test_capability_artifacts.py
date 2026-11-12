"""The Capability artifact: stored, judged by its law, authored, indexed and read.

A capability version is accepted at ``capabilities/<name>/v<N>.json`` when it is
immutable, succeeds its predecessor exactly with the same effect, pins two
accepted Contracts exactly, and stays out of ``workspace.`` unless core's builtin
serves the name. Every law is checked through real acceptance (a raw candidate
tree, so lowering cannot pre-empt it) and through authoring, where the same law
reaches the author as a typed refusal with its repair. The typed index holds one
row per version, maintained from the accepted generation; ``get``, the orient
list page and its count, and ``capability_lookup_at`` read that index.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.authoring.inputs import (
    CapabilityInput,
    ChangeSetInput,
    ContractInput,
    lower_authoring_input,
)
from cruxible_client.contracts.capabilities import (
    Capability,
    CapabilityFormatError,
    CapabilityRef,
    capability_digest,
    capability_path,
    parse_capability,
    render_capability,
)
from cruxible_client.contracts.errors import ProjectionFormatError
from cruxible_client.contracts.proposal_models import ProposalAdmissionRequest
from cruxible_client.contracts.shapes import (
    Contract,
    contract_digest,
    contract_path,
    render_contract,
)
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.capabilities.lookup import capabilities_by_name, capability_lookup_at
from cruxible_core.compiler.projection_artifacts import registered_path_kind
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._identity_support import accept_tree
from tests.core_support._support import initialize_local
from tests.test_authoring.test_authoring_preflight import TIMESTAMP
from tests.test_ledger.test_activation import _sign

ACTOR = AuthenticatedActor(actor_id="owner")
_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=False)
NAME = "acme.match"


# -- authoring helpers -------------------------------------------------------------------


def _contracts() -> tuple[ContractInput, ContractInput]:
    return (
        ContractInput(
            kind="contract",
            name="acme.match.request",
            version=1,
            fields={"components": "list<string>{max_items: 100}"},  # type: ignore[dict-item]
        ),
        ContractInput(
            kind="contract",
            name="acme.match.result",
            version=1,
            fields={"document": "capture<*>"},  # type: ignore[dict-item]
        ),
    )


def _capability(
    version: int = 1,
    *,
    name: str = NAME,
    effect: str = "pure",
    description: str = "From the components asked about, one document of matches.",
    request: str = "acme.match.request@1",
) -> CapabilityInput:
    return CapabilityInput.model_validate(
        {
            "kind": "capability",
            "name": name,
            "version": version,
            "description": description,
            "effect": effect,
            "in": request,
            "out": "acme.match.result@1",
        }
    )


def _accept(instance: PlaybillInstance, owner: object, value: object) -> None:
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    submitted = coordinator.compile_and_submit(
        actor=ACTOR,
        payload=lower_authoring_input(value),  # type: ignore[arg-type]
        canonical_timestamp=TIMESTAMP,
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


def _authoring_refusal(instance: PlaybillInstance, value: object) -> tuple[str, str]:
    """The first diagnostic one authoring earns at preflight: its code and its repair."""

    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    compiled = coordinator.compile(
        actor=ACTOR,
        payload=lower_authoring_input(value),  # type: ignore[arg-type]
        canonical_timestamp=TIMESTAMP,
    )
    assert compiled.verdict == "refused", compiled
    diagnostic = compiled.frontier.diagnostics[0]
    assert diagnostic.repairs, diagnostic
    return diagnostic.code, diagnostic.repairs[0].description


def _tree(instance: PlaybillInstance) -> dict[str, bytes]:
    return dict(instance.tree_at(instance.accepted_coordinate().git_oid))


def _world(tmp_path: Path) -> tuple[PlaybillInstance, object]:
    """An instance holding the two Contracts and acme.match@1, all authored in one set."""

    instance, owner = initialize_local(tmp_path)
    _accept(
        instance,
        owner,
        ChangeSetInput(kind="change_set", members=(_capability(), *_contracts())),
    )
    return instance, owner


# -- raw acceptance helpers --------------------------------------------------------------


def _contract_bytes(tree: dict[str, bytes], name: str) -> tuple[Contract, str]:
    from cruxible_client.contracts.shapes import parse_contract

    path = contract_path(name, 1)
    contract = parse_contract(tree[path], path=path)
    return contract, contract_digest(contract).tagged


def _raw(tree: dict[str, bytes], version: int = 1, **changes: object) -> Capability:
    """A pinned capability over the accepted Contracts, for a raw candidate tree."""

    _request, request_digest = _contract_bytes(tree, "acme.match.request")
    _result, result_digest = _contract_bytes(tree, "acme.match.result")
    data: dict[str, object] = {
        "name": NAME,
        "version": version,
        "description": "From the components asked about, one document of matches.",
        "effect": "pure",
        "in": {"name": "acme.match.request", "version": 1, "digest": request_digest},
        "out": {"name": "acme.match.result", "version": 1, "digest": result_digest},
    }
    if version > 1:
        previous = capability_path(NAME, version - 1)
        data["predecessor_digest"] = capability_digest(
            parse_capability(tree[previous], path=previous)
        ).tagged
    data.update(changes)
    return Capability.model_validate(data)


def _files(*capabilities: Capability) -> dict[str, bytes]:
    return {capability_path(c.name, c.version): render_capability(c) for c in capabilities}


def _refused_codes(instance: PlaybillInstance, updates: dict[str, bytes]) -> set[str]:
    base = instance.accepted_coordinate()
    tree = {**instance.tree_at(base.git_oid), **updates}
    proposed = instance.proposal_service().submit(
        actor=ACTOR,
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/raw-capability", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp="2026-10-09T11:59:00.000000Z",
    )
    assert proposed.candidate is None, "the raw candidate was expected to refuse"
    return {item.code for item in proposed.evaluation.diagnostics}


# -- the stored kind ---------------------------------------------------------------------


def test_the_capability_path_is_registered() -> None:
    assert registered_path_kind("capabilities/acme.match/v1.json") == "capability"
    with pytest.raises(ProjectionFormatError):
        registered_path_kind("capabilities/acme.match/v0.json")


def test_a_capability_and_its_successor_are_authored_and_accepted(tmp_path: Path) -> None:
    instance, owner = _world(tmp_path)
    tree = _tree(instance)
    first_path = capability_path(NAME, 1)
    first = parse_capability(tree[first_path], path=first_path)
    _request, request_digest = _contract_bytes(tree, "acme.match.request")
    assert first.in_.digest == request_digest
    assert first.predecessor_digest is None
    _accept(instance, owner, _capability(2, description="One document of (component, match)."))
    tree = _tree(instance)
    second_path = capability_path(NAME, 2)
    second = parse_capability(tree[second_path], path=second_path)
    assert second.predecessor_digest == capability_digest(first).tagged
    assert tree[first_path] == render_capability(first)


def test_a_version_chain_in_one_change_set_stages_in_version_order(tmp_path: Path) -> None:
    """v1..v10 together: v10 stages after v9, not after v1 as identity bytes sort."""

    instance, owner = initialize_local(tmp_path)
    chain = tuple(
        _capability(version, description=f"Version {version}.") for version in range(1, 11)
    )
    _accept(instance, owner, ChangeSetInput(kind="change_set", members=(*_contracts(), *chain)))
    tree = _tree(instance)
    previous: str | None = None
    for version in range(1, 11):
        path = capability_path(NAME, version)
        capability = parse_capability(tree[path], path=path)
        assert capability.predecessor_digest == previous
        previous = capability_digest(capability).tagged


# -- the law through real acceptance -----------------------------------------------------


def test_raw_acceptance_admits_a_lawful_version(tmp_path: Path) -> None:
    instance, _owner = _world(tmp_path)
    successor = _raw(_tree(instance), 2, description="A second wording.")
    accept_tree(instance, _files(successor), name="raw-successor")
    assert capability_path(NAME, 2) in _tree(instance)


def test_raw_acceptance_refuses_changing_an_accepted_version(tmp_path: Path) -> None:
    instance, _owner = _world(tmp_path)
    changed = _raw(_tree(instance), description="Different words, same version.")
    assert "cruxible.capability.immutable" in _refused_codes(instance, _files(changed))


def test_raw_acceptance_refuses_a_missing_predecessor(tmp_path: Path) -> None:
    instance, _owner = _world(tmp_path)
    tree = _tree(instance)
    orphan = _raw(tree, 1).model_copy(
        update={"version": 3, "predecessor_digest": "sha256:" + "a" * 64}
    )
    assert "cruxible.capability.predecessor_missing" in _refused_codes(instance, _files(orphan))


def test_raw_acceptance_refuses_a_mismatched_predecessor(tmp_path: Path) -> None:
    instance, _owner = _world(tmp_path)
    wrong = _raw(_tree(instance), 2, predecessor_digest="sha256:" + "b" * 64)
    assert "cruxible.capability.predecessor_mismatch" in _refused_codes(instance, _files(wrong))


def test_raw_acceptance_refuses_a_changed_effect(tmp_path: Path) -> None:
    instance, _owner = _world(tmp_path)
    louder = _raw(_tree(instance), 2, effect="external_read")
    assert "cruxible.capability.effect_changed" in _refused_codes(instance, _files(louder))


def test_raw_acceptance_refuses_an_unresolved_contract_pin(tmp_path: Path) -> None:
    instance, _owner = _world(tmp_path)
    missing = _raw(
        _tree(instance),
        name="acme.other",
        **{"in": {"name": "acme.absent", "version": 1, "digest": "sha256:" + "c" * 64}},
    )
    assert "cruxible.capability.pin_unresolved" in _refused_codes(instance, _files(missing))


def test_raw_acceptance_refuses_a_contract_pin_digest_mismatch(tmp_path: Path) -> None:
    instance, _owner = _world(tmp_path)
    stale = _raw(
        _tree(instance),
        name="acme.other",
        **{"in": {"name": "acme.match.request", "version": 1, "digest": "sha256:" + "d" * 64}},
    )
    assert "cruxible.capability.pin_digest_mismatch" in _refused_codes(instance, _files(stale))


def test_raw_acceptance_reserves_workspace_names_to_core(tmp_path: Path) -> None:
    instance, _owner = _world(tmp_path)
    tree = _tree(instance)
    squatter = _raw(tree, name="workspace.scan")
    assert "cruxible.capability.reserved" in _refused_codes(instance, _files(squatter))
    # A name core's builtin serves is declarable (the base kit owns it).
    accept_tree(instance, _files(_raw(tree, name="workspace.file")), name="builtin-name")
    assert capability_path("workspace.file", 1) in _tree(instance)


def test_raw_acceptance_refuses_removing_a_capability(tmp_path: Path) -> None:
    instance, _owner = _world(tmp_path)
    base = instance.accepted_coordinate()
    tree = dict(instance.tree_at(base.git_oid))
    del tree[capability_path(NAME, 1)]
    proposed = instance.proposal_service().submit(
        actor=ACTOR,
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/remove-capability", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp="2026-10-09T11:59:00.000000Z",
    )
    assert proposed.candidate is None
    assert "cruxible.capability.removal_unsupported" in {
        item.code for item in proposed.evaluation.diagnostics
    }


# -- the law through authoring -----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (_capability(2), "cruxible.capability.predecessor_missing"),
        (_capability(request="acme.absent@1"), "cruxible.capability.pin_unresolved"),
        (_capability(name="workspace.scan"), "cruxible.capability.reserved"),
    ],
)
def test_authoring_refuses_with_the_law_code_and_a_repair(
    tmp_path: Path, value: CapabilityInput, code: str
) -> None:
    instance, _owner = initialize_local(tmp_path)
    accept_tree(instance, _contract_files(), name="contracts")
    refused, repair = _authoring_refusal(instance, value)
    assert refused == code
    assert repair


def test_authoring_refuses_rewording_or_changing_the_effect_of_an_accepted_name(
    tmp_path: Path,
) -> None:
    instance, _owner = _world(tmp_path)
    reworded, repair = _authoring_refusal(
        instance, _capability(description="New words, version 1.")
    )
    assert (reworded, repair) == ("cruxible.capability.immutable", "Author acme.match@2 instead.")
    louder, _repair = _authoring_refusal(instance, _capability(2, effect="external_read"))
    assert louder == "cruxible.capability.effect_changed"


def _contract_files() -> dict[str, bytes]:
    request = Contract.model_validate(
        {"name": "acme.match.request", "version": 1, "fields": {"n": "int{0..9}"}}
    )
    result = Contract.model_validate(
        {"name": "acme.match.result", "version": 1, "fields": {"document": "capture<*>"}}
    )
    return {contract_path(c.name, c.version): render_contract(c) for c in (request, result)}


# -- the index and the reads -------------------------------------------------------------


def test_the_index_holds_one_row_per_version_from_the_accepted_generation(
    tmp_path: Path,
) -> None:
    instance, owner = _world(tmp_path)
    _accept(instance, owner, _capability(2, description="Second wording."))
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        connection = projection.typed.connection
        rows = connection.execute(
            "SELECT identity, capability_name, capability_version, effect, in_contract, "
            "out_contract FROM capabilities ORDER BY capability_version"
        ).fetchall()
        count = connection.execute(
            "SELECT amount FROM capability_count WHERE singleton=1"
        ).fetchone()
    assert [tuple(row) for row in rows] == [
        (
            f"Capability:{NAME}/v{version}",
            NAME,
            version,
            "pure",
            "Contract:acme.match.request/v1",
            "Contract:acme.match.result/v1",
        )
        for version in (1, 2)
    ]
    assert tuple(count) == (2,)


def test_capability_lookup_at_resolves_pins_and_lists_by_name(tmp_path: Path) -> None:
    instance, owner = _world(tmp_path)
    first_coordinate = instance.accepted_coordinate()
    _accept(instance, owner, _capability(2, description="Second wording."))
    head = instance.accepted_coordinate()
    lookup = capability_lookup_at(instance, head)
    first = lookup(CapabilityRef(name=NAME, version=1))
    assert first is not None and first.version == 1
    pinned = CapabilityRef(name=NAME, version=1, digest=capability_digest(first).tagged)
    assert lookup(pinned) == first
    assert lookup(pinned.model_copy(update={"digest": "sha256:" + "e" * 64})) is None
    assert lookup(CapabilityRef(name="acme.absent", version=1)) is None
    # The lookup is at its coordinate: version 2 is absent at the earlier base.
    assert (
        capability_lookup_at(instance, first_coordinate)(CapabilityRef(name=NAME, version=2))
        is None
    )
    assert [c.version for c in capabilities_by_name(instance, head, NAME)] == [1, 2]
    assert capabilities_by_name(instance, head, "acme.absent") == ()


def test_get_reads_a_capability_by_version_or_newest(tmp_path: Path) -> None:
    from cruxible_client.contracts.get_reads import GetCapabilityCard, GetRequest
    from cruxible_core.service.discovery.get import service_playbill_get

    instance, owner = _world(tmp_path)
    _accept(instance, owner, _capability(2, description="Second wording."))
    newest = service_playbill_get(
        instance, request=GetRequest(ref=f"Capability:{NAME}"), access=_ACCESS
    )
    assert newest.kind == "capability"
    assert isinstance(newest.card, GetCapabilityCard)
    assert newest.card.capability == f"Capability:{NAME}@2"
    assert newest.card.predecessor == f"{NAME}@1"
    first = service_playbill_get(
        instance, request=GetRequest(ref=f"Capability:{NAME}@1"), access=_ACCESS
    )
    assert isinstance(first.card, GetCapabilityCard)
    card = first.card.model_dump(mode="json")
    assert card["capability"] == f"Capability:{NAME}@1"
    assert card["effect"] == "pure"
    assert card["in"] == "acme.match.request@1"
    assert card["out"] == "acme.match.result@1"
    assert "predecessor" not in card
    by_path = service_playbill_get(
        instance, request=GetRequest(ref=capability_path(NAME, 1)), access=_ACCESS
    )
    assert by_path.kind == "capability"


def test_get_refuses_an_unknown_capability_with_the_nearest_names(tmp_path: Path) -> None:
    from cruxible_client.contracts.errors import ReadRefusalError
    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_core.service.discovery.get import service_playbill_get

    instance, _owner = _world(tmp_path)
    with pytest.raises(ReadRefusalError) as caught:
        service_playbill_get(
            instance, request=GetRequest(ref="Capability:acme.matc"), access=_ACCESS
        )
    assert caught.value.error_code == "cruxible.get.ref_not_found"
    assert "Capability:acme.match" in str(caught.value)


def test_orient_lists_capabilities_by_page_and_counts_them(tmp_path: Path) -> None:
    from cruxible_core.service.discovery.orient import service_playbill_orient

    instance, owner = _world(tmp_path)
    _accept(instance, owner, _capability(2, description="Second wording."))
    first = service_playbill_orient(instance, section="capabilities", limit=1)
    assert [row.capability for row in first.capabilities or ()] == [f"{NAME}@1"]
    assert first.truncated and first.next_cursor is not None
    row = (first.capabilities or ())[0].model_dump(mode="json")
    assert row == {
        "capability": f"{NAME}@1",
        "effect": "pure",
        "in": "acme.match.request@1",
        "out": "acme.match.result@1",
        "description": "From the components asked about, one document of matches.",
    }
    second = service_playbill_orient(
        instance, section="capabilities", limit=1, cursor=first.next_cursor
    )
    assert [row.capability for row in second.capabilities or ()] == [f"{NAME}@2"]
    assert not second.truncated
    default = service_playbill_orient(instance)
    assert default.artifacts is not None and default.artifacts.capabilities == 2


def test_a_fresh_instance_counts_no_capabilities(tmp_path: Path) -> None:
    from cruxible_core.service.discovery.orient import service_playbill_orient

    instance, _owner = initialize_local(tmp_path)
    default = service_playbill_orient(instance)
    assert default.artifacts is not None and default.artifacts.capabilities == 0
    assert service_playbill_orient(instance, section="capabilities").capabilities == ()


# -- kits carry capabilities -------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    raises=CapabilityFormatError,
    reason=(
        "owner: coordinator (service/kits.py is outside slice 1): a kit build gives every "
        "non-Contract definition a snapshot lifecycle; capabilities must keep their bytes "
        "like Contracts (patch in the slice 1 report)"
    ),
)
def test_a_kit_that_owns_a_capability_builds_adds_and_keeps_it(tmp_path: Path) -> None:
    from cruxible_client.contracts.kits import KitAddRequest, KitBuildRequest, KitRemoveRequest
    from cruxible_core.service.kits import build_kit, service_add_kit, service_remove_kit

    stamp = "2026-10-29T12:00:00.000000Z"
    (tmp_path / "publisher").mkdir()
    publisher, _owner = _world(tmp_path / "publisher")
    published = _tree(publisher)
    bundle = build_kit(published, KitBuildRequest(kit_id="acme", version="1.0.0", owns=("acme.",)))
    path = capability_path(NAME, 1)
    assert bundle.contents()[path] == published[path]
    (tmp_path / "consumer").mkdir()
    consumer, _owner = initialize_local(tmp_path / "consumer")

    def add() -> list[tuple[str, str]]:
        result = service_add_kit(
            consumer, KitAddRequest(bundle=bundle, dry_run=False), actor_id="owner", timestamp=stamp
        )
        assert result.status == "accepted"
        return [(item.path, item.action) for item in result.plan]

    assert (path, "add") in add()
    assert _tree(consumer)[path] == published[path]
    with consumer.bind_accepted_projection(consumer.accepted_coordinate()) as projection:
        assert projection.typed.envelope(f"Capability:{NAME}/v1") is not None
    removed = service_remove_kit(
        consumer, KitRemoveRequest(kit_id="acme", dry_run=False), actor_id="owner", timestamp=stamp
    )
    assert (path, "keep") in [(item.path, item.action) for item in removed.plan]
    assert (path, "unchanged") in add()


def test_the_cli_renders_the_card_and_the_list_page_by_their_public_field_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_core.cli.commands.playbill import _emit_get_text, _render_orient
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.service.discovery.orient import service_playbill_orient

    instance, _owner = _world(tmp_path)
    result = service_playbill_get(
        instance, request=GetRequest(ref=f"Capability:{NAME}", surface="cli"), access=_ACCESS
    )
    _emit_get_text(result)
    shown = capsys.readouterr().out
    assert "in: acme.match.request@1" in shown
    assert "out: acme.match.result@1" in shown
    page = service_playbill_orient(instance, section="capabilities", surface="cli")
    assert (
        f"{NAME}@1  pure  acme.match.request@1 -> acme.match.result@1"
        in _render_orient(page.model_dump(mode="json")).splitlines()
    )
