"""Minimal service-owned Playbill consumption receipts."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import get_args

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.curation.review_operational import ReviewOperationalStoreError
from cruxible_core.exhaust.consumption import (
    QUALIFYING_CONSUMPTION_OPERATIONS,
    ConsumptionContextV1,
    ConsumptionOperation,
    build_consumption_receipt,
    consumption_aggregate,
    consumption_artifacts_for_dependency_closure,
    record_consumption,
)
from cruxible_core.governance.actor_context import GovernedActorContext
from tests.core_support._support import initialize_local


@pytest.fixture(autouse=True)
def _consumption_receipts_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests exercise recorded receipts, which a local daemon leaves off.
    monkeypatch.setenv("CRUXIBLE_CONSUMPTION_RECEIPTS", "on")


NOW = datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)


def test_procedure_consumption_reads_only_its_transitive_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_client.contracts.procedures.artifacts import procedure_path
    from cruxible_core.claims.closure import dependency_artifacts
    from tests.test_procedures.test_procedure_run_surface import _world

    instance, _owner, procedure = _world(tmp_path)
    accepted = instance.accepted_coordinate()
    states = dependency_artifacts(instance.tree_at(accepted.git_oid))
    by_identity = {state.identity.qualified: state for state in states}
    reachable = {procedure.identity.qualified}
    while True:
        expanded = reachable | {
            pin.target.qualified
            for identity in reachable
            for pin in by_identity[identity].pins
            if pin.target.qualified in by_identity
        }
        if expanded == reachable:
            break
        reachable = expanded
    expected = tuple(
        (by_identity[identity].identity, by_identity[identity].artifact_digest)
        for identity in sorted(reachable)
    )
    assert len(reachable) < len(states)
    allowed = {by_identity[identity].path for identity in reachable}
    with instance.bind_accepted_projection(accepted) as projection:
        reader_type = type(projection.typed)
    member_bytes = reader_type.member_bytes

    def selected_member(reader, path):
        assert path in allowed
        return member_bytes(reader, path)

    monkeypatch.setattr(reader_type, "member_bytes", selected_member)
    monkeypatch.setattr(
        instance, "tree_at", lambda _oid: pytest.fail("receipt closure must not load the world")
    )
    assert (
        consumption_artifacts_for_dependency_closure(
            instance,
            AcceptedCoordinate.from_internal(accepted),
            procedure_path(procedure.identity.name),
        )
        == expected
    )
    assert (
        consumption_artifacts_for_dependency_closure(
            instance, AcceptedCoordinate.from_internal(accepted), "procedures/missing.json"
        )
        == ()
    )


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


def test_consumption_checks_the_complete_coordinate_before_writing(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    with pytest.raises(ReviewOperationalStoreError, match="coordinate is not accepted"):
        record_consumption(
            instance,
            context=_context(),
            operation="playbill.claim_type.get",
            coordinate=coordinate.model_copy(update={"semantic_root": "sha256:" + "a" * 64}),
            artifacts=(_artifact(),),
        )
    assert instance.review_operational_store().head().initialized is False


def test_a_local_daemon_records_no_receipts_and_dead_vocabulary_stands_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.curation.curation_detectors import _dead_vocabulary

    monkeypatch.delenv("CRUXIBLE_CONSUMPTION_RECEIPTS")
    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    assert (
        record_consumption(
            instance,
            context=_context(),
            operation="playbill.claim_type.get",
            coordinate=coordinate,
            artifacts=(_artifact(),),
        )
        == ()
    )
    assert instance.review_operational_store().head().initialized is False
    detections, coverage = _dead_vocabulary(
        instance=instance,
        tree=instance.tree_at(coordinate.git_oid),
        generation=0,
        operational_head_digest="sha256:" + "0" * 64,
    )
    assert detections == ()
    assert [item.reason for item in coverage.omissions] == ["consumption_receipts_off"]


def test_an_unknown_receipt_setting_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from cruxible_client.contracts.errors import PlaybillFormatError
    from cruxible_core.exhaust.consumption import consumption_receipts_enabled

    monkeypatch.setenv("CRUXIBLE_CONSUMPTION_RECEIPTS", "sometimes")
    with pytest.raises(PlaybillFormatError, match="'off' or 'on'"):
        consumption_receipts_enabled()


def test_switching_receipts_off_then_on_leaves_a_closed_gap_not_zero_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.exhaust import consumption

    instance, _owner = initialize_local(tmp_path)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())

    def read() -> None:
        record_consumption(
            instance,
            context=_context(),
            operation="playbill.claim_type.get",
            coordinate=coordinate,
            artifacts=(_artifact(),),
        )

    def restart() -> None:
        consumption._OBSERVATION_CHECKED.clear()  # a new daemon process

    read()  # observing: the epoch exists
    assert consumption_aggregate(instance).observation_gap_open is False
    restart()
    monkeypatch.setenv("CRUXIBLE_CONSUMPTION_RECEIPTS", "off")
    read()
    read()
    aggregate = consumption_aggregate(instance)
    assert aggregate.observation_gap_open is True
    gaps = [
        payload
        for _event, payload in instance.review_operational_store().events(family="consumption")
        if payload.get("tag") == "playbill-consumption-gap-v1"
    ]
    assert len(gaps) == 1  # once per process, not per read
    restart()
    monkeypatch.setenv("CRUXIBLE_CONSUMPTION_RECEIPTS", "on")
    read()
    aggregate = consumption_aggregate(instance)
    assert aggregate.observation_gap_open is False
    assert aggregate.observed_since_generation == 0


def test_a_historical_first_read_cannot_backdate_resumed_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.claims.closure import dependency_artifacts
    from cruxible_core.curation import curation_detectors as detectors
    from cruxible_core.exhaust import consumption
    from tests.core_support._knowledge_loop_support import seed_claims

    instance, _owner = seed_claims(tmp_path)
    history = instance.accepted_history()
    first = AcceptedCoordinate.from_internal(instance.coordinate_for_oid(history[0].oid))
    current = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    generation = len(history) - 1
    assert generation > 1
    consumption.ensure_consumption_epoch(
        instance, coordinate=first, generation=0, actor_context=_context().actor_context
    )
    tree = instance.tree_at(current.git_oid)
    vocabulary = next(a for a in dependency_artifacts(tree) if a.artifact_kind == "claim-type")
    monkeypatch.setenv("CRUXIBLE_CONSUMPTION_RECEIPTS", "off")
    record_consumption(
        instance,
        context=_context(),
        operation="playbill.claim_type.get",
        coordinate=current,
        artifacts=((vocabulary.identity, vocabulary.artifact_digest),),
    )
    assert consumption_aggregate(instance).observation_gap_open
    consumption._OBSERVATION_CHECKED.clear()  # a daemon restart into recording
    monkeypatch.setenv("CRUXIBLE_CONSUMPTION_RECEIPTS", "on")
    historical = AcceptedCoordinate.from_internal(instance.coordinate_for_oid(history[-2].oid))
    claim = next(
        a
        for a in dependency_artifacts(instance.tree_at(historical.git_oid))
        if a.artifact_kind == "claim"
    )
    (receipt,) = record_consumption(
        instance,
        context=_context(),
        operation="playbill.claim.get",
        coordinate=historical,
        artifacts=((claim.identity, claim.artifact_digest),),
    )
    # The receipt describes what it read; observation resumed at the head.
    assert receipt.accepted_coordinate == historical
    assert consumption_aggregate(instance).observed_since_generation == generation
    monkeypatch.setattr(detectors, "DEAD_VOCABULARY_MINIMUM_ZERO_TOUCH_GENERATIONS", 1)
    detections, _coverage = detectors._dead_vocabulary(
        instance=instance,
        tree=tree,
        generation=generation,
        operational_head_digest="sha256:" + "0" * 64,
    )
    assert vocabulary.identity not in {item.subject for item in detections}
