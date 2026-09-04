"""PC-G5 deterministic Playbill next queue laws."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import get_args

import pytest

from cruxible_client import contracts
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.captures import (
    FOREIGN_SOURCE_COORDINATE_TYPE,
    FOREIGN_SOURCE_SELECTOR_TYPE,
    CaptureEnvelopeV1,
    parse_capture_envelope,
)
from cruxible_client.contracts.claims import (
    claim_artifact_digest,
    claim_citation_references,
    claim_path,
    claim_statement_digest,
)
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    render_document,
)
from cruxible_client.contracts.source_references import ExternalSourceReferenceV1
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.playbill_claims import (
    _claim_from_view,
    _claim_law_evidence,
    service_list_playbill_claims,
)
from cruxible_core.service.playbill_next import (
    NextReason,
    PlaybillNextAccessProfileInvalid,
    PlaybillNextDriftObservationV1,
    PlaybillNextRequestV1,
    PlaybillNextSourceObservationV3,
    PlaybillNextWorkspaceObservationInvalid,
    PlaybillNextWorkspaceObservationV1,
    _citation_commitments,
    _qualifier_discriminator,
    service_playbill_next,
    validate_playbill_next_request,
)
from cruxible_core.service.playbill_query import build_accepted_query_facts
from tests.test_playbill._adoption_fixture import _Builder
from tests.test_playbill._claim_authoring_support import (
    ExistingStatementHandoffV1,
    service_propose_playbill_claim,
)
from tests.test_playbill._knowledge_loop_support import (
    activate as activate_work_item_claim,
)
from tests.test_playbill._knowledge_loop_support import (
    authoring as work_item_authoring,
)
from tests.test_playbill._knowledge_loop_support import (
    seed_claims,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_claim_query_engine import status_claim

EVALUATION_TIME = datetime(2026, 8, 24, 18, tzinfo=UTC)


def _access() -> CoverageAccessProfileV1:
    return CoverageAccessProfileV1(
        profile_id="next-test",
        permitted_access_classes=("instance", "public"),
    )


def test_next_is_deterministic_and_excludes_the_removed_brief_reason(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    request = PlaybillNextRequestV1(
        evaluation_time=EVALUATION_TIME,
        access_profile=_access(),
    )

    first = service_playbill_next(instance, request=request)
    retry = service_playbill_next(instance, request=request)

    assert retry == first
    assert first.observed_domains == ("accepted_state",)
    assert first.unobserved_domains == (
        "workspace_floor",
        "workspace_sources",
        "workspace_projections",
    )
    assert "brief_unhealthy" not in get_args(NextReason)
    assert all(item.reason != "brief_unhealthy" for item in first.items)
    assert first.result_digest.startswith("sha256:")


def test_next_reason_uses_the_exact_public_closed_vocabulary() -> None:
    assert set(get_args(NextReason)) == set(get_args(contracts.PlaybillNextReason))


def test_provider_lane_degradation_is_a_typed_advisory_with_hand_edit_repair(
    tmp_path: Path,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    result = service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            evaluation_time=EVALUATION_TIME,
            access_profile=_access(),
        ),
        provider_lane=contracts.ProviderLaneStatusV1(
            state="unavailable",
            code="provider_process_lease_invalid",
            detail="control socket path is too long",
        ),
    )

    row = next(item for item in result.items if item.reason == "provider_lane_unavailable")
    assert row.severity == "warning"
    assert row.detail == {
        "code": "provider_process_lease_invalid",
        "detail": "control socket path is too long",
    }
    assert row.repair.operation == "hand_edit"
    assert row.repair.target == "daemon/provider-runtime.json"
    assert row.repair.command is None


def test_workspace_drift_is_verified_against_the_accepted_citation(
    tmp_path: Path,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    listed = service_list_playbill_claims(instance)
    claim = _claim_from_view(listed.claims[0])
    citation = claim_citation_references(claim)[0]
    envelope = parse_capture_envelope(
        instance.body_store().read(
            citation.capture_digest,
            access=BodyAccessContext(principal_id="next-test", can_read_body=True),
        )
    )
    observed = typed_digest(
        Sha256Value,
        "playbill-next-test-observed-v1",
        {"citation_id": citation.citation_id},
    ).tagged
    request = PlaybillNextRequestV1(
        at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        evaluation_time=EVALUATION_TIME,
        access_profile=_access(),
        workspace_observation=PlaybillNextWorkspaceObservationV1(
            floor_status="missing",
            drift_observations=(
                PlaybillNextDriftObservationV1(
                    citation_id=citation.citation_id,
                    expected_commitment_digest=envelope.commitment.digest,
                    observed_commitment_digest=observed,
                ),
            ),
        ),
    )

    result = service_playbill_next(instance, request=request)

    assert result.observed_domains == (
        "accepted_state",
        "workspace_floor",
        "workspace_sources",
    )
    assert result.unobserved_domains == ("workspace_projections",)
    assert {item.reason for item in result.items}.issuperset({"citation_drifted", "floor_missing"})
    drift = next(item for item in result.items if item.reason == "citation_drifted")
    assert drift.related_identities == (citation.citation_id,)
    assert drift.repair.operation == "playbill.authoring.bind"

    workspace = request.workspace_observation
    assert workspace is not None
    drifts = workspace.drift_observations
    assert drifts is not None
    substituted = request.model_copy(
        update={
            "workspace_observation": workspace.model_copy(
                update={
                    "drift_observations": (
                        drifts[0].model_copy(
                            update={
                                "expected_commitment_digest": typed_digest(
                                    Sha256Value,
                                    "playbill-next-test-substitution-v1",
                                    {},
                                ).tagged
                            }
                        ),
                    )
                }
            )
        }
    )
    with pytest.raises(PlaybillNextWorkspaceObservationInvalid):
        service_playbill_next(instance, request=substituted)


def _accept_claim_successor(instance, owner, *, value: str, sequence: int):  # type: ignore[no-untyped-def]
    current = next(
        claim
        for claim in (
            _claim_from_view(view) for view in service_list_playbill_claims(instance).claims
        )
        if claim.statement.subject.artifact_path.endswith("/wi-42.json")
    )
    proposed = service_propose_playbill_claim(
        instance,
        authoring=work_item_authoring("wi-42", value, with_claim_type=False).model_copy(
            update={
                "claim_id": current.identity.name,
                "predecessor_artifact_digest": claim_artifact_digest(current).tagged,
            }
        ),
        actor_id="owner",
        proposal_name=f"next-successor-{sequence}",
        timestamp=f"2026-08-24T17:00:{sequence:02d}.000000Z",
    )
    activate_work_item_claim(instance, owner, proposed)
    return current


@pytest.mark.parametrize(
    ("synthetic_predecessor_count", "expected_note"),
    [
        (257, "predecessor_lineage_limit_exceeded"),
        (0, "predecessor_unresolved"),
    ],
)
def test_unresolved_citation_predecessor_degrades_to_a_row_with_a_typed_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_predecessor_count: int,
    expected_note: str,
) -> None:
    instance, owner = seed_claims(tmp_path)
    predecessor = _accept_claim_successor(instance, owner, value="blocked", sequence=3)
    old_citation = claim_citation_references(predecessor)[0]
    envelope = parse_capture_envelope(
        instance.body_store().read(
            old_citation.capture_digest,
            access=BodyAccessContext(principal_id="next-test", can_read_body=True),
        )
    )
    observed = typed_digest(
        Sha256Value,
        "playbill-next-cap-degraded-observation-v1",
        {},
    ).tagged
    internal_coordinate = instance.accepted_coordinate()
    query_facts = build_accepted_query_facts(
        instance,
        coordinate=internal_coordinate,
    )
    law_evidence = {
        claim_path(claim.identity.name): _claim_law_evidence(
            instance,
            path=claim_path(claim.identity.name),
            at=internal_coordinate,
        )
        for claim in (
            _claim_from_view(view) for view in service_list_playbill_claims(instance).claims
        )
    }
    current = instance.accepted_history()[-1]
    synthetic = tuple(
        SimpleNamespace(oid=f"{index + 1:064x}") for index in range(synthetic_predecessor_count)
    )
    original_tree_at = instance.tree_at
    synthetic_oids = {item.oid for item in synthetic}
    monkeypatch.setattr(instance, "accepted_history", lambda: (*synthetic, current))
    original_blob_at = instance.blob_at
    monkeypatch.setattr(
        instance,
        "tree_at",
        lambda oid: {} if oid in synthetic_oids else original_tree_at(oid),
    )
    original_blobs_at = instance.blobs_at
    monkeypatch.setattr(
        instance,
        "blob_at",
        lambda oid, path: None if oid in synthetic_oids else original_blob_at(oid, path),
    )
    monkeypatch.setattr(
        instance,
        "blobs_at",
        lambda oid, paths: {} if oid in synthetic_oids else original_blobs_at(oid, paths),
    )
    monkeypatch.setattr(
        "cruxible_core.service.playbill_next._claim_law_evidence",
        lambda _instance, *, path, at: law_evidence[path],
    )
    monkeypatch.setattr(
        "cruxible_core.service.playbill_next.build_accepted_query_facts",
        lambda _instance, *, coordinate, **_kwargs: query_facts,
    )
    monkeypatch.setattr("cruxible_core.service.playbill_next._claim_items", lambda *a, **k: ())

    result = service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            evaluation_time=EVALUATION_TIME,
            access_profile=CoverageAccessProfileV1(
                profile_id="public-next-test",
                permitted_access_classes=("instance", "public"),
            ),
            workspace_observation=PlaybillNextWorkspaceObservationV1(
                drift_observations=(
                    PlaybillNextDriftObservationV1(
                        citation_id=old_citation.citation_id,
                        expected_commitment_digest=envelope.commitment.digest,
                        observed_commitment_digest=observed,
                    ),
                )
            ),
        ),
    )

    row = next(item for item in result.items if item.reason == "citation_drifted")
    assert row.detail["lineage_note"] == expected_note  # type: ignore[index]


def test_resolvable_citation_predecessor_still_subtracts_dead_spans(tmp_path: Path) -> None:
    instance, owner = seed_claims(tmp_path)
    predecessor = _accept_claim_successor(instance, owner, value="blocked", sequence=3)
    old_citation = claim_citation_references(predecessor)[0]

    commitments = _citation_commitments(
        instance,
        coordinate=PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        evaluation_time=EVALUATION_TIME,
    )

    assert old_citation.citation_id not in commitments


def test_backing_only_successor_keeps_edited_predecessor_span_in_drift_queue(
    tmp_path: Path,
) -> None:
    instance, owner = seed_claims(tmp_path)
    predecessor = _accept_claim_successor(instance, owner, value="ready", sequence=3)
    old_citation = claim_citation_references(predecessor)[0]
    envelope = parse_capture_envelope(
        instance.body_store().read(
            old_citation.capture_digest,
            access=BodyAccessContext(principal_id="next-test", can_read_body=True),
        )
    )
    observed = typed_digest(
        Sha256Value,
        "playbill-next-backing-only-edited-span-v1",
        {},
    ).tagged

    result = service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            evaluation_time=EVALUATION_TIME,
            access_profile=_access(),
            workspace_observation=PlaybillNextWorkspaceObservationV1(
                drift_observations=(
                    PlaybillNextDriftObservationV1(
                        citation_id=old_citation.citation_id,
                        expected_commitment_digest=envelope.commitment.digest,
                        observed_commitment_digest=observed,
                    ),
                )
            ),
        ),
    )

    row = next(item for item in result.items if item.reason == "citation_drifted")
    assert row.related_identities == (old_citation.citation_id,)
    assert row.detail["expected_commitment_digest"] == envelope.commitment.digest  # type: ignore[index]
    assert row.detail["observed_window_digest"] == observed  # type: ignore[index]
    assert row.detail["drift_state"] == "changed"  # type: ignore[index]
    assert "lineage_note" not in row.detail  # type: ignore[operator]


def test_unknown_access_profile_value_has_the_frozen_refusal() -> None:
    with pytest.raises(PlaybillNextAccessProfileInvalid) as raised:
        validate_playbill_next_request(
            {
                "evaluation_time": EVALUATION_TIME,
                "access_profile": {
                    "tag": "playbill-coverage-access-profile-v1",
                    "profile_id": "next-test",
                    "permitted_access_classes": ["secret"],
                    "disclose_restricted_existence": False,
                },
            }
        )

    assert raised.value.code == "playbill.next.access_profile_invalid"


def test_document_modified_names_a_reproposal_that_clears_the_row(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    body = instance.store_document_body(b"accepted body\n")
    document = DocumentShell(
        identity="document:runbook",
        document_kind="runbook",
        title="Runbook",
        media_type="text/markdown",
        body_digest=body.digest,
        authority=DocumentAuthority(required_tier="governed_write"),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    builder = _Builder(instance, owner)
    builder.accept(
        {"documents/runbook.json": render_document(document)},
        phase="document-next",
    )
    current = instance.__class__.open(instance.root, trust_root=instance.trust_root)
    changed = typed_digest(Sha256Value, "playbill-next-modified-document-v1", {}).tagged

    def queued(  # type: ignore[no-untyped-def]
        observed_digest: str,
        access_profile: CoverageAccessProfileV1 = _access(),
    ):
        return service_playbill_next(
            current,
            request=PlaybillNextRequestV1(
                evaluation_time=EVALUATION_TIME,
                access_profile=access_profile,
                workspace_observation=PlaybillNextWorkspaceObservationV1(
                    source_observations=(
                        PlaybillNextSourceObservationV3(
                            tag="playbill-next-source-observation-v3",
                            source_id="corpus.runbook",
                            document_id="runbook",
                            observed_source_digest=observed_digest,
                            byte_length=0,
                            marker_summaries=(),
                            occurrences=(),
                            scanned_commitment_digests=(),
                            scan_complete=True,
                            scan_notes=(),
                            marker_notes=(),
                        ),
                    )
                ),
            ),
        )

    row = next(item for item in queued(changed).items if item.reason == "document_modified")
    assert row.severity == "warning"
    assert row.repair.operation == "playbill.document.propose"
    assert row.repair.required_change == "repropose_modified_document"
    assert all(item.reason != "document_modified" for item in queued(body.digest).items)
    public_only = CoverageAccessProfileV1(
        profile_id="public-only",
        permitted_access_classes=("public",),
    )
    assert all(item.reason != "document_modified" for item in queued(changed, public_only).items)


@pytest.mark.parametrize(
    "malformed_coordinate",
    [
        {},
        {"source_content_digest": 12},
        {"source_content_digest": "not-a-digest"},
    ],
)
def test_malformed_capture_snapshot_never_hides_another_citations_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    malformed_coordinate: dict[str, object],
) -> None:
    instance, _owner = seed_claims(tmp_path)
    captured_digest = typed_digest(Sha256Value, "playbill-next-captured-source-v1", {}).tagged
    observed_digest = typed_digest(Sha256Value, "playbill-next-observed-source-v1", {}).tagged
    calls = 0

    def synthetic_capture(content: bytes) -> CaptureEnvelopeV1:
        nonlocal calls
        envelope = parse_capture_envelope(content)
        calls += 1
        source = ExternalSourceReferenceV1(
            source_identity="corpus.malformed" if calls == 1 else "corpus.healthy",
            producer_binding_digest=envelope.commitment.digest,
            coordinate_type=FOREIGN_SOURCE_COORDINATE_TYPE,
            coordinate=(
                malformed_coordinate if calls == 1 else {"source_content_digest": captured_digest}
            ),
            selector_type=FOREIGN_SOURCE_SELECTOR_TYPE,
            selector={},
            replayability="attested_only",
        )
        return envelope.model_copy(update={"source": source})

    monkeypatch.setattr(
        "cruxible_core.service.playbill_next.parse_capture_envelope", synthetic_capture
    )
    commitments = _citation_commitments(
        instance,
        coordinate=PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        evaluation_time=EVALUATION_TIME,
    )
    healthy = next(item for item in commitments.values() if item.source_id == "corpus.healthy")
    calls = 0

    result = service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            evaluation_time=EVALUATION_TIME,
            access_profile=_access(),
            workspace_observation=PlaybillNextWorkspaceObservationV1(
                source_observations=(
                    PlaybillNextSourceObservationV3(
                        tag="playbill-next-source-observation-v3",
                        source_id="corpus.healthy",
                        observed_source_digest=observed_digest,
                        byte_length=healthy.byte_length,
                        marker_summaries=(),
                        occurrences=(),
                        scanned_commitment_digests=(healthy.commitment_digest,),
                        scan_complete=True,
                        scan_notes=(),
                        marker_notes=(),
                    ),
                )
            ),
        ),
    )

    assert "workspace_sources" in result.observed_domains
    drift = next(item for item in result.items if item.reason == "citation_drifted")
    assert drift.detail["logical_source"]["identity"] == "corpus.healthy"  # type: ignore[index]


def test_conflict_repair_names_qualifier_separation_not_dispositions(tmp_path: Path) -> None:
    instance, owner = seed_claims(tmp_path)
    listed = service_list_playbill_claims(instance)
    current = next(
        claim
        for claim in (_claim_from_view(view) for view in listed.claims)
        if claim.statement.subject.artifact_path.endswith("/wi-42.json")
    )
    second = service_propose_playbill_claim(
        instance,
        authoring=work_item_authoring("wi-42", "blocked", with_claim_type=False).model_copy(
            update={
                "existing_statement_handoffs": (
                    ExistingStatementHandoffV1(
                        statement_digest=claim_statement_digest(current.statement).tagged,
                        disposition="contradict",
                    ),
                )
            }
        ),
        actor_id="owner",
        proposal_name="conflicting-work-item",
        timestamp="2026-08-24T17:00:03.000000Z",
    )
    activate_work_item_claim(instance, owner, second)

    result = service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            evaluation_time=EVALUATION_TIME,
            access_profile=_access(),
        ),
    )

    conflict = next(item for item in result.items if item.reason == "claim_conflicted")
    assert conflict.repair.required_change == "revise_claims_into_distinct_qualifiers"
    assert conflict.repair.arguments == {"claim_ids": list(conflict.related_identities)}


def test_conflict_repair_names_the_first_byte_ordered_disjoint_value_discriminator() -> None:
    claims = [
        status_claim(1, "wi-1", {"topic": "paging", "rule": "page"}).accepted.claim,
        status_claim(2, "wi-1", {"topic": "change_lanes", "rule": "approve"}).accepted.claim,
    ]

    assert _qualifier_discriminator(claims) == "rule"
