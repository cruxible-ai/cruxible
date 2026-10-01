"""World.values against a real daemon: every admitted field, and the Claims it serves.

``World.values`` reads Claim values through the served ``query`` verb. These
run the SDK over an in-process HTTP app on a real instance: what is under test
is the request the SDK builds (a kind with more fields than one query may
select) and what the service records about the Claims it served.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client.authoring.sdk import Playbill
from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.compact_query import PLAYBILL_QUERY_MAX_SELECT
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from tests.core_support._write_support import (
    CLAIM_TYPES,
    KIND,
    _claim_type,
    seed_write_vocabulary,
)

WI1 = f"{KIND}/wi-1"
TITLE = f"{KIND}.title"
#: Enough extra string fields that the kind admits more than one query can select.
EXTRA_FIELDS = tuple(f"extra_{index:02d}" for index in range(PLAYBILL_QUERY_MAX_SELECT + 1))


def _accept_extra_fields(instance: PlaybillInstance, *, actor_id: str) -> None:
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    for field in EXTRA_FIELDS:
        claim_type = _claim_type(field, literal_schema={"type": "string"})
        tree[claim_type_path(claim_type.predicate)] = render_claim_type(claim_type)
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id=actor_id),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/wide-vocabulary",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp="2026-09-29T11:59:30.000000Z",
    )
    assert proposed.candidate is not None, proposed.evaluation
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposed.admission.proposal_id, activated_by=actor_id
    )
    assert receipt.status == "accepted"


def _sdk(client: TestClient, instance_id: str, tmp_path: Path) -> Playbill:
    transport = CruxibleClient(base_url="http://testserver")
    transport._client._client = client  # type: ignore[attr-defined]  # noqa: SLF001
    workspace = tmp_path / "sdk-workspace"
    workspace.mkdir(exist_ok=True)
    return Playbill._from_client(  # noqa: SLF001
        transport,
        instance_id=instance_id,
        workspace=workspace,
        clock=lambda: datetime(2026, 9, 29, 12, tzinfo=UTC),
    )


@pytest.fixture
def served(playbill_http: tuple[TestClient, str, Path]) -> tuple[TestClient, str, str]:
    client, instance_id, _key = playbill_http
    actor = client.get(f"/api/v1/{instance_id}/playbill/whoami").json()["actor_id"]
    seed_write_vocabulary(get_playbill_manager().get(instance_id), actor_id=actor)
    return client, instance_id, actor


def test_values_reads_every_field_of_a_kind_wider_than_one_select(
    served: tuple[TestClient, str, str], tmp_path: Path
) -> None:
    client, instance_id, actor = served
    _accept_extra_fields(get_playbill_manager().get(instance_id), actor_id=actor)
    pb = _sdk(client, instance_id, tmp_path)
    title = pb.set(WI1, "title", "Tidy the CLI", because="Named in review.")
    last = pb.set(WI1, EXTRA_FIELDS[-1], "the last field", because="Named in review.")
    world = pb.world()
    admitted = len(CLAIM_TYPES) + len(EXTRA_FIELDS)
    assert len(world.predicates) == admitted > PLAYBILL_QUERY_MAX_SELECT

    values = world.values(subjects=[WI1])

    assert {(item.predicate, item.value, item.claim) for item in values} == {
        (TITLE, "Tidy the CLI", title.changes[0].claim),
        (f"{KIND}.{EXTRA_FIELDS[-1]}", "the last field", last.changes[0].claim),
    }
