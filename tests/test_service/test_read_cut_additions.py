"""The reads that replace the cut surfaces: head, orient sections, get refs, query dimensions."""

from __future__ import annotations

import pytest

from cruxible_client.contracts.write import PlaybillWriteRequestV1
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.discovery.orient import service_playbill_head
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._write_support import caller, seed_write_surface

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)


def _write(instance: PlaybillInstance, *changes: dict[str, object]) -> list[str]:
    request = PlaybillWriteRequestV1.model_validate({"because": "test", "changes": list(changes)})
    outcome = service_playbill_write(instance, request=request, caller=caller())
    assert outcome.status == "accepted", outcome
    return [str(change.claim) for change in outcome.changes]


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> PlaybillInstance:
    instance, _owner = seed_write_surface(tmp_path_factory.mktemp("read-cut"))
    return instance


def test_head_names_the_coordinate_and_generation_and_resolves_at(
    world: PlaybillInstance,
) -> None:
    head = service_playbill_head(world)
    history = world.accepted_history()

    assert head.instance == world.descriptor.instance_id
    assert head.coordinate.git_oid == world.accepted_coordinate().git_oid
    assert head.generation == history[-1].sequence
    earlier = service_playbill_head(world, at=history[0].oid[:12])
    assert earlier.generation == history[0].sequence
    assert earlier.coordinate.git_oid == history[0].oid


def test_orient_pages_the_principal_registry_and_get_reads_one_principal(
    world: PlaybillInstance,
) -> None:
    from cruxible_client.contracts.get_reads import PlaybillGetRequestV1
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.service.discovery.orient import service_playbill_orient

    result = service_playbill_orient(world, section="principals", surface="mcp")

    ids = [row.principal_id for row in result.principals or ()]
    assert "daemon" in ids and "owner" in ids
    assert result.next[0] == f'cruxible_playbill_get(ref="Principal:{ids[0]}")'
    card = service_playbill_get(
        world, request=PlaybillGetRequestV1(ref="Principal:owner"), access=_ACCESS
    )
    assert card.kind == "principal" and card.card is not None
    assert card.card.model_dump()["status"] == "active"
    proof = service_playbill_get(
        world, request=PlaybillGetRequestV1(ref="Principal:owner", detail="proof"), access=_ACCESS
    )
    assert proof.proof is not None and proof.proof["record"]["principal_id"] == "owner"
    with pytest.raises(Exception) as refused:
        service_playbill_get(
            world, request=PlaybillGetRequestV1(ref="Principal:ownr"), access=_ACCESS
        )
    assert "Principal:owner" in str(getattr(refused.value, "candidates", "")) or "owner" in str(
        refused.value
    )


def test_orient_pages_policies_in_force_and_get_reads_the_approval_policy(
    world: PlaybillInstance,
) -> None:
    from cruxible_client.contracts.get_reads import PlaybillGetRequestV1
    from cruxible_core.service.claims.policies import list_playbill_policies_in_force
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.service.discovery.orient import service_playbill_orient

    result = service_playbill_orient(world, section="policies", limit=500)
    rows = result.policies or ()
    expected = list_playbill_policies_in_force(world).policies
    assert [row.model_dump(mode="json") for row in rows] == [
        row.model_dump(mode="json") for row in expected
    ]
    approval = next(row for row in rows if row.policy_kind == "approval_policy")
    assert approval.declaring_artifact_identity == "ApprovalPolicy:instance"

    card = service_playbill_get(
        world, request=PlaybillGetRequestV1(ref="ApprovalPolicy:instance"), access=_ACCESS
    )
    assert card.kind == "approval_policy" and card.card is not None
    assert card.card.model_dump()["mode"] == approval.policy["mode"]
    by_path = service_playbill_get(
        world, request=PlaybillGetRequestV1(ref="governance/approval-policy.json"), access=_ACCESS
    )
    assert by_path.ref == "ApprovalPolicy:instance"
    history = service_playbill_get(
        world,
        request=PlaybillGetRequestV1(ref="ApprovalPolicy:instance", detail="history"),
        access=_ACCESS,
    )
    assert history.history is not None and history.history.revisions
