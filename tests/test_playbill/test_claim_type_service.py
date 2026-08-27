"""PC-G-S1a governed ClaimType service reads and proposals."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactAuthority, ArtifactIdentity
from cruxible_client.contracts.claim_types import claim_type_digest, claim_type_path
from cruxible_client.contracts.errors import ClaimNotFoundError
from cruxible_core.playbill.service.claim_types import (
    service_get_playbill_claim_type,
    service_list_playbill_claim_types,
    service_propose_playbill_claim_type,
)
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from tests.test_playbill._knowledge_loop_support import PREDICATE, TIMESTAMP, seed_claims
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_claims import _claim_type


def test_accepted_claim_type_reads_back_at_its_accepted_coordinate(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    accepted = instance.accepted_coordinate()

    view = service_get_playbill_claim_type(instance, predicate=PREDICATE)

    assert view.coordinate.git_oid == accepted.git_oid
    assert view.predicate == PREDICATE
    assert view.identity == f"ClaimType:{PREDICATE}"
    assert view.path == claim_type_path(PREDICATE)
    assert view.artifact_digest == claim_type_digest(_claim_type()).tagged
    assert view.envelope["artifact_format"] == "playbill-claim-type-v1"


def test_claim_type_listing_is_the_byte_sorted_accepted_inventory(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)

    listing = service_list_playbill_claim_types(instance)

    paths = tuple(item.path for item in listing.claim_types)
    assert paths == tuple(sorted(paths, key=lambda item: item.encode("utf-8")))
    assert claim_type_path(PREDICATE) in paths
    assert listing.coordinate.git_oid == instance.accepted_coordinate().git_oid
    assert service_get_playbill_claim_type(instance, predicate=PREDICATE) in listing.claim_types


def test_absent_predicate_is_refused_rather_than_returned_empty(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)

    with pytest.raises(ClaimNotFoundError):
        service_get_playbill_claim_type(instance, predicate="project.work_item.absent")


def test_new_claim_type_preserves_but_does_not_enforce_authority_bytes(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    delegated = _claim_type().model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name="project.work_item.owner_note"),
            "predicate": "project.work_item.owner_note",
            "authority": ArtifactAuthority(
                propose_roles=("reviewer",),
                approve_roles=("reviewer",),
            ),
        }
    )

    proposed = service_propose_playbill_claim_type(
        instance,
        claim_type=delegated,
        actor_id="owner",
        proposal_name="claim-type-delegated-authority",
        timestamp=TIMESTAMP,
    )

    assert proposed.proposal.candidate is not None
    assert proposed.proposal.evaluation.diagnostics == ()


def test_claim_type_read_is_pinned_to_the_requested_accepted_coordinate(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    accepted = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())

    pinned = service_get_playbill_claim_type(instance, predicate=PREDICATE, at=accepted)

    assert pinned.coordinate == accepted
