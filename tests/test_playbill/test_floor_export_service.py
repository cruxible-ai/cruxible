"""PC-G-S1a deterministic floor projection from accepted Playbill state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cruxible_client import contracts
from cruxible_client.contracts.authoring.models import ProcedureAuthoringPayloadV1
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.service.documents import (
    PlaybillAcceptedCoordinate,
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.service.playbill_floor import (
    COVERAGE_MANIFEST_PATH,
    MANIFEST_PATH,
    PlaybillFloorCoverageManifestV2,
    PlaybillFloorManifestV1,
    PlaybillFloorManifestV2,
    PlaybillProcedureFloorCardV1,
    render_floor_json_v1,
    render_floor_json_v2,
    service_export_playbill_floor,
)
from tests.test_playbill._candidate_support import submit_query_definition_candidate
from tests.test_playbill._knowledge_loop_support import (
    PREDICATE,
    TIMESTAMP,
    accept_proposal,
    seed_claims,
    work_item_query,
)
from tests.test_playbill._support import client_material
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_authoring_procedures import _slot_definition

CARD_PATH = "claim-types/project.work_item/status.card.json"
PROFILE_PATH = "subjects/project.work_item/wi-42.profile.json"
PROCEDURE_CARD_PATH = "procedures/triage.card.json"


def _instance_with_query(tmp_path: Path):
    instance, owner = seed_claims(tmp_path)
    inspection = submit_query_definition_candidate(
        instance,
        query=work_item_query(),
        actor_id="owner",
        proposal_name="work-item-query",
        timestamp=TIMESTAMP,
    )
    accept_proposal(instance, owner, inspection)
    return instance, owner


def _manifest(floor: dict[str, bytes]) -> PlaybillFloorManifestV2:
    return PlaybillFloorManifestV2.model_validate(json.loads(floor[MANIFEST_PATH]))


def _instance_with_procedure(tmp_path: Path):
    instance, owner = _instance_with_query(tmp_path)
    actor = AuthenticatedActor(actor_id="owner")
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    intent = coordinator.create(
        actor=actor,
        payload=ProcedureAuthoringPayloadV1(
            definition=_slot_definition().model_dump(mode="json", by_alias=True),
            activation_policy="drain",
        ),
        canonical_timestamp="2026-08-21T12:00:00.000000Z",
    ).intent
    submitted = coordinator.submit(intent.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None
    assert submitted.status.candidate_digest is not None
    approval = _sign(
        client_material(instance.root.parent, instance),
        submitted.status.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=submitted.status.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="reviewer",
    )
    service_activate_playbill_proposal(
        instance,
        proposal_id=submitted.status.proposal_id,
        activated_by="owner",
    )
    return instance


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
    assert manifest.format == "playbill-floor-export-v2"
    listed = {item.path for item in manifest.files}
    assert listed == set(floor) - {MANIFEST_PATH}
    assert all(item.content_digest.startswith("sha256:") for item in manifest.files)
    assert all(item.byte_length == len(floor[item.path]) for item in manifest.files)
    assert all(
        item.content_digest == "sha256:" + hashlib.sha256(floor[item.path]).hexdigest()
        for item in manifest.files
    )


def test_v2_json_render_is_pretty_stable_and_v1_spelling_is_unchanged() -> None:
    payload = {"z": [2, 1], "a": "é"}

    assert render_floor_json_v1(payload) == canonical_bytes(payload) + b"\n"
    assert render_floor_json_v2(payload) == (
        '{\n  "a": "é",\n  "z": [\n    2,\n    1\n  ]\n}\n'.encode()
    )
    assert (
        PlaybillFloorManifestV1.model_validate(
            {
                "tag": "playbill-floor-manifest-v1",
                "format": "playbill-floor-export-v1",
                "coordinate": {
                    "git_oid": "1" * 64,
                    "semantic_root": "sha256:" + "2" * 64,
                    "generation_root": "sha256:" + "3" * 64,
                    "compiler_digest": "sha256:" + "4" * 64,
                },
                "files": [],
                "floor_digest": "sha256:" + "5" * 64,
            }
        ).format
        == "playbill-floor-export-v1"
    )


def test_floor_client_envelope_defaults_to_v2_and_still_reads_v1() -> None:
    coordinate = contracts.PlaybillAcceptedCoordinate(
        git_oid="1" * 64,
        semantic_root="sha256:" + "2" * 64,
        generation_root="sha256:" + "3" * 64,
        compiler_digest="sha256:" + "4" * 64,
    )

    assert (
        contracts.PlaybillFloorExport(
            coordinate=coordinate,
            manifest={},
            files=[],
        ).tag
        == "playbill-floor-export-v2"
    )
    assert (
        contracts.PlaybillFloorExport(
            tag="playbill-floor-export-v1",
            coordinate=coordinate,
            manifest={},
            files=[],
        ).tag
        == "playbill-floor-export-v1"
    )


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
    inspection = submit_query_definition_candidate(
        instance,
        query=work_item_query(),
        actor_id="owner",
        proposal_name="work-item-query",
        timestamp=TIMESTAMP,
    )
    accept_proposal(instance, owner, inspection)

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
    boundary = PlaybillFloorCoverageManifestV2.model_validate(
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


def test_procedure_floor_card_keeps_runnability_governance_and_track_record_separate(
    tmp_path: Path,
) -> None:
    instance = _instance_with_procedure(tmp_path)

    floor = service_export_playbill_floor(instance)
    card = PlaybillProcedureFloorCardV1.model_validate_json(floor[PROCEDURE_CARD_PATH])

    assert tuple(type(card).model_fields) == (
        "tag",
        "identity",
        "path",
        "artifact_digest",
        "accepted_coordinate",
        "input_contract",
        "output_contract",
        "binding_state",
        "capabilities",
        "budget",
        "hard_caps",
        "governance",
        "track_record",
    )
    assert card.identity.qualified == "Procedure:triage"
    assert card.binding_state == "binding_required"
    assert card.capabilities.node_kinds == ("project", "state_tap")
    assert card.capabilities.terminal_capability == 1
    assert card.governance.lifecycle.state == "live"
    assert card.governance.lifecycle.state == "live"
    assert card.track_record == ()
    assert card.accepted_coordinate == PlaybillAcceptedCoordinate.from_internal(
        instance.accepted_coordinate()
    )
    assert PROCEDURE_CARD_PATH in {item.path for item in _manifest(floor).files}
    (render_floor_json_v1,)
    (render_floor_json_v2,)
