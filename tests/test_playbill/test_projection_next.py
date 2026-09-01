"""Visibility-safe projection repair rows and semantic, coordinate-independent freshness."""

from __future__ import annotations

import hashlib
import unittest.mock as mock
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_types import claim_type_digest
from cruxible_client.contracts.claims import claim_artifact_digest
from cruxible_client.contracts.declared_blocks import (
    PlaybillPresentationPolicyV2,
    PlaybillProjectionAdvisoryPolicyV1,
    PlaybillProjectionCoverageBindingV1,
    PlaybillProjectionCoverageObservationV1,
    ProjectionBackingV1,
    ProjectionBlockStampV1,
    ProjectionClaimBackingV1,
    ProjectionMarkerSummaryV1,
    ProjectionQueryBackingV1,
    ProjectionResolvedParameterBindingV1,
    projection_parameter_digest,
    projection_query_semantic_result_digest,
)
from cruxible_client.contracts.procedures.artifacts import render_procedure
from cruxible_client.contracts.projection import AcceptedCoordinate as ClientAcceptedCoordinate
from cruxible_client.contracts.query.grammar import (
    QueryEntryV1,
    QueryParameterDeclarationV1,
    QueryParameterRefV1,
)
from cruxible_client.contracts.source_catalog import ProcedureProjectionCatalogEntry
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.query.engine import evaluate_claim_query
from cruxible_core.playbill.service.query_definitions import (
    accepted_query_definition,
)
from cruxible_core.service.playbill_claims import (
    _claim_from_view,
    service_list_playbill_claims,
)
from cruxible_core.service.playbill_next import (
    PlaybillNextRequestV1,
    PlaybillNextSourceObservationV3,
    PlaybillNextWorkspaceObservationV1,
    _procedure_projection_items,
    service_playbill_next,
)
from cruxible_core.service.playbill_query import build_accepted_query_facts
from tests.test_playbill._candidate_support import submit_query_definition_candidate
from tests.test_playbill._claim_authoring_support import (
    DirectClaimAuthoringV1,
    ExistingStatementHandoffV1,
    service_propose_playbill_claim,
)
from tests.test_playbill._knowledge_loop_support import (
    QUERY_NAME,
    SUBJECT_KIND,
    TIMESTAMP,
    accept_proposal,
    activate,
    authoring,
    seed_claims,
    work_item_query,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_claims import _claim_type
from tests.test_playbill.test_graph_v4_provider_closure import _accepted_procedure
from tests.test_playbill.test_query_execution_service import _instance_with_query
from tests.test_playbill.test_reverse_drift_next import _published_world, _retire

NOW = datetime(2026, 8, 16, 21, tzinfo=UTC)
BODY = "sha256:" + hashlib.sha256(b"status: ready\n").hexdigest()
EDITED_BODY = "sha256:" + hashlib.sha256(b"status: blocked\n").hexdigest()


@pytest.fixture(scope="module")
def accepted_world(tmp_path_factory: pytest.TempPathFactory) -> PlaybillInstance:
    instance, _owner = _instance_with_query(tmp_path_factory.mktemp("projection-accepted"))
    return instance


def _claim_backing(instance: PlaybillInstance, *, stale: bool = False) -> ProjectionClaimBackingV1:
    facts = build_accepted_query_facts(instance, coordinate=instance.accepted_coordinate())
    row = facts.claims[0]
    return ProjectionClaimBackingV1(
        identity=row.accepted.claim.identity,
        statement_digest=("sha256:" + "f" * 64) if stale else row.accepted.statement_digest,
    )


def _query_backing(
    instance: PlaybillInstance,
    *,
    name: str = QUERY_NAME,
    at: datetime = NOW,
    parameters: tuple[ProjectionResolvedParameterBindingV1, ...] = (),
    stale: bool = False,
) -> ProjectionQueryBackingV1:
    coordinate = instance.accepted_coordinate()
    definition = accepted_query_definition(instance, name=name, coordinate=coordinate)
    result = evaluate_claim_query(
        definition,
        facts=build_accepted_query_facts(instance, coordinate=coordinate),
        coordinate=coordinate,
        evaluation_time=at,
        parameters={binding.name: binding.value for binding in parameters},
    )
    assert result.verdict == "completed"
    return ProjectionQueryBackingV1(
        identity=definition.query.identity,
        definition_digest=definition.artifact_digest,
        resolved_parameter_bindings=parameters,
        canonical_param_digest=projection_parameter_digest(parameters),
        declared_evaluation_time=at,
        semantic_result_digest=(
            "sha256:" + "e" * 64 if stale else projection_query_semantic_result_digest(result)
        ),
    )


def _request(
    instance: PlaybillInstance,
    *,
    backing: tuple[ProjectionBackingV1, ...],
    dirty: bool = False,
    evaluation_time: datetime = NOW,
    permitted: tuple[str, ...] = ("instance", "public"),
    start_byte: int = 0,
    complete: bool = True,
    marker_notes: tuple[str, ...] = (),
) -> PlaybillNextRequestV1:
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    stamp = ProjectionBlockStampV1(
        source_id="corpus.runbook",
        block_id="status",
        declared_generation=0,
        declared_coordinate=coordinate,
        backing=tuple(sorted(backing, key=lambda item: item.identity.qualified.encode())),
        body_digest=BODY,
    )
    marker = ProjectionMarkerSummaryV1(
        stamp=stamp,
        observed_body_digest=EDITED_BODY if dirty else BODY,
        start_byte=start_byte,
        end_byte=start_byte + 100,
    )
    return PlaybillNextRequestV1(
        at=coordinate,
        evaluation_time=evaluation_time,
        access_profile=CoverageAccessProfileV1(
            profile_id="projection-test",
            permitted_access_classes=permitted,  # type: ignore[arg-type]
        ),
        workspace_observation=PlaybillNextWorkspaceObservationV1(
            source_observations=(
                PlaybillNextSourceObservationV3(
                    tag="playbill-next-source-observation-v3",
                    source_id="corpus.runbook",
                    observed_source_digest="sha256:" + "a" * 64,
                    byte_length=1000,
                    marker_summaries=(marker,),
                    occurrences=(),
                    scanned_commitment_digests=(),
                    scan_complete=complete,
                    scan_notes=() if complete else ("coverage_partial",),
                    marker_notes=marker_notes,
                ),
            )
        ),
    )


def _projection_rows(instance: PlaybillInstance, request: PlaybillNextRequestV1):  # type: ignore[no-untyped-def]
    return tuple(
        item
        for item in service_playbill_next(instance, request=request).items
        if item.reason.startswith("projection_")
    )


def test_unprojected_procedure_advisory_is_coordinate_bound_and_policy_controlled(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinate = instance.accepted_coordinate()
    procedure = _accepted_procedure()

    class ProcedureTree:
        def tree_at(self, _oid: str) -> dict[str, bytes]:
            return {procedure.path: render_procedure(procedure.procedure)}

    public = ClientAcceptedCoordinate.model_validate(
        AcceptedCoordinate.from_internal(coordinate).model_dump(mode="json")
    )
    base_observation = PlaybillNextWorkspaceObservationV1(
        presentation_policy=PlaybillPresentationPolicyV2(),
        projection_coverage=PlaybillProjectionCoverageObservationV1(
            coordinate=public,
            complete_kinds=("Procedure",),
            bindings=(),
        ),
    )
    profile = CoverageAccessProfileV1(
        profile_id="procedure-projection-test",
        permitted_access_classes=("instance",),
    )

    (row,) = _procedure_projection_items(
        ProcedureTree(),  # type: ignore[arg-type]
        coordinate=coordinate,
        access_profile=profile,
        observation=base_observation,
    )
    assert row.severity == "warning"
    assert row.reason == "procedure_projection_missing"
    assert row.subject_identity == ".playbill/sources.yaml"
    assert row.related_identities == (procedure.procedure.identity.qualified,)
    assert row.repair.operation == "hand_edit"
    assert row.repair.command is None
    assert row.repair.target == ".playbill/sources.yaml"
    assert row.repair.required_change == "add_procedure_projection_catalog_entries"
    assert row.repair.arguments == {
        "catalog_entries": [
            {
                "kind": "procedure",
                "procedure_identity": procedure.procedure.identity.model_dump(mode="json"),
                "locator": f"procedures/{procedure.procedure.identity.name}.md",
            }
        ]
    }

    projected = base_observation.model_copy(
        update={
            "projection_coverage": PlaybillProjectionCoverageObservationV1(
                coordinate=public,
                complete_kinds=("Procedure",),
                bindings=(
                    PlaybillProjectionCoverageBindingV1(
                        artifact=procedure.procedure.identity,
                        workspace_path="runbooks/procedure.md",
                        evidence_kind="procedure_catalog",
                    ),
                ),
            )
        }
    )
    assert (
        _procedure_projection_items(
            ProcedureTree(),  # type: ignore[arg-type]
            coordinate=coordinate,
            access_profile=profile,
            observation=projected,
        )
        == ()
    )

    disabled = base_observation.model_copy(
        update={
            "presentation_policy": PlaybillPresentationPolicyV2(
                projection_advisories=PlaybillProjectionAdvisoryPolicyV1(procedure=False)
            )
        }
    )
    assert (
        _procedure_projection_items(
            ProcedureTree(),  # type: ignore[arg-type]
            coordinate=coordinate,
            access_profile=profile,
            observation=disabled,
        )
        == ()
    )

    foreign = base_observation.model_copy(
        update={
            "projection_coverage": base_observation.projection_coverage.model_copy(
                update={"coordinate": public.model_copy(update={"git_oid": "f" * 40})}
            )
        }
    )
    assert (
        _procedure_projection_items(
            ProcedureTree(),  # type: ignore[arg-type]
            coordinate=coordinate,
            access_profile=profile,
            observation=foreign,
        )
        == ()
    )


def test_service_next_coalesces_projection_advice_in_its_own_observed_domain(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    before = instance.accepted_coordinate()
    before_tree = instance.tree_at(before.git_oid)
    procedure = _accepted_procedure()
    real_tree_at = PlaybillInstance.tree_at

    def with_procedure(self, oid):  # type: ignore[no-untyped-def]
        tree = dict(real_tree_at(self, oid))
        tree[procedure.path] = render_procedure(procedure.procedure)
        return tree

    public = ClientAcceptedCoordinate.model_validate(
        AcceptedCoordinate.from_internal(before).model_dump(mode="json")
    )
    request = PlaybillNextRequestV1(
        evaluation_time=NOW,
        access_profile=CoverageAccessProfileV1(
            profile_id="procedure-service-test",
            permitted_access_classes=("instance",),
        ),
        workspace_observation=PlaybillNextWorkspaceObservationV1(
            presentation_policy=PlaybillPresentationPolicyV2(),
            projection_coverage=PlaybillProjectionCoverageObservationV1(
                coordinate=public,
                complete_kinds=("Procedure",),
                bindings=(),
            ),
        ),
    )

    observation = request.workspace_observation
    assert observation is not None
    coverage = observation.projection_coverage
    assert coverage is not None
    foreign = request.model_copy(
        update={
            "workspace_observation": observation.model_copy(
                update={
                    "projection_coverage": coverage.model_copy(
                        update={"coordinate": public.model_copy(update={"git_oid": "f" * 40})}
                    )
                }
            )
        }
    )
    with mock.patch.object(PlaybillInstance, "tree_at", with_procedure):
        result = service_playbill_next(instance, request=request)
        unobserved = service_playbill_next(
            instance,
            request=PlaybillNextRequestV1(
                evaluation_time=NOW,
                access_profile=request.access_profile,
            ),
        )
        foreign_result = service_playbill_next(instance, request=foreign)

    assert "workspace_projections" in result.observed_domains
    assert "workspace_sources" not in result.observed_domains
    (row,) = tuple(item for item in result.items if item.reason == "procedure_projection_missing")
    entries = row.repair.arguments["catalog_entries"]  # type: ignore[index]
    assert len(entries) == 1
    assert ProcedureProjectionCatalogEntry.model_validate(entries[0])
    assert not [item for item in unobserved.items if item.reason == "procedure_projection_missing"]
    assert "workspace_projections" in unobserved.unobserved_domains
    assert not [
        item for item in foreign_result.items if item.reason == "procedure_projection_missing"
    ]
    assert "workspace_projections" in foreign_result.unobserved_domains
    assert instance.accepted_coordinate() == before
    assert instance.tree_at(before.git_oid) == before_tree


def test_many_unprojected_procedures_coalesce_without_a_cardinality_cap(
    tmp_path: Path,
) -> None:
    from cruxible_client.contracts.procedures.artifacts import ProcedureArtifactV1
    from cruxible_client.contracts.procedures.graph import (
        compute_procedure_definition_digest_v4,
    )

    instance, _owner = initialize_local(tmp_path)
    coordinate = instance.accepted_coordinate()
    public = ClientAcceptedCoordinate.model_validate(
        AcceptedCoordinate.from_internal(coordinate).model_dump(mode="json")
    )
    template = _accepted_procedure().procedure
    real_tree_at = PlaybillInstance.tree_at

    def with_many(self, oid):  # type: ignore[no-untyped-def]
        tree = dict(real_tree_at(self, oid))
        for index in range(25):
            name = f"{template.definition.name}{index:02d}"
            definition = template.definition.model_copy(update={"name": name})
            artifact = ProcedureArtifactV1(
                identity=template.identity.model_copy(update={"name": name}),
                definition=definition,
                definition_digest=compute_procedure_definition_digest_v4(definition).tagged,
                pins=template.pins,
                activation_policy=template.activation_policy,
            )
            tree[f"procedures/{name}.json"] = render_procedure(artifact)
        return tree

    request = PlaybillNextRequestV1(
        evaluation_time=NOW,
        access_profile=CoverageAccessProfileV1(
            profile_id="many-procedures",
            permitted_access_classes=("instance",),
        ),
        workspace_observation=PlaybillNextWorkspaceObservationV1(
            presentation_policy=PlaybillPresentationPolicyV2(),
            projection_coverage=PlaybillProjectionCoverageObservationV1(
                coordinate=public,
                complete_kinds=("Procedure",),
                bindings=(),
            ),
        ),
    )

    with mock.patch.object(PlaybillInstance, "tree_at", with_many):
        result = service_playbill_next(instance, request=request)

    rows = tuple(item for item in result.items if item.reason == "procedure_projection_missing")
    assert len(rows) == 1
    row = rows[0]
    assert len(row.related_identities) == 25
    assert list(row.related_identities) == sorted(row.related_identities)
    assert len(row.repair.arguments["catalog_entries"]) == 25  # type: ignore[index]


def test_clean_claim_and_query_backings_do_not_stale_on_coordinate_or_time_alone(
    tmp_path: Path,
) -> None:
    instance, owner = _instance_with_query(tmp_path)
    request = _request(
        instance,
        backing=(_claim_backing(instance), _query_backing(instance)),
    )
    original_coordinate = instance.accepted_coordinate()
    original_rows = _projection_rows(instance, request)

    unrelated = submit_query_definition_candidate(
        instance,
        query=work_item_query("project.unrelated_items"),
        actor_id="owner",
        proposal_name="unrelated-projection-generation",
        timestamp=TIMESTAMP,
    )
    accept_proposal(instance, owner, unrelated)
    advanced_coordinate = instance.accepted_coordinate()
    assert advanced_coordinate.git_oid != original_coordinate.git_oid
    assert advanced_coordinate.generation_root != original_coordinate.generation_root

    advanced = request.model_copy(
        update={
            "at": AcceptedCoordinate.from_internal(advanced_coordinate),
            "evaluation_time": NOW + timedelta(hours=1),
        }
    )
    assert advanced.workspace_observation is not None
    assert advanced.workspace_observation.source_observations is not None
    source = advanced.workspace_observation.source_observations[0]
    assert isinstance(source, PlaybillNextSourceObservationV3)
    assert source.marker_summaries[0].stamp.declared_coordinate.git_oid == (
        original_coordinate.git_oid
    )
    assert original_rows == _projection_rows(instance, advanced) == ()


def test_dirty_and_stale_rows_have_exact_frozen_repairs_and_deterministic_ids(
    accepted_world: PlaybillInstance,
) -> None:
    request = _request(
        accepted_world, backing=(_claim_backing(accepted_world, stale=True),), dirty=True
    )

    first = _projection_rows(accepted_world, request)
    repeat = _projection_rows(accepted_world, request)

    assert first == repeat
    assert {item.reason for item in first} == {"projection_dirty", "projection_backing_stale"}
    by_reason = {item.reason: item for item in first}
    assert by_reason["projection_dirty"].repair.required_change == (
        "verify_alignment_then_repin_or_edit"
    )
    assert by_reason["projection_backing_stale"].repair.required_change == (
        "review_block_supersede_prose_then_repin"
    )
    for item in first:
        assert item.severity == "repair"
        assert item.subject_identity == "corpus.runbook#status"
        assert item.repair.operation == "playbill.block.repin"
        assert item.repair.arguments == {"source_id": "corpus.runbook", "block_id": "status"}


def test_presentation_offsets_do_not_enter_projection_queue_identity(
    accepted_world: PlaybillInstance,
) -> None:
    backing = (_claim_backing(accepted_world),)
    (first,) = _projection_rows(
        accepted_world, _request(accepted_world, backing=backing, dirty=True, start_byte=0)
    )
    (moved,) = _projection_rows(
        accepted_world, _request(accepted_world, backing=backing, dirty=True, start_byte=400)
    )

    assert first == moved
    assert first.item_id == moved.item_id


def test_missing_hidden_or_incomplete_backing_omits_the_entire_block_without_disclosure(
    accepted_world: PlaybillInstance,
) -> None:
    visible = _claim_backing(accepted_world)
    hidden = ProjectionClaimBackingV1(
        identity=ArtifactIdentity(kind="Claim", name="CLM-00000000000000000000000000000000"),
        statement_digest="sha256:" + "b" * 64,
    )

    for request in (
        _request(accepted_world, backing=(visible, hidden), dirty=True),
        _request(accepted_world, backing=(visible,), dirty=True, complete=False),
    ):
        assert _projection_rows(accepted_world, request) == ()


def test_invalid_projection_marker_surfaces_source_level_blocking_row(
    accepted_world: PlaybillInstance,
) -> None:
    request = _request(
        accepted_world,
        backing=(_claim_backing(accepted_world),),
        marker_notes=("projection_marker_invalid",),
    )

    (row,) = _projection_rows(accepted_world, request)

    assert row.severity == "blocking"
    assert row.reason == "projection_marker_invalid"
    assert row.subject_identity == "corpus.runbook"
    assert row.related_identities == ()
    assert row.detail == {
        "source_id": "corpus.runbook",
        "error_code": "playbill.projection.marker_invalid",
        "marker_status": "invalid",
    }
    assert row.repair.target == "corpus.runbook"
    assert row.repair.required_change == "restore_projection_frame_then_repin"
    assert row.repair.arguments == {"source_id": "corpus.runbook"}
    assert row.repair.command is None


def test_missing_registered_projection_marker_surfaces_runnable_block_row(
    accepted_world: PlaybillInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(accepted_world, backing=(_claim_backing(accepted_world),))
    source = request.workspace_observation.source_observations[0]
    request = request.model_copy(
        update={
            "workspace_observation": request.workspace_observation.model_copy(
                update={
                    "source_observations": (source.model_copy(update={"marker_summaries": ()}),)
                }
            )
        }
    )
    monkeypatch.setattr(
        "cruxible_core.service.playbill_next._registered_publication_blocks",
        lambda _instance: frozenset({("corpus.runbook", "pub-status")}),
    )

    (row,) = _projection_rows(accepted_world, request)

    assert row.severity == "blocking"
    assert row.subject_identity == "corpus.runbook#pub-status"
    assert row.detail == {
        "source_id": "corpus.runbook",
        "block_id": "pub-status",
        "error_code": "playbill.projection.marker_invalid",
        "marker_status": "registered_marker_missing",
    }
    assert row.repair.arguments == {
        "source_id": "corpus.runbook",
        "block_id": "pub-status",
    }
    assert row.repair.command == "cruxible playbill block repin corpus.runbook pub-status"


def test_invalid_projection_marker_recovers_registered_block_identity(
    accepted_world: PlaybillInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(
        accepted_world,
        backing=(_claim_backing(accepted_world),),
        marker_notes=("projection_marker_invalid",),
    )
    monkeypatch.setattr(
        "cruxible_core.service.playbill_next._registered_publication_blocks",
        lambda _instance: frozenset({("corpus.runbook", "pub-status")}),
    )

    (row,) = _projection_rows(accepted_world, request)

    assert row.subject_identity == "corpus.runbook#pub-status"
    assert row.detail == {
        "source_id": "corpus.runbook",
        "block_id": "pub-status",
        "error_code": "playbill.projection.marker_invalid",
        "marker_status": "invalid",
    }
    assert row.repair.command == "cruxible playbill block repin corpus.runbook pub-status"


def test_subject_gated_claim_backing_omits_its_marker_under_instance_access(
    accepted_world: PlaybillInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = build_accepted_query_facts

    def without_visible_subjects(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        return original(*args, **kwargs).model_copy(update={"subjects": ()})  # type: ignore[arg-type]

    monkeypatch.setattr(
        "cruxible_core.service.playbill_next.build_accepted_query_facts",
        without_visible_subjects,
    )
    request = _request(
        accepted_world,
        backing=(_claim_backing(accepted_world),),
        dirty=True,
        permitted=("instance",),
    )

    assert _projection_rows(accepted_world, request) == ()


def test_retired_claim_backing_requires_depublication_without_access_disclosure(
    tmp_path: Path,
) -> None:
    instance, owner, claim_id = _published_world(tmp_path)
    backing = _claim_backing(instance)
    assert backing.identity.name == claim_id
    request = _request(instance, backing=(backing,))
    assert _projection_rows(instance, request) == ()

    _retire(instance, owner, claim_id)
    retired_request = request.model_copy(
        update={"at": AcceptedCoordinate.from_internal(instance.accepted_coordinate())}
    )
    (row,) = _projection_rows(instance, retired_request)
    assert row.reason == "projection_backing_stale"
    assert row.subject_identity == "corpus.runbook#status"
    assert row.related_identities == (backing.identity.qualified,)
    assert row.detail["retired_backings"] == [backing.identity.qualified]
    assert row.repair.operation == "playbill.block.repin"
    assert row.repair.required_change == "depublish_retired_backing_block"

    assert retired_request.workspace_observation is not None
    assert retired_request.workspace_observation.source_observations is not None
    source = retired_request.workspace_observation.source_observations[0]
    depublished = retired_request.model_copy(
        update={
            "workspace_observation": retired_request.workspace_observation.model_copy(
                update={
                    "source_observations": (source.model_copy(update={"marker_summaries": ()}),)
                }
            )
        }
    )
    assert _projection_rows(instance, depublished) == ()
    access_hidden = retired_request.model_copy(
        update={
            "access_profile": retired_request.access_profile.model_copy(
                update={"permitted_access_classes": ("public",)}
            )
        }
    )
    assert _projection_rows(instance, access_hidden) == ()


def test_overturned_claim_backing_requires_depublication(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    base_type = _claim_type()
    claim_type = base_type.model_copy(
        update={
            "cardinality": "many",
            "resolution_policy": base_type.resolution_policy.model_copy(
                update={
                    "cardinality": "many",
                    "eligible_verdicts": ("contradicted",),
                    "selector": "all",
                }
            ),
        }
    )
    direct = authoring("wi-42", "ready", with_claim_type=False)
    direct = direct.model_copy(
        update={
            "statement": direct.statement.model_copy(
                update={"claim_type_digest": claim_type_digest(claim_type).tagged}
            ),
            "claim_type_artifact": claim_type,
        }
    )
    proposed = service_propose_playbill_claim(
        instance,
        authoring=direct,
        actor_id="owner",
        proposal_name="projection-overturned-claim",
        timestamp=TIMESTAMP,
    )
    activate(instance, owner, proposed)
    overturned = _claim_from_view(service_list_playbill_claims(instance).claims[0])
    backing = ProjectionClaimBackingV1(
        identity=overturned.identity,
        statement_digest=proposed.statement_digest,
    )

    (row,) = _projection_rows(instance, _request(instance, backing=(backing,)))
    assert row.reason == "projection_backing_stale"
    assert row.related_identities == (backing.identity.qualified,)
    assert row.detail["overturned_backings"] == [backing.identity.qualified]
    assert row.repair.required_change == "depublish_overturned_backing_block"


def test_query_backing_stales_only_when_its_semantic_result_changes(
    accepted_world: PlaybillInstance,
) -> None:
    (result,) = _projection_rows(
        accepted_world,
        _request(accepted_world, backing=(_query_backing(accepted_world, stale=True),)),
    )

    assert result.reason == "projection_backing_stale"
    assert result.related_identities == (f"QueryDefinition:{QUERY_NAME}",)


def test_query_backing_replays_actual_resolved_parameter_values(tmp_path: Path) -> None:
    instance, owner = seed_claims(tmp_path)
    query = work_item_query("project.one_item").model_copy(
        update={
            "entry": QueryEntryV1(
                binding="item",
                subject_kinds=(SUBJECT_KIND,),
                subject_id=QueryParameterRefV1(parameter="subject"),
            ),
            "parameters": (QueryParameterDeclarationV1(name="subject", value_type="string"),),
        }
    )
    proposal = submit_query_definition_candidate(
        instance,
        query=query,
        actor_id="owner",
        proposal_name="parameterized-projection-query",
        timestamp=TIMESTAMP,
    )
    accept_proposal(instance, owner, proposal)
    parameters = (
        ProjectionResolvedParameterBindingV1(name="subject", value_type="string", value="wi-42"),
    )
    backing = _query_backing(instance, name=query.identity.name, parameters=parameters)

    assert _projection_rows(instance, _request(instance, backing=(backing,))) == ()

    mismatched = backing.model_copy(
        update={
            "resolved_parameter_bindings": (
                ProjectionResolvedParameterBindingV1(name="subject", value_type="string", value=17),
            ),
        }
    )
    invalid_bindings = mismatched.resolved_parameter_bindings
    mismatched = mismatched.model_copy(
        update={"canonical_param_digest": projection_parameter_digest(invalid_bindings)}
    )
    assert _projection_rows(instance, _request(instance, backing=(mismatched,), dirty=True)) == ()


def test_query_backing_reacts_to_real_time_dependent_visibility(tmp_path: Path) -> None:
    instance, owner = _instance_with_query(tmp_path)
    later = NOW + timedelta(hours=2)
    future_authoring = authoring("wi-99", "ready", with_claim_type=False)
    future_authoring = future_authoring.model_copy(
        update={
            "statement": future_authoring.statement.model_copy(update={"effective_from": later})
        }
    )
    proposal = service_propose_playbill_claim(
        instance,
        authoring=future_authoring,
        actor_id="owner",
        proposal_name="future-projection-claim",
        timestamp=TIMESTAMP,
    )
    activate(instance, owner, proposal)
    backing = _query_backing(instance, at=NOW)

    assert _projection_rows(instance, _request(instance, backing=(backing,))) == ()
    (changed,) = _projection_rows(
        instance,
        _request(instance, backing=(backing,), evaluation_time=later),
    )
    assert changed.reason == "projection_backing_stale"


def test_claim_backing_statement_digest_ignores_artifact_only_revision(
    tmp_path: Path,
) -> None:
    instance, owner = _instance_with_query(tmp_path)
    claim = _claim_from_view(service_list_playbill_claims(instance).claims[0])
    backing = _claim_backing(instance)
    assert backing.identity == claim.identity
    original_artifact_digest = claim_artifact_digest(claim).tagged
    original_request = _request(instance, backing=(backing,))
    assert _projection_rows(instance, original_request) == ()

    successor = service_propose_playbill_claim(
        instance,
        authoring=DirectClaimAuthoringV1(
            statement=claim.statement,
            rationale="Add independent backing without revising the accepted statement.",
            claim_id=claim.identity.name,
            predecessor_artifact_digest=original_artifact_digest,
            existing_statement_handoffs=(
                ExistingStatementHandoffV1(
                    statement_digest=backing.statement_digest,
                    disposition="support",
                ),
            ),
        ),
        actor_id="owner",
        proposal_name="projection-backing-only-successor",
        timestamp="2026-08-16T20:02:00.000000Z",
    )
    assert successor.statement_digest == backing.statement_digest
    assert successor.artifact_digest != original_artifact_digest
    activate(instance, owner, successor)

    accepted_successor = next(
        item
        for item in (
            _claim_from_view(view) for view in service_list_playbill_claims(instance).claims
        )
        if item.identity == claim.identity
    )
    assert accepted_successor.lifecycle.predecessor_digest == original_artifact_digest
    assert claim_artifact_digest(accepted_successor).tagged == successor.artifact_digest
    assert _claim_backing(instance).statement_digest == backing.statement_digest

    at_successor = original_request.model_copy(
        update={"at": AcceptedCoordinate.from_internal(instance.accepted_coordinate())}
    )
    assert _projection_rows(instance, at_successor) == ()
