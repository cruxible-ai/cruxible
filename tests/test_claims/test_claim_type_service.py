"""PC-G-S1a governed ClaimType service reads and proposals."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_types import (
    CLAIM_TYPE_FORMAT,
    claim_type_digest,
    claim_type_path,
)
from cruxible_client.contracts.errors import ClaimNotFoundError
from cruxible_client.contracts.policies import ClaimEvidenceAdmissionPolicy
from cruxible_core.service.authoring.documents import AcceptedCoordinate
from cruxible_core.service.claims.claim_types import (
    service_get_playbill_claim_type,
    service_propose_playbill_claim_type,
)
from tests.core_support._knowledge_loop_support import (
    PREDICATE,
    TIMESTAMP,
    seed_claims,
    work_item_query,
)
from tests.core_support._support import initialize_local
from tests.test_claims.test_claims import _claim_type


def test_accepted_claim_type_reads_back_at_its_accepted_coordinate(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    accepted = instance.accepted_coordinate()

    view = service_get_playbill_claim_type(instance, predicate=PREDICATE)

    assert view.coordinate.git_oid == accepted.git_oid
    assert view.predicate == PREDICATE
    assert view.identity == f"ClaimType:{PREDICATE}"
    assert view.path == claim_type_path(PREDICATE)
    from cruxible_client.contracts.claim_types import parse_claim_type

    path = claim_type_path(PREDICATE)
    source_type = parse_claim_type(instance.blob_at(accepted.git_oid, path), path=path)
    assert work_item_query(claim_type=source_type).referenced_predicates == (PREDICATE,)
    assert view.artifact_digest == claim_type_digest(source_type).tagged
    assert view.envelope["artifact_format"] == CLAIM_TYPE_FORMAT


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
            # A rule names its contracts by identity, and a change set carries
            # what it names; a fresh instance holds no capture contract to name.
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(),
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
    accepted = AcceptedCoordinate.from_internal(instance.accepted_coordinate())

    pinned = service_get_playbill_claim_type(instance, predicate=PREDICATE, at=accepted)

    assert pinned.coordinate == accepted
