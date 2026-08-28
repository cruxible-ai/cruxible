"""Shared-Capture retirement consequences through real authoring and next surfaces."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cruxible_client.authoring.workspace import _coverage_v3_fields
from cruxible_client.contracts.claims import claim_path
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    document_digest,
)
from cruxible_core.playbill.citation_relations import (
    RELATION_RETIRED_CONFLICT_SCHEMA,
    RELATION_USE_SCHEMA,
    build_citation_relation_facts,
)
from cruxible_core.playbill.claim_retirement import service_retire_claim
from cruxible_core.playbill.coverage.adapter import observe_working_source
from cruxible_core.playbill.coverage.contracts import (
    CoverageAccessProfileV1,
    CoverageCommitmentScanProofV1,
    LogicalSourceIdentityV1,
    PlaybillCitationWindowObservationV1,
)
from cruxible_core.playbill.coverage.indexes import WorkingOccurrenceV1
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_propose_playbill_document,
    service_submit_playbill_approval,
)
from cruxible_core.service.playbill_coverage import service_resolve_playbill_coverage
from cruxible_core.service.playbill_next import (
    PlaybillNextRequestV1,
    PlaybillNextSourceObservationV4,
    PlaybillNextWorkspaceObservationV1,
    post_retirement_examined_support_suppresses_claim_cites_retired,
    service_playbill_next,
)
from tests.test_playbill._support import client_material
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_authoring_existing_capture import shared_capture_world
from tests.test_playbill.test_claim_retirement import (
    _activate as _activate_retirement,
)
from tests.test_playbill.test_claim_retirement import (
    _request as _retirement_request,
)
from tests.test_playbill.test_retirement_citing_advisory import copied_from_world
from tests.test_playbill.test_reverse_drift_next import _published_world, _retire

EVALUATION_TIME = datetime(2026, 8, 24, 18, tzinfo=UTC)


def _access() -> CoverageAccessProfileV1:
    return CoverageAccessProfileV1(
        profile_id="citation-retirement-test",
        permitted_access_classes=("instance", "public"),
    )


def _retire_claim(instance, owner, claim_id: str) -> None:  # type: ignore[no-untyped-def]
    result = service_retire_claim(
        instance,
        claim_id=claim_id,
        request=_retirement_request(instance, mode="submit"),
        actor=AuthenticatedActor(actor_id="owner"),
    )
    _activate_retirement(instance, owner, result)
    instance.refresh()


def claim_cites_retired_world(root: Path):  # type: ignore[no-untyped-def]
    instance, owner, _actor, first, second, *_rest = shared_capture_world(root)
    _retire_claim(instance, owner, first)
    return instance, owner, second


def _next(instance, workspace=None):  # type: ignore[no-untyped-def]
    return service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            evaluation_time=EVALUATION_TIME,
            access_profile=_access(),
            workspace_observation=workspace,
        ),
    )


def test_shared_capture_emits_one_claim_cites_retired_row_and_retirement_clears_it(
    tmp_path: Path,
) -> None:
    instance, owner, live_claim_id = claim_cites_retired_world(tmp_path)

    rows = tuple(item for item in _next(instance).items if item.reason == "claim_cites_retired")

    assert len(rows) == 1
    (row,) = rows
    assert row.subject_identity == f"Claim:{live_claim_id}"
    assert row.detail["relation_kind"] == "capture"
    assert row.repair.operation == "playbill.claim.retire"
    assert row.repair.required_change == "retire_or_replace_claim_citing_retired_evidence"

    _retire_claim(instance, owner, live_claim_id)
    assert not [item for item in _next(instance).items if item.reason == "claim_cites_retired"]


def test_future_examined_support_seam_is_named_and_always_false_today() -> None:
    assert not post_retirement_examined_support_suppresses_claim_cites_retired("sha256:" + "1" * 64)


def test_relation_delta_reopens_only_the_changed_claim_captures(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    instance, owner, _actor, first, _second, *_rest = shared_capture_world(tmp_path)
    before = instance.accepted_coordinate()
    with instance.bind_accepted_projection(before) as projection:
        previous_uses = projection.semantic_facts(RELATION_USE_SCHEMA)
        previous_conflicts = projection.semantic_facts(RELATION_RETIRED_CONFLICT_SCHEMA)

    _retire_claim(instance, owner, first)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    full = build_citation_relation_facts(tree, bodies=instance.body_store())

    from cruxible_core.playbill import citation_relations

    original_parse = citation_relations.parse_capture_envelope
    parse_calls = 0

    def counted_parse(content: bytes):  # type: ignore[no-untyped-def]
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(content)

    monkeypatch.setattr(citation_relations, "parse_capture_envelope", counted_parse)
    incremental = build_citation_relation_facts(
        tree,
        bodies=instance.body_store(),
        previous_use_facts=previous_uses,
        previous_conflict_facts=previous_conflicts,
        changed_claim_paths=frozenset((claim_path(first),)),
    )

    def key(fact):  # type: ignore[no-untyped-def]
        return fact.schema_id, fact.subject_identity, fact.fact_key

    assert sorted(incremental, key=key) == sorted(full, key=key)
    assert parse_calls == 1


def _accept_document(
    instance,  # type: ignore[no-untyped-def]
    *,
    shell: DocumentShell,
    proposal_name: str,
    timestamp: str,
) -> None:
    proposal = service_propose_playbill_document(
        instance,
        shell=shell,
        actor_id="owner",
        proposal_name=proposal_name,
        timestamp=timestamp,
    )
    candidate = proposal.proposal.candidate
    assert candidate is not None
    approval = _sign(
        client_material(instance.root.parent, instance),
        candidate.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=proposal.proposal.admission.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    assert (
        service_activate_playbill_proposal(
            instance,
            proposal_id=proposal.proposal.admission.proposal_id,
            activated_by="owner",
        ).status
        == "accepted"
    )
    instance.refresh()


def _workspace_observation(
    instance,  # type: ignore[no-untyped-def]
    *,
    content: bytes,
    expect_no_claim_cards: bool = False,
) -> PlaybillNextWorkspaceObservationV1:
    source_id = "repo.work-items"
    source = LogicalSourceIdentityV1(plane="external", identity=source_id)
    coverage = service_resolve_playbill_coverage(
        instance,
        instance_id=instance.descriptor.instance_id,
        observations=(observe_working_source(source, content),),
    )
    assert len(coverage.spans) == 1
    if expect_no_claim_cards:
        assert all(not card.citation_associations for card in coverage.spans[0].cards), (
            "retired citations may supply windows but never retired Claim cards"
        )
    occurrences, proofs, windows, notes = _coverage_v3_fields(
        coverage.spans[0].model_dump(mode="json"),
        source_id=source_id,
        content=content,
    )
    return PlaybillNextWorkspaceObservationV1(
        source_observations=(
            PlaybillNextSourceObservationV4(
                source_id=source_id,
                document_id="work-items",
                observed_source_digest=observe_working_source(source, content).content_digest,
                byte_length=len(content),
                marker_summaries=(),
                occurrences=tuple(WorkingOccurrenceV1.model_validate(item) for item in occurrences),
                commitment_scan_proofs=tuple(
                    CoverageCommitmentScanProofV1.model_validate(item) for item in proofs
                ),
                citation_window_observations=tuple(
                    PlaybillCitationWindowObservationV1.model_validate(item) for item in windows
                ),
                scan_notes=notes,
                marker_notes=(),
            ),
        )
    )


def retired_source_world(root: Path):  # type: ignore[no-untyped-def]
    instance, owner, claim_id = _published_world(root)
    content = b"ready"
    body = instance.store_document_body(content)
    document = DocumentShell(
        identity="document:work-items",
        document_kind="work-items",
        title="Work items",
        media_type="text/markdown",
        body_digest=body.digest,
        authority=DocumentAuthority(required_tier="governed_write"),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    _accept_document(
        instance,
        shell=document,
        proposal_name="add-work-items-document",
        timestamp="2026-08-21T12:00:02.000000Z",
    )
    _retire(instance, owner, claim_id)
    instance.refresh()
    return instance, document, content


def test_retired_source_window_is_served_without_retired_claim_cards_and_repair_clears(
    tmp_path: Path,
) -> None:
    instance, document, content = retired_source_world(tmp_path)
    observation = _workspace_observation(
        instance,
        content=content,
        expect_no_claim_cards=True,
    )

    rows = tuple(
        item
        for item in _next(instance, observation).items
        if item.reason == "retired_claim_source_stale"
    )
    assert len(rows) == 1
    (row,) = rows
    assert row.subject_identity == "document:work-items"
    assert row.repair.operation == "playbill.document.propose"
    assert row.repair.required_change == "revise_retired_claim_source_span"
    assert not [
        item
        for item in _next(instance, observation).items
        if item.reason == "self_published_source_stale"
    ]

    replacement = b"status: replaced"
    replacement_body = instance.store_document_body(replacement)
    successor = document.model_copy(
        update={
            "body_digest": replacement_body.digest,
            "predecessor_digest": document_digest(document).tagged,
            "lifecycle": DocumentLifecycle(revision=2),
        }
    )
    _accept_document(
        instance,
        shell=successor,
        proposal_name="replace-retired-passage",
        timestamp="2026-08-21T12:02:00.000000Z",
    )
    replacement_observation = _workspace_observation(instance, content=replacement)
    assert not [
        item
        for item in _next(instance, replacement_observation).items
        if item.reason == "retired_claim_source_stale"
    ]


def test_ambiguous_relocation_is_silent_instead_of_guessing(tmp_path: Path) -> None:
    instance, _document, _content = retired_source_world(tmp_path)
    ambiguous = _workspace_observation(instance, content=b"ready ready")

    assert not [
        item
        for item in _next(instance, ambiguous).items
        if item.reason == "retired_claim_source_stale"
    ]


def test_live_copy_association_suppresses_retired_source_staleness(tmp_path: Path) -> None:
    instance, owner, _coordinator, _actor = copied_from_world(tmp_path)
    content = b"status: ready"
    body = instance.store_document_body(content)
    document = DocumentShell(
        identity="document:work-items",
        document_kind="work-items",
        title="Work items",
        media_type="text/markdown",
        body_digest=body.digest,
        authority=DocumentAuthority(required_tier="governed_write"),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    _accept_document(
        instance,
        shell=document,
        proposal_name="add-covered-work-items-document",
        timestamp="2026-08-21T12:03:00.000000Z",
    )

    rows = _next(instance, _workspace_observation(instance, content=content)).items

    assert not [item for item in rows if item.reason == "retired_claim_source_stale"]
    assert [item for item in rows if item.reason == "claim_cites_retired"]
