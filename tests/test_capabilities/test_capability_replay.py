"""A generation holding a capability replays: the edges it was accepted with reproduce.

Each later acceptance re-evaluates its parent generation from the parent's
carried state (``ledger/recovery.py``), and that state's dependency edges are
read back from the projection's pin rows, while the accepted generation's were
derived from the member bytes. Both must name the same edge set, or the next
acceptance refuses with ``generation candidate law/closure evidence diverged``.
These cases cover the shapes a capability can take into a tree: its request and
result one Contract, two versions in successive generations, and a kit's carriage.
"""

from __future__ import annotations

from pathlib import Path

from cruxible_client.contracts.capabilities import Capability, capability_path
from cruxible_client.contracts.shapes import (
    Contract,
    contract_digest,
    contract_path,
    render_contract,
)
from cruxible_core.capabilities.artifact import capability_pins
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._candidate_support import submit_member_candidate
from tests.core_support._identity_support import accept_tree
from tests.core_support._knowledge_loop_support import accept_proposal
from tests.core_support._support import initialize_local
from tests.test_capabilities.test_capability_artifacts import (
    NAME,
    _contract_files,
    _files,
    _raw,
    _tree,
    _world,
)

_ECHO = Contract.model_validate(
    {"name": "acme.echo.value", "version": 1, "fields": {"value": "string"}}
)


def _echo(version: int = 1, **changes: object) -> Capability:
    """A capability whose request and result are one Contract."""

    ref = {"name": _ECHO.name, "version": 1, "digest": contract_digest(_ECHO).tagged}
    data: dict[str, object] = {
        "name": "acme.echo",
        "version": version,
        "description": "Returns the value it is sent.",
        "effect": "pure",
        "in": ref,
        "out": ref,
    }
    data.update(changes)
    return Capability.model_validate(data)


def _later(instance: PlaybillInstance, owner: object, name: str, stamp: str) -> None:
    """Any later acceptance: one new Contract, through prepare-generation's replay."""

    later = Contract.model_validate(
        {"name": f"acme.{name}", "version": 1, "fields": {"x": "string"}}
    )
    inspection = submit_member_candidate(
        instance,
        members={contract_path(later.name, 1): render_contract(later)},
        actor_id="owner",
        proposal_name=name,
        proposal_family="procedure",
        timestamp=stamp,
    )
    accept_proposal(instance, owner, inspection)  # type: ignore[arg-type]


def _reopens(instance: PlaybillInstance) -> None:
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_coordinate() == instance.accepted_coordinate()


def test_a_capability_whose_request_and_result_are_one_contract_pins_it_once() -> None:
    (pin,) = capability_pins(_echo())
    assert pin.role == "contract-in"
    assert pin.target == _ECHO.identity


def test_a_later_acceptance_replays_a_capability_with_one_contract_both_ways(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    accept_tree(
        instance,
        {contract_path(_ECHO.name, 1): render_contract(_ECHO), **_files(_echo())},
        name="echo",
    )
    _later(instance, owner, "after-echo", "2026-10-09T12:01:00.000000Z")
    _later(instance, owner, "after-after", "2026-10-09T12:02:00.000000Z")
    _reopens(instance)


def test_a_later_acceptance_replays_a_capability_with_two_contracts(tmp_path: Path) -> None:
    instance, owner = _world(tmp_path)
    _later(instance, owner, "after-match", "2026-10-09T12:01:00.000000Z")
    _reopens(instance)


def test_two_versions_in_successive_generations_replay(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    accept_tree(
        instance,
        {contract_path(_ECHO.name, 1): render_contract(_ECHO), **_files(_echo())},
        name="echo-v1",
    )
    tree = _tree(instance)
    from cruxible_client.contracts.capabilities import capability_digest, parse_capability

    first_path = capability_path("acme.echo", 1)
    first = parse_capability(tree[first_path], path=first_path)
    accept_tree(
        instance,
        _files(
            _echo(
                2,
                description="Returns the value it is sent, unchanged.",
                predecessor_digest=capability_digest(first).tagged,
            )
        ),
        name="echo-v2",
    )
    _later(instance, owner, "after-v2", "2026-10-09T12:01:00.000000Z")
    _reopens(instance)


def test_a_capability_a_kit_carried_in_replays(tmp_path: Path) -> None:
    from cruxible_client.contracts.kits import KitAddRequest, KitBuildRequest
    from cruxible_core.service.kits import build_kit, service_add_kit

    (tmp_path / "publisher").mkdir()
    publisher, publisher_owner = initialize_local(tmp_path / "publisher")
    files = _contract_files()
    accept_tree(publisher, files, name="contracts")
    tree = _tree(publisher)
    accept_tree(
        publisher,
        {
            contract_path(_ECHO.name, 1): render_contract(_ECHO),
            **_files(_raw(tree), _echo()),
        },
        name="capabilities",
    )
    bundle = build_kit(
        _tree(publisher), KitBuildRequest(kit_id="acme", version="1.0.0", owns=("acme.",))
    )
    assert capability_path(NAME, 1) in bundle.contents()
    (tmp_path / "consumer").mkdir()
    consumer, owner = initialize_local(tmp_path / "consumer")
    added = service_add_kit(
        consumer,
        KitAddRequest(bundle=bundle, dry_run=False),
        actor_id="owner",
        timestamp="2026-10-29T12:00:00.000000Z",
    )
    assert added.status == "accepted"
    _later(consumer, owner, "after-kit", "2026-10-29T12:01:00.000000Z")
    _reopens(consumer)
    del publisher_owner
