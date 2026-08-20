"""PC-G-S1a deterministic floor projection from accepted Playbill state."""

from __future__ import annotations

import json
from pathlib import Path

from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.playbill.service.query_definitions import (
    service_propose_playbill_query_definition,
)
from cruxible_core.service.playbill_floor import (
    COVERAGE_MANIFEST_PATH,
    MANIFEST_PATH,
    PlaybillFloorCoverageManifestV1,
    PlaybillFloorManifestV1,
    service_export_playbill_floor,
)
from tests.test_playbill._knowledge_loop_support import (
    PREDICATE,
    TIMESTAMP,
    accept_proposal,
    seed_claims,
    work_item_query,
)

CARD_PATH = "claim-types/project.work_item/status.card.json"
PROFILE_PATH = "subjects/project.work_item/wi-42.profile.json"


def _instance_with_query(tmp_path: Path):
    instance, owner = seed_claims(tmp_path)
    inspection = service_propose_playbill_query_definition(
        instance,
        query=work_item_query(),
        actor_id="owner",
        proposal_name="work-item-query",
        timestamp=TIMESTAMP,
    )
    accept_proposal(instance, owner, inspection, sequence=3)
    return instance, owner


def _manifest(floor: dict[str, bytes]) -> PlaybillFloorManifestV1:
    return PlaybillFloorManifestV1.model_validate(json.loads(floor[MANIFEST_PATH]))


def test_floor_carries_a_card_per_claim_type_and_a_profile_per_subject(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    floor = service_export_playbill_floor(instance)

    assert MANIFEST_PATH in floor
    assert CARD_PATH in floor
    assert PROFILE_PATH in floor
    assert "subjects/project.work_item/wi-43.profile.json" in floor


def test_manifest_binds_every_file_to_the_accepted_coordinate(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)
    accepted = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())

    floor = service_export_playbill_floor(instance)

    manifest = _manifest(floor)
    assert manifest.coordinate == accepted
    assert manifest.format == "playbill-floor-export-v1"
    listed = {item.path for item in manifest.files}
    assert listed == set(floor) - {MANIFEST_PATH}
    assert all(item.content_digest.startswith("sha256:") for item in manifest.files)
    assert all(item.byte_length == len(floor[item.path]) for item in manifest.files)


def test_manifest_file_inventory_is_byte_sorted(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    manifest = _manifest(service_export_playbill_floor(instance))

    paths = tuple(item.path for item in manifest.files)
    assert paths == tuple(sorted(paths, key=lambda item: item.encode("utf-8")))


def test_floor_is_byte_stable_for_one_accepted_coordinate(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    first = service_export_playbill_floor(instance)
    second = service_export_playbill_floor(instance)

    assert first == second
    assert _manifest(first).floor_digest == _manifest(second).floor_digest


def test_card_states_the_accepted_predicate_and_its_usage(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    card = json.loads(service_export_playbill_floor(instance)[CARD_PATH])

    assert card["predicate"] == PREDICATE
    assert card["at"]["git_oid"] == instance.accepted_coordinate().git_oid
    assert card["usage"]["subject_count"] == 2


def test_profile_states_the_accepted_claim_value_for_its_subject(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)

    profile = json.loads(service_export_playbill_floor(instance)[PROFILE_PATH])

    assert profile["subject_id"] == "wi-42"
    assert profile["subject_kind"] == "project.work_item"
    predicates = {row["predicate"] for row in profile["predicates"]}
    assert PREDICATE in predicates


def test_floor_is_pinned_to_the_requested_accepted_coordinate(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)
    accepted = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())

    floor = service_export_playbill_floor(instance, at=accepted)

    assert _manifest(floor).coordinate == accepted


def test_floor_grows_with_accepted_state_rather_than_being_frozen(tmp_path: Path) -> None:
    instance, owner = seed_claims(tmp_path)
    before = service_export_playbill_floor(instance)
    inspection = service_propose_playbill_query_definition(
        instance,
        query=work_item_query(),
        actor_id="owner",
        proposal_name="work-item-query",
        timestamp=TIMESTAMP,
    )
    accept_proposal(instance, owner, inspection, sequence=3)

    after = service_export_playbill_floor(instance)

    assert _manifest(before).coordinate != _manifest(after).coordinate
    assert _manifest(before).floor_digest != _manifest(after).floor_digest


def test_floor_carries_its_coverage_boundary_and_enumerates_it(tmp_path: Path) -> None:
    """§11.7: the exported floor is half the reference surface, boundary included."""

    instance, _owner = _instance_with_query(tmp_path)
    accepted = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())

    floor = service_export_playbill_floor(instance)

    assert COVERAGE_MANIFEST_PATH in floor
    assert COVERAGE_MANIFEST_PATH in {item.path for item in _manifest(floor).files}
    boundary = PlaybillFloorCoverageManifestV1.model_validate(
        json.loads(floor[COVERAGE_MANIFEST_PATH])
    )
    assert boundary.coordinate == accepted
    assert boundary.instance_id == instance.descriptor.instance_id
    assert boundary.index_digest.startswith("sha256:")
    assert boundary.completeness == "complete"
    assert boundary.truncation_reason_codes == ()
    assert boundary.cited_commitment_count > 0
    # An export observes no working snapshot, so it proves no freshness and
    # therefore carries no epoch and no watcher.
    assert boundary.epoch is None
    assert boundary.watcher_health == "absent"
