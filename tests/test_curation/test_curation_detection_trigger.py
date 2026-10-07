"""Curation detection runs as the curation.detect internal action; the list is a pure read."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from cruxible_client.contracts.triggers import INTERNAL_ACTIONS
from cruxible_core.consumers.next import curation as part
from cruxible_core.coverage.contracts import CoverageAccessProfile
from cruxible_core.ledger.bootstrap import seeded_triggers
from cruxible_core.service.discovery.curation import (
    PlaybillCurationListRequestV1,
    service_list_playbill_curation,
)
from tests.core_support._knowledge_loop_support import seed_claims

FIRED = datetime(2026, 9, 1, 12, tzinfo=UTC)
REQUEST = PlaybillCurationListRequestV1(access_profile=CoverageAccessProfile(profile_id="t"))


def test_new_instances_seed_a_live_generation_accepted_detection_trigger() -> None:
    (trigger,) = (item for item in seeded_triggers() if item.identity.name == "curation-detect")
    assert trigger.schedule.kind == "generation_accepted"
    assert trigger.target.action == "curation.detect"
    spec = INTERNAL_ACTIONS["curation.detect"]
    assert (spec.effect, spec.consumer, spec.part) == ("findings", "next", "curation")


def test_the_list_writes_nothing_and_says_detection_has_not_run(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)

    listed = service_list_playbill_curation(instance, request=REQUEST)

    assert instance.review_operational_store().events() == ()
    assert listed.detection.state == "never_run"
    assert listed.detection.trigger == "live"
    assert listed.detector_coverage == ()
    assert {item.pattern_kind for item in listed.inactive_detectors} == {
        "playbill.curation.block_churn.v1",
        "playbill.curation.dead_vocabulary.v1",
    }


def test_a_fire_runs_detection_at_its_instant_and_the_list_reads_it(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    instance, _owner = seed_claims(tmp_path)
    fired = [SimpleNamespace(sequence=7, fired_at=FIRED)]
    monkeypatch.setattr(
        part,
        "trigger_events",
        lambda _instance, *, after, action, limit: [
            event for event in fired if event.sequence > after
        ],
    )
    manager = SimpleNamespace(get=lambda _instance_id: instance)

    (work,) = part._PART.due(instance, now=FIRED)
    part._PART.run(manager, "inst", work, now=datetime(2030, 1, 1, tzinfo=UTC))

    listed = service_list_playbill_curation(instance, request=REQUEST)
    assert listed.detection.state == "current"
    assert listed.detection.detected_at == FIRED
    assert listed.detection.detected_through_generation == listed.generation
    assert listed.detector_coverage
    assert tuple(part._PART.due(instance, now=FIRED)) == ()
    # Recorded at the fire's instant, never the worker's clock.
    recorded = [event for event, _payload in instance.review_operational_store().events()]
    assert all(event.recorded_at == FIRED for event in recorded)
