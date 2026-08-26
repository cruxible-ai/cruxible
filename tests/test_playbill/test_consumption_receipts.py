"""Minimal service-owned Playbill consumption receipts."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import get_args

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.consumption import (
    QUALIFYING_CONSUMPTION_OPERATIONS,
    ConsumptionContextV1,
    ConsumptionOperation,
    build_consumption_receipt,
    consumption_aggregate,
    record_consumption,
)
from tests.test_playbill._support import initialize_local

NOW = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)


def test_qualifying_consumption_operations_exhaust_the_closed_wire_vocabulary() -> None:
    expected = {
        "playbill.claim.get",
        "playbill.claim_type.get",
        "playbill.coverage.resolve",
        "playbill.discover.match",
        "playbill.expand",
        "playbill.procedure.run.resolve",
        "playbill.query.run",
        "playbill.query_definition.get",
        "playbill.search.match",
        "playbill.subject.get",
    }

    assert set(get_args(ConsumptionOperation)) == set(QUALIFYING_CONSUMPTION_OPERATIONS) == expected


def _context(actor_id: str = "reader") -> ConsumptionContextV1:
    return ConsumptionContextV1(
        actor_context=GovernedActorContext(
            actor_type="service_account",
            actor_id=actor_id,
            org_id="org-test",
            operation_id="op-consume",
            timestamp=NOW,
        ),
        access_profile_id="playbill.coverage.read",
    )


def _artifact() -> tuple[ArtifactIdentity, str]:
    return (
        ArtifactIdentity(kind="ClaimType", name="status"),
        typed_digest(Sha256Value, "test-artifact-v1", {"name": "status"}).tagged,
    )


def test_identical_public_retry_collapses_and_aggregate_is_replay_derived(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())

    first = record_consumption(
        instance,
        context=_context(),
        operation="playbill.claim_type.get",
        coordinate=coordinate,
        artifacts=(_artifact(),),
    )
    retry = record_consumption(
        instance,
        context=_context(),
        operation="playbill.claim_type.get",
        coordinate=coordinate,
        artifacts=(_artifact(),),
    )
    aggregate = consumption_aggregate(instance)

    assert first == retry
    assert aggregate.initialized is True
    assert aggregate.consumption_epoch_generation == 0
    assert aggregate.artifacts[0].total_touch_count == 1
    assert aggregate.artifacts[0].qualifying_touch_count == 1
    assert aggregate.artifacts[0].touches_by_operation == (("playbill.claim_type.get", 1),)
    assert len(instance.review_operational_store().events(family="consumption")) == 2


def test_reader_operation_coordinate_and_digest_are_all_receipt_identity_inputs(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    identity, digest = _artifact()
    baseline = build_consumption_receipt(
        context=_context(),
        operation="playbill.claim_type.get",
        coordinate=coordinate,
        artifact_identity=identity,
        artifact_digest=digest,
    )

    variants = (
        build_consumption_receipt(
            context=_context("other-reader"),
            operation="playbill.claim_type.get",
            coordinate=coordinate,
            artifact_identity=identity,
            artifact_digest=digest,
        ),
        build_consumption_receipt(
            context=_context(),
            operation="playbill.search.match",
            coordinate=coordinate,
            artifact_identity=identity,
            artifact_digest=digest,
        ),
        build_consumption_receipt(
            context=_context(),
            operation="playbill.claim_type.get",
            coordinate=coordinate.model_copy(update={"git_oid": "f" * 64}),
            artifact_identity=identity,
            artifact_digest=digest,
        ),
        build_consumption_receipt(
            context=_context(),
            operation="playbill.claim_type.get",
            coordinate=coordinate,
            artifact_identity=identity,
            artifact_digest=typed_digest(Sha256Value, "test-artifact-v1", {"name": "other"}).tagged,
        ),
    )
    assert len({baseline.receipt_id, *(item.receipt_id for item in variants)}) == 5


def test_internal_read_without_outer_context_writes_nothing(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())

    assert (
        record_consumption(
            instance,
            context=None,
            operation="playbill.expand",
            coordinate=coordinate,
            artifacts=(_artifact(),),
        )
        == ()
    )
    assert instance.review_operational_store().head().initialized is False
