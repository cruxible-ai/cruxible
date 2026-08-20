"""Shared seeding for the PC-F3-S1 native render-lens tests.

The render input is assembled from the same served projections the CLI reads, so
the state these tests render is accepted state rather than a fixture, and the
one builder both surfaces use is exercised here rather than mirrored.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.keys import GeneratedKeyMaterial
from cruxible_core.playbill.native import (
    NativeAcceptedStateV1,
    NativeRenderV1,
    RenderContextV1,
    artifact_record_from_projection,
    build_native_render,
    build_native_state,
    claim_record_from_projection,
    native_boundary_from_floor,
    whole_scope_context,
)
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.service.claim_types import service_list_playbill_claim_types
from cruxible_core.playbill.service.documents import service_list_playbill_documents
from cruxible_core.playbill.service.query_definitions import (
    service_list_playbill_query_definitions,
    service_propose_playbill_query_definition,
)
from cruxible_core.playbill.service.subjects import service_list_playbill_subjects
from cruxible_core.service.playbill_claims import service_list_playbill_claims
from cruxible_core.service.playbill_floor import (
    COVERAGE_MANIFEST_PATH,
    service_export_playbill_floor,
)
from tests.test_playbill._knowledge_loop_support import (
    EVALUATION_TIME,
    TIMESTAMP,
    accept_proposal,
    seed_claims,
    work_item_query,
)

WI_42 = "subjects/project.work_item/wi-42.md"
WI_43 = "subjects/project.work_item/wi-43.md"
READ_TIME = datetime.fromisoformat(EVALUATION_TIME)


def seed_native_instance(tmp_path: Path) -> tuple[PlaybillInstance, GeneratedKeyMaterial]:
    """Two accepted work-item Claims plus one accepted named entrypoint."""

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


def native_state(instance: PlaybillInstance) -> NativeAcceptedStateV1:
    """Assemble the render input from the served reads, exactly as the CLI does."""

    floor = service_export_playbill_floor(instance)
    accepted = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    return build_native_state(
        instance_id=instance.descriptor.instance_id,
        at=accepted,
        boundary=native_boundary_from_floor(json.loads(floor[COVERAGE_MANIFEST_PATH])),
        subjects=[
            artifact_record_from_projection("Subject", view.envelope)
            for view in service_list_playbill_subjects(instance).subjects
        ],
        claim_types=[
            artifact_record_from_projection(
                "ClaimType",
                view.envelope,
                path=view.path,
                identity=view.identity,
                artifact_digest=view.artifact_digest,
            )
            for view in service_list_playbill_claim_types(instance).claim_types
        ],
        query_definitions=[
            artifact_record_from_projection(
                "QueryDefinition",
                view.envelope,
                path=view.path,
                identity=view.identity,
                artifact_digest=view.artifact_digest,
            )
            for view in service_list_playbill_query_definitions(instance).query_definitions
        ],
        documents=[
            artifact_record_from_projection("Document", view.envelope)
            for view in service_list_playbill_documents(
                instance,
                access=BodyAccessContext(principal_id="playbill-native"),
            ).documents
        ],
        claims=[
            claim_record_from_projection(view.envelope, list(view.facts))
            for view in service_list_playbill_claims(instance).claims
        ],
    )


def native_context(state: NativeAcceptedStateV1) -> RenderContextV1:
    """One explicit render context: this generation, this instant, whole scope."""

    return whole_scope_context(
        instance_id=state.instance_id,
        at=state.at,
        evaluation_time=READ_TIME,
        access_profile=CoverageAccessProfileV1(profile_id=state.boundary.access_profile_id),
    )


def seeded_render(tmp_path: Path) -> tuple[NativeAcceptedStateV1, RenderContextV1, NativeRenderV1]:
    """Seed, assemble, and render in one step: the fixture most tests want."""

    instance, _owner = seed_native_instance(tmp_path)
    state = native_state(instance)
    ctx = native_context(state)
    return state, ctx, build_native_render(state, ctx)
