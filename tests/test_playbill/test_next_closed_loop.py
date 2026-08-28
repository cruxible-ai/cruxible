"""Every Playbill next row names a repair that can close that row."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import pytest

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.captures import (
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    CanonicalDurationV1,
    DirectForeignSourceSelectionV1,
    capture_contract_digest,
    capture_contract_path,
    foreign_source_capture_contract,
    parse_capture_envelope,
    render_capture_contract,
)
from cruxible_client.contracts.claim_types import (
    ClaimEvidenceFreshnessV1,
    ClaimFreshnessDurationV1,
    ClaimType,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.claims import (
    ClaimRetireRequestV1,
    LiteralClaimObject,
    claim_artifact_digest,
    claim_citation_references,
    claim_path,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    document_digest,
    render_document,
)
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
)
from cruxible_client.contracts.semantic import ContentSpan
from cruxible_client.contracts.source_references import ExternalSourceReferenceV1
from cruxible_client.contracts.subjects import render_subject, subject_path
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.claim_retirement import ClaimRetireResultV1, service_retire_claim
from cruxible_core.playbill.claim_type_migrations import (
    ClaimTypeDependentDispositionV1,
    ClaimTypeMigrationRequestV1,
    service_migrate_claim_type,
)
from cruxible_core.playbill.coverage.contracts import (
    CoverageAccessProfileV1,
    CoverageCommitmentScanProofV1,
    CoverageLineOverlayV1,
    LogicalSourceIdentityV1,
    PlaybillCitationWindowObservationV1,
    occurrence_identity_digest,
)
from cruxible_core.playbill.coverage.indexes import WorkingOccurrenceV1
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_propose_playbill_document,
    service_submit_playbill_approval,
)
from cruxible_core.playbill.settlement import ChangeActorBinding
from cruxible_core.service.playbill_claims import (
    _claim_from_view,
    service_list_playbill_claims,
)
from cruxible_core.service.playbill_next import (
    NextReason,
    PlaybillNextDriftObservationV1,
    PlaybillNextRequestV1,
    PlaybillNextSourceObservationV3,
    PlaybillNextSourceObservationV4,
    PlaybillNextWorkspaceObservationV1,
    service_playbill_next,
)
from tests.test_playbill._adoption_fixture import _Builder
from tests.test_playbill._claim_authoring_support import (
    DirectClaimAuthoringV1,
    ExistingStatementHandoffV1,
    _activate_direct_claim,
    service_propose_playbill_claim,
)
from tests.test_playbill._knowledge_loop_support import (
    activate,
    authoring,
    seed_claims,
    subject_shell,
)
from tests.test_playbill._support import client_material, initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_authoring_preflight import _seed_claim_surface
from tests.test_playbill.test_claims import _claim_type
from tests.test_playbill.test_dependency_impact import (
    DERIVED_INDEX,
    SOURCE_PATH,
    _derived,
    _source_v1,
    _source_v2,
)
from tests.test_playbill.test_dependency_impact import (
    _claim as _dependency_claim,
)
from tests.test_playbill.test_dependency_impact import (
    _facts as _dependency_facts,
)
from tests.test_playbill.test_evidence_freshness import _activate as _activate_migration
from tests.test_playbill.test_projection_next import (
    _claim_backing,
    _instance_with_query,
)
from tests.test_playbill.test_projection_next import (
    _request as _projection_request,
)
from tests.test_playbill.test_reverse_drift_next import (
    _publish_self_published_claim,
    _published_world,
    _retire,
)
from tests.test_playbill.test_reverse_drift_next import (
    _request as _published_request,
)

EVALUATION_TIME = datetime(2026, 8, 24, 18, tzinfo=UTC)
RepairCase = Callable[[Path, pytest.MonkeyPatch], None]
ClosedLoopKey = tuple[str, str | None]

EXPECTED_OPERATIONS = {
    "claim_conflicted": "playbill.authoring.create",
    "claim_uncovered": "playbill.authoring.bind",
    "claim_stale_evidence": "playbill.authoring.bind",
    "citation_drifted": "playbill.authoring.bind",
    "citation_source_unobserved": "playbill.authoring.bind",
    "evidence_expiring": "playbill.authoring.bind",
    "floor_missing": "playbill.floor.export",
    "floor_stale": "playbill.floor.export",
    "floor_invalid": "playbill.floor.export",
    "projection_dirty": "playbill.block.repin",
    "projection_backing_stale": "playbill.block.repin",
    "self_published_source_stale": "playbill.authoring.create",
    "claim_dependency_stale": "playbill.authoring.create",
    "claim_attestation_threshold_met": "playbill.authoring.create",
    "document_modified": "playbill.document.propose",
}


def _expected_operation(key: ClosedLoopKey) -> str:
    if key == ("citation_drifted", "gone"):
        return "playbill.claim.retire"
    return EXPECTED_OPERATIONS[key[0]]


def _access() -> CoverageAccessProfileV1:
    return CoverageAccessProfileV1(
        profile_id="next-closed-loop",
        permitted_access_classes=("instance", "public"),
    )


def _request(
    instance,  # type: ignore[no-untyped-def]
    *,
    evaluation_time: datetime = EVALUATION_TIME,
    expiring_within: int = 604_800_000_000,
    workspace: PlaybillNextWorkspaceObservationV1 | None = None,
) -> PlaybillNextRequestV1:
    return PlaybillNextRequestV1(
        at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        evaluation_time=evaluation_time,
        access_profile=_access(),
        expiring_within=CanonicalDurationV1(microseconds=expiring_within),
        workspace_observation=workspace,
    )


def _row(instance, reason: str, request: PlaybillNextRequestV1):  # type: ignore[no-untyped-def]
    return next(
        item
        for item in service_playbill_next(instance, request=request).items
        if item.reason == reason
    )


def _assert_gone(instance, reason: str, request: PlaybillNextRequestV1) -> None:  # type: ignore[no-untyped-def]
    assert all(
        item.reason != reason for item in service_playbill_next(instance, request=request).items
    )


def _item_key(item: object) -> ClosedLoopKey:
    reason = getattr(item, "reason")
    detail = getattr(item, "detail")
    discriminator = (
        detail.get("drift_state")
        if reason == "citation_drifted" and isinstance(detail, Mapping)
        else None
    )
    return reason, discriminator


def _row_by_key(
    instance,
    key: ClosedLoopKey,
    request: PlaybillNextRequestV1,  # type: ignore[no-untyped-def]
):  # type: ignore[no-untyped-def]
    return next(
        item
        for item in service_playbill_next(instance, request=request).items
        if _item_key(item) == key
    )


def _assert_key_gone(
    instance,
    key: ClosedLoopKey,
    request: PlaybillNextRequestV1,  # type: ignore[no-untyped-def]
) -> None:
    assert all(
        _item_key(item) != key for item in service_playbill_next(instance, request=request).items
    )


def _current_claim(instance, *, subject_id: str = "wi-42"):  # type: ignore[no-untyped-def]
    return next(
        claim
        for claim in (
            _claim_from_view(view) for view in service_list_playbill_claims(instance).claims
        )
        if claim.statement.subject.artifact_path.endswith(f"/{subject_id}.yaml")
    )


def _claim_conflicted(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    instance, owner = seed_claims(root)
    first = _current_claim(instance)
    conflicting = service_propose_playbill_claim(
        instance,
        authoring=authoring("wi-42", "blocked", with_claim_type=False).model_copy(
            update={
                "existing_statement_handoffs": (
                    ExistingStatementHandoffV1(
                        statement_digest=claim_statement_digest(first.statement).tagged,
                        disposition="contradict",
                    ),
                )
            }
        ),
        actor_id="owner",
        proposal_name="closed-loop-conflict",
        timestamp="2026-08-24T17:00:03.000000Z",
    )
    activate(instance, owner, conflicting)
    before = _request(instance)
    row = _row(instance, "claim_conflicted", before)
    assert row.repair.operation == EXPECTED_OPERATIONS["claim_conflicted"]

    contenders = tuple(
        claim
        for claim in (
            _claim_from_view(view) for view in service_list_playbill_claims(instance).claims
        )
        if claim.statement.subject.artifact_path.endswith("/wi-42.yaml")
    )
    revised = next(
        claim for claim in contenders if claim.identity.qualified == conflicting.claim_identity
    )
    other = next(claim for claim in contenders if claim.identity != revised.identity)
    repair = service_propose_playbill_claim(
        instance,
        authoring=DirectClaimAuthoringV1(
            statement=revised.statement.model_copy(update={"qualifier": "blocked-lane"}),
            rationale="Separate the conflicting meanings into explicit qualifier lanes.",
            claim_id=revised.identity.name,
            predecessor_artifact_digest=claim_artifact_digest(revised).tagged,
            existing_statement_handoffs=(
                ExistingStatementHandoffV1(
                    statement_digest=claim_statement_digest(other.statement).tagged,
                    disposition="not_tested",
                ),
            ),
        ),
        actor_id="owner",
        proposal_name="closed-loop-qualify-conflict",
        timestamp="2026-08-24T17:00:04.000000Z",
    )
    activate(instance, owner, repair)
    _assert_gone(instance, "claim_conflicted", _request(instance))


def _foreign_world(root: Path, *, bind: bool):  # type: ignore[no-untyped-def]
    instance, owner = initialize_local(root)
    source_id = "corpus.closed-loop"
    foreign_contract = foreign_source_capture_contract(source_id)
    direct_digest = capture_contract_digest(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT).tagged
    foreign_digest = capture_contract_digest(foreign_contract).tagged
    base_type = _claim_type()
    claim_type = base_type.model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV1(
                rules=(
                    ClaimEvidenceAdmissionRuleV1(
                        rule_id="direct-origin",
                        claim_roles=("normative", "observation"),
                        capture_contract_digests=(direct_digest,),
                        evidence_kinds=("self_asserted",),
                        admission="origin_only",
                        subject_binding="exact_claim_subject",
                    ),
                    ClaimEvidenceAdmissionRuleV1(
                        rule_id="foreign-direct",
                        claim_roles=("normative", "observation"),
                        capture_contract_digests=(foreign_digest,),
                        evidence_kinds=("self_asserted",),
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            )
        }
    )
    shell = subject_shell("wi-42")
    _Builder(instance, owner).accept(
        {
            subject_path(shell.subject_kind, shell.subject_id): render_subject(shell),
            claim_type_path(claim_type.predicate): render_claim_type(claim_type),
            capture_contract_path(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity.name): (
                render_capture_contract(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT)
            ),
            capture_contract_path(foreign_contract.identity.name): render_capture_contract(
                foreign_contract
            ),
        },
        phase="closed-loop-evidence-dependencies",
    )
    instance.refresh()
    proposed = service_propose_playbill_claim(
        instance,
        authoring=authoring("wi-42", "ready", with_claim_type=False).model_copy(
            update={
                "statement": authoring(
                    "wi-42", "ready", with_claim_type=False
                ).statement.model_copy(
                    update={"claim_type_digest": claim_type_digest(claim_type).tagged}
                )
            }
        ),
        actor_id="owner",
        proposal_name="closed-loop-origin-only",
        timestamp="2026-08-24T17:00:02.000000Z",
    )
    activate(instance, owner, proposed)
    source = b"status: ready\n"
    source_body = instance.body_store().store(source)
    if not bind:
        return instance, owner, proposed, source_id, source_body.digest
    current = _current_claim(instance)
    selected = DirectForeignSourceSelectionV1(
        logical_source_identity=source_id,
        span=ContentSpan(
            content_digest=source_body.digest,
            start_byte=8,
            end_byte=13,
        ),
        media_type="text/markdown",
    )
    successor = service_propose_playbill_claim(
        instance,
        authoring=DirectClaimAuthoringV1(
            statement=current.statement,
            rationale="Bind an admissible independent source span.",
            claim_id=current.identity.name,
            predecessor_artifact_digest=claim_artifact_digest(current).tagged,
            source_selection=selected,
        ),
        actor_id="owner",
        proposal_name="closed-loop-bind-evidence",
        timestamp="2026-08-24T17:00:03.000000Z",
    )
    activate(instance, owner, successor)
    return instance, owner, successor, source_id, source_body.digest


def _claim_uncovered(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    instance, owner, proposed, source_id, source_digest = _foreign_world(root, bind=False)
    before = _request(instance)
    row = _row(instance, "claim_uncovered", before)
    assert row.repair.operation == EXPECTED_OPERATIONS["claim_uncovered"]
    assert "evidence_admission_policy" in row.detail["policy_hint"]

    current = _current_claim(instance)
    successor = service_propose_playbill_claim(
        instance,
        authoring=DirectClaimAuthoringV1(
            statement=current.statement,
            rationale="Bind admissible evidence to close the uncovered row.",
            claim_id=proposed.claim_identity.removeprefix("Claim:"),
            predecessor_artifact_digest=claim_artifact_digest(current).tagged,
            source_selection=DirectForeignSourceSelectionV1(
                logical_source_identity=source_id,
                span=ContentSpan(
                    content_digest=source_digest,
                    start_byte=8,
                    end_byte=13,
                ),
                media_type="text/markdown",
            ),
        ),
        actor_id="owner",
        proposal_name="closed-loop-cover-claim",
        timestamp="2026-08-24T17:00:03.000000Z",
    )
    activate(instance, owner, successor)
    _assert_gone(instance, "claim_uncovered", _request(instance))


def _freshness_world(root: Path):  # type: ignore[no-untyped-def]
    instance, owner = initialize_local(root)
    source_id = "fixture.freshness"
    _seed_claim_surface(
        instance,
        owner,
        contract=foreign_source_capture_contract(source_id),
    )
    body = instance.body_store().store(b"status: ready")
    proposed = service_propose_playbill_claim(
        instance,
        authoring=authoring("wi-42", "ready", with_claim_type=False).model_copy(
            update={
                "source_selection": DirectForeignSourceSelectionV1(
                    logical_source_identity=source_id,
                    span=ContentSpan(
                        content_digest=body.digest,
                        start_byte=8,
                        end_byte=13,
                    ),
                )
            }
        ),
        actor_id="owner",
        proposal_name="closed-loop-freshness-initial",
        timestamp="2026-08-16T20:00:00.000000Z",
    )
    _activate_direct_claim(instance, owner, proposed)
    path = claim_type_path(_claim_type().predicate)
    predecessor = parse_claim_type(
        instance.tree_at(instance.accepted_coordinate().git_oid)[path],
        path=path,
    )
    successor = ClaimType.model_validate(
        {
            **predecessor.model_dump(mode="json"),
            "artifact_format": "playbill-claim-type-v3",
            "evidence_freshness": ClaimEvidenceFreshnessV1(
                stale_after=ClaimFreshnessDurationV1(microseconds=10_000_000)
            ).model_dump(mode="json"),
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=claim_type_digest(predecessor).tagged
            ).model_dump(mode="json"),
        }
    )
    migration = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV1(
            successor=successor,
            dependents=(
                ClaimTypeDependentDispositionV1(
                    claim_id=proposed.claim_identity.removeprefix("Claim:"),
                    disposition="successor",
                ),
            ),
        ),
        actor=AuthenticatedActor(actor_id="owner"),
    )
    candidate = migration.proposal.proposal.candidate
    assert candidate is not None
    _activate_migration(
        instance,
        owner,
        migration.proposal.proposal.admission.proposal_id,
        candidate.candidate_digest,
    )
    return instance, owner


def _refresh_claim(instance, owner, *, timestamp: str) -> None:  # type: ignore[no-untyped-def]
    current = _current_claim(instance)
    body = instance.body_store().store(b"status: ready\n")
    proposed = service_propose_playbill_claim(
        instance,
        authoring=DirectClaimAuthoringV1(
            statement=current.statement,
            rationale="Recapture the still-standing statement at a fresh instant.",
            claim_id=current.identity.name,
            predecessor_artifact_digest=claim_artifact_digest(current).tagged,
            source_selection=DirectForeignSourceSelectionV1(
                logical_source_identity="fixture.freshness",
                span=ContentSpan(
                    content_digest=body.digest,
                    start_byte=8,
                    end_byte=13,
                ),
            ),
        ),
        actor_id="owner",
        proposal_name="closed-loop-refresh-evidence",
        timestamp=timestamp,
    )
    _activate_direct_claim(instance, owner, proposed)


def _claim_stale_evidence(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    instance, owner = _freshness_world(root)
    at_expiry = datetime(2026, 8, 16, 20, 0, 10, tzinfo=UTC)
    before = _request(instance, evaluation_time=at_expiry)
    row = _row(instance, "claim_stale_evidence", before)
    assert row.repair.operation == EXPECTED_OPERATIONS["claim_stale_evidence"]

    _refresh_claim(instance, owner, timestamp="2026-08-16T20:00:09.000000Z")
    _assert_gone(instance, "claim_stale_evidence", _request(instance, evaluation_time=at_expiry))


def _evidence_expiring(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    instance, owner = _freshness_world(root)
    evaluation_time = datetime(2026, 8, 16, 20, 0, 9, tzinfo=UTC)
    before = _request(instance, evaluation_time=evaluation_time, expiring_within=2_000_000)
    row = _row(instance, "evidence_expiring", before)
    assert row.repair.operation == EXPECTED_OPERATIONS["evidence_expiring"]

    _refresh_claim(instance, owner, timestamp="2026-08-16T20:00:08.000000Z")
    _assert_gone(
        instance,
        "evidence_expiring",
        _request(instance, evaluation_time=evaluation_time, expiring_within=2_000_000),
    )


def _citation_drifted_changed(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    instance, owner = seed_claims(root)
    current = _current_claim(instance)
    citation = claim_citation_references(current)[0]
    expected = next(
        item.capture_digest
        for item in claim_citation_references(current)
        if item.citation_id == citation.citation_id
    )
    envelope = instance.body_store().read(
        expected,
        access=BodyAccessContext(principal_id="closed-loop", can_read_body=True),
    )
    commitment = parse_capture_envelope(envelope).commitment.digest
    rebound_body = instance.body_store().store(b"status: blocked\n")
    successor = service_propose_playbill_claim(
        instance,
        authoring=authoring("wi-42", "blocked", with_claim_type=False).model_copy(
            update={
                "claim_id": current.identity.name,
                "predecessor_artifact_digest": claim_artifact_digest(current).tagged,
                "source_selection": DirectForeignSourceSelectionV1(
                    logical_source_identity="fixture.work-items",
                    span=ContentSpan(
                        content_digest=rebound_body.digest,
                        start_byte=8,
                        end_byte=15,
                    ),
                    media_type="text/markdown",
                ),
            }
        ),
        actor_id="owner",
        proposal_name="closed-loop-revise-drifted-claim",
        timestamp="2026-08-24T17:00:03.000000Z",
    )
    evaluated_oid = successor.proposal.proposal.evaluation.evaluated_tree_oid
    assert evaluated_oid is not None
    proposed_claim = parse_claim(
        instance.proposal_tree(evaluated_oid)[claim_path(current.identity.name)],
        path=claim_path(current.identity.name),
    )
    accepted_citation_ids = {item.citation_id for item in claim_citation_references(current)}
    rebound_citation, rebound_envelope = next(
        (candidate, candidate_envelope)
        for candidate in claim_citation_references(proposed_claim)
        for candidate_envelope in (
            parse_capture_envelope(
                instance.body_store().read(
                    candidate.capture_digest,
                    access=BodyAccessContext(principal_id="closed-loop", can_read_body=True),
                )
            ),
        )
        if candidate.citation_id not in accepted_citation_ids
    )
    observed = rebound_envelope.commitment.digest
    assert observed != commitment
    before = _request(
        instance,
        workspace=PlaybillNextWorkspaceObservationV1(
            drift_observations=(
                PlaybillNextDriftObservationV1(
                    citation_id=citation.citation_id,
                    expected_commitment_digest=commitment,
                    observed_commitment_digest=observed,
                ),
            )
        ),
    )
    key = ("citation_drifted", "changed")
    row = _row_by_key(instance, key, before)
    assert row.repair.operation == _expected_operation(key)
    assert row.repair.required_change == "adjudicate_citation_drift"

    activate(instance, owner, successor)
    _assert_key_gone(
        instance,
        key,
        _request(
            instance,
            workspace=PlaybillNextWorkspaceObservationV1(
                drift_observations=(
                    PlaybillNextDriftObservationV1(
                        citation_id=rebound_citation.citation_id,
                        expected_commitment_digest=rebound_envelope.commitment.digest,
                        observed_commitment_digest=observed,
                    ),
                )
            ),
        ),
    )


def _foreign_citation(instance):  # type: ignore[no-untyped-def]
    current = _current_claim(instance)
    for citation in claim_citation_references(current):
        envelope = parse_capture_envelope(
            instance.body_store().read(
                citation.capture_digest,
                access=BodyAccessContext(principal_id="closed-loop", can_read_body=True),
            )
        )
        if isinstance(envelope.source, ExternalSourceReferenceV1):
            return current, citation, envelope
    raise AssertionError("the closed-loop world has no foreign citation")


def _v4_citation_observation(
    *,
    instance,  # type: ignore[no-untyped-def]
    source_id: str,
    citation_id: str,
    envelope,  # type: ignore[no-untyped-def]
    state: str,
) -> PlaybillNextSourceObservationV4:
    source = LogicalSourceIdentityV1(plane="external", identity=source_id)
    selector = envelope.source.selector
    assert isinstance(selector, Mapping)
    window = selector.get("working_selection", selector)
    assert isinstance(window, Mapping)
    start = window["start_byte"]
    end = window["end_byte"]
    assert isinstance(start, int) and isinstance(end, int)
    commitment = envelope.commitment.digest
    byte_length = envelope.commitment.byte_length
    assert byte_length is not None

    if state == "gone":
        source_bytes = b"gone\n"
    elif state == "ambiguous":
        selected = b"ready"
        source_bytes = selected + b"\n" + selected + b"\n"
    elif state == "current":
        source_bytes = b"status: blocked\n"
    else:
        raise AssertionError(f"unsupported closed-loop citation state: {state}")
    source_digest = instance.body_store().store(source_bytes).digest

    occurrences: tuple[WorkingOccurrenceV1, ...] = ()
    if state in {"ambiguous", "current"}:
        offsets = (0, 6) if state == "ambiguous" else (start,)
        occurrences = tuple(
            WorkingOccurrenceV1(
                source=source,
                observed_commitment_digest=commitment,
                byte_length=byte_length,
                ordinal=ordinal,
                identity_digest=occurrence_identity_digest(
                    source=source,
                    observed_commitment_digest=commitment,
                    ordinal=ordinal,
                ),
                line_overlay=CoverageLineOverlayV1(
                    start_byte=offset,
                    end_byte=offset + byte_length,
                    start_line=ordinal + 1,
                    end_line=ordinal + 1,
                ),
            )
            for ordinal, offset in enumerate(offsets)
        )
    windows = (
        (
            PlaybillCitationWindowObservationV1(
                source=source,
                citation_id=citation_id,
                commitment_digest=commitment,
                original_start=start,
                original_end=end,
                addressable=False,
                observed_window_digest=None,
            ),
        )
        if state == "gone"
        else ()
    )
    return PlaybillNextSourceObservationV4(
        source_id=source_id,
        observed_source_digest=source_digest,
        byte_length=len(source_bytes),
        marker_summaries=(),
        occurrences=occurrences,
        commitment_scan_proofs=(
            CoverageCommitmentScanProofV1(
                source=source,
                commitment_digest=commitment,
                byte_length=byte_length,
            ),
        ),
        citation_window_observations=windows,
        scan_notes=(),
        marker_notes=(),
    )


def _citation_drifted_v4(
    root: Path,
    *,
    drift_state: str,
) -> None:
    instance, owner, _successor, source_id, _source_digest = _foreign_world(root, bind=True)
    current, citation, envelope = _foreign_citation(instance)
    before_observation = _v4_citation_observation(
        instance=instance,
        source_id=source_id,
        citation_id=citation.citation_id,
        envelope=envelope,
        state=drift_state,
    )
    key = ("citation_drifted", drift_state)
    before = _request(
        instance,
        workspace=PlaybillNextWorkspaceObservationV1(source_observations=(before_observation,)),
    )
    row = _row_by_key(instance, key, before)
    assert row.repair.operation == _expected_operation(key)
    assert row.repair.required_change == (
        "retire_claim_with_attribution" if drift_state == "gone" else "adjudicate_citation_drift"
    )

    if drift_state == "gone":
        coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
        retirement = service_retire_claim(
            instance,
            claim_id=current.identity.name,
            request=ClaimRetireRequestV1(
                mode="submit",
                claim_ref=current.identity.qualified,
                reason="was-rescinded",
                expected_coordinate=coordinate,
            ),
            actor=AuthenticatedActor(actor_id="owner"),
        )
        assert isinstance(retirement, ClaimRetireResultV1)
        assert retirement.proposal is not None
        proposal = retirement.proposal.proposal
        candidate = proposal.candidate
        assert candidate is not None
        if candidate.approval_requirements:
            approval = _sign(
                client_material(instance.root.parent, instance),
                candidate.candidate_digest,
                instance.accepted_coordinate().semantic_root,
            )
            service_submit_playbill_approval(
                instance,
                proposal_id=proposal.admission.proposal_id,
                attestation=approval.attestation,
                authenticated_submitter="owner",
            )
        assert (
            service_activate_playbill_proposal(
                instance,
                proposal_id=proposal.admission.proposal_id,
                activated_by="owner",
            ).status
            == "accepted"
        )
        _assert_key_gone(
            instance,
            key,
            _request(instance, workspace=before.workspace_observation),
        )
        return

    source_body = instance.body_store().store(b"status: blocked\n")
    successor = service_propose_playbill_claim(
        instance,
        authoring=DirectClaimAuthoringV1(
            statement=authoring("wi-42", "blocked", with_claim_type=False).statement.model_copy(
                update={"claim_type_digest": current.statement.claim_type_digest}
            ),
            rationale=f"Adjudicate the mechanically {drift_state} citation.",
            claim_id=current.identity.name,
            predecessor_artifact_digest=claim_artifact_digest(current).tagged,
            source_selection=DirectForeignSourceSelectionV1(
                logical_source_identity=source_id,
                span=ContentSpan(
                    content_digest=source_body.digest,
                    start_byte=8,
                    end_byte=15,
                ),
                media_type="text/markdown",
            ),
        ),
        actor_id="owner",
        proposal_name=f"closed-loop-adjudicate-{drift_state}-citation",
        timestamp="2026-08-24T17:00:04.000000Z",
    )
    activate(instance, owner, successor)
    _new_current, new_citation, new_envelope = _foreign_citation(instance)
    current_observation = _v4_citation_observation(
        instance=instance,
        source_id=source_id,
        citation_id=new_citation.citation_id,
        envelope=new_envelope,
        state="current",
    )
    _assert_key_gone(
        instance,
        key,
        _request(
            instance,
            workspace=PlaybillNextWorkspaceObservationV1(
                source_observations=(current_observation,)
            ),
        ),
    )


def _citation_drifted_gone(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    _citation_drifted_v4(root, drift_state="gone")


def _citation_drifted_ambiguous(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    _citation_drifted_v4(root, drift_state="ambiguous")


def _citation_source_unobserved(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _owner, _successor, source_id, _source_digest = _foreign_world(root, bind=True)
    before = _request(
        instance,
        workspace=PlaybillNextWorkspaceObservationV1(source_observations=()),
    )
    row = _row(instance, "citation_source_unobserved", before)
    assert row.repair.operation == EXPECTED_OPERATIONS["citation_source_unobserved"]

    _current, citation, envelope = _foreign_citation(instance)
    observed = PlaybillNextWorkspaceObservationV1(
        source_observations=(
            _v4_citation_observation(
                instance=instance,
                source_id=source_id,
                citation_id=citation.citation_id,
                envelope=envelope,
                state="current",
            ),
        )
    )
    _assert_gone(instance, "citation_source_unobserved", _request(instance, workspace=observed))


def _floor_case(root: Path, reason: str) -> None:
    instance, _owner = initialize_local(root)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    if reason == "floor_missing":
        before_workspace = PlaybillNextWorkspaceObservationV1(floor_status="missing")
    elif reason == "floor_invalid":
        before_workspace = PlaybillNextWorkspaceObservationV1(floor_status="invalid")
    else:
        before_workspace = PlaybillNextWorkspaceObservationV1(floor_status="stale")
    row = _row(instance, reason, _request(instance, workspace=before_workspace))
    assert row.repair.operation == EXPECTED_OPERATIONS[reason]

    current = PlaybillNextWorkspaceObservationV1(
        floor_status="current",
        installed_coordinate=coordinate,
    )
    _assert_gone(instance, reason, _request(instance, workspace=current))


def _floor_missing(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_case(root, "floor_missing")


def _floor_stale(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_case(root, "floor_stale")


def _floor_invalid(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_case(root, "floor_invalid")


def _projection_dirty(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _owner = _instance_with_query(root)
    backing = (_claim_backing(instance),)
    before = _projection_request(instance, backing=backing, dirty=True)
    row = _row(instance, "projection_dirty", before)
    assert row.repair.operation == EXPECTED_OPERATIONS["projection_dirty"]

    _assert_gone(
        instance,
        "projection_dirty",
        _projection_request(instance, backing=backing, dirty=False),
    )


def _projection_backing_stale(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _owner = _instance_with_query(root)
    before = _projection_request(instance, backing=(_claim_backing(instance, stale=True),))
    row = _row(instance, "projection_backing_stale", before)
    assert row.repair.operation == EXPECTED_OPERATIONS["projection_backing_stale"]

    _assert_gone(
        instance,
        "projection_backing_stale",
        _projection_request(instance, backing=(_claim_backing(instance),)),
    )


def _publish_replacement_claim(instance, owner) -> None:  # type: ignore[no-untyped-def]
    _publish_self_published_claim(
        instance,
        owner,
        timestamp="2026-08-21T12:02:00.000000Z",
        successor_timestamp="2026-08-21T12:02:01.000000Z",
        proposal_suffix="replacement-self-published-copy",
    )


def _self_published_source_stale(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    instance, owner, claim_id = _published_world(root)
    _retire(instance, owner, claim_id)
    row = _row(instance, "self_published_source_stale", _published_request(instance))
    assert row.repair.operation == EXPECTED_OPERATIONS["self_published_source_stale"]

    _publish_replacement_claim(instance, owner)
    _assert_gone(instance, "self_published_source_stale", _published_request(instance))


def _claim_dependency_stale(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _owner = initialize_local(root)
    source_old = _source_v1()
    source_current = _source_v2()
    dependent_old = _derived()
    dependent_current = _dependency_claim(
        DERIVED_INDEX,
        item="wi-2",
        value="derived-from-wi-1",
        lifecycle=ArtifactLifecycle(predecessor_digest=dependent_old.accepted.artifact_digest),
        input_digests=(source_current.accepted.artifact_digest,),
    )
    repaired = False

    def facts(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        rows = (source_current, dependent_current if repaired else dependent_old)
        return _dependency_facts(rows).model_copy(
            update={"coordinate": instance.accepted_coordinate()}
        )

    def lineages(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        dependent = dependent_current if repaired else dependent_old
        return (
            {
                SOURCE_PATH: (
                    source_old.accepted.artifact_digest,
                    source_current.accepted.artifact_digest,
                ),
                dependent.accepted.path: (dependent.accepted.artifact_digest,),
            },
            frozenset(),
        )

    monkeypatch.setattr("cruxible_core.service.playbill_next.build_accepted_query_facts", facts)
    monkeypatch.setattr("cruxible_core.service.playbill_next._bounded_claim_lineages", lineages)
    row = _row(instance, "claim_dependency_stale", _request(instance))
    assert row.repair.operation == EXPECTED_OPERATIONS["claim_dependency_stale"]

    repaired = True
    _assert_gone(instance, "claim_dependency_stale", _request(instance))


def _document_modified(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    instance, owner = initialize_local(root)
    accepted_body = instance.store_document_body(b"accepted body\n")
    changed_body = instance.store_document_body(b"changed body\n")
    document = DocumentShell(
        identity="document:runbook",
        document_kind="runbook",
        title="Runbook",
        media_type="text/markdown",
        body_digest=accepted_body.digest,
        authority=DocumentAuthority(required_tier="governed_write"),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    _Builder(instance, owner).accept(
        {"documents/runbook.yaml": render_document(document)},
        phase="closed-loop-document",
    )
    instance.refresh()
    changed_observation = PlaybillNextWorkspaceObservationV1(
        source_observations=(
            PlaybillNextSourceObservationV3(
                tag="playbill-next-source-observation-v3",
                source_id="corpus.runbook",
                document_id="runbook",
                observed_source_digest=changed_body.digest,
                byte_length=0,
                marker_summaries=(),
                occurrences=(),
                scanned_commitment_digests=(),
                scan_complete=True,
                scan_notes=(),
                marker_notes=(),
            ),
        )
    )
    row = _row(instance, "document_modified", _request(instance, workspace=changed_observation))
    assert row.repair.operation == EXPECTED_OPERATIONS["document_modified"]

    successor = document.model_copy(
        update={
            "body_digest": changed_body.digest,
            "predecessor_digest": document_digest(document).tagged,
            "lifecycle": DocumentLifecycle(revision=2),
        }
    )
    proposal = service_propose_playbill_document(
        instance,
        shell=successor,
        actor_id="owner",
        proposal_name="closed-loop-repropose-document",
        timestamp="2026-08-24T17:00:02.000000Z",
    )
    candidate = proposal.proposal.candidate
    assert candidate is not None
    base = instance.accepted_coordinate()
    evaluated_oid = proposal.proposal.evaluation.evaluated_tree_oid
    assert evaluated_oid is not None
    bundle = instance.prepare_generation(
        base=base,
        candidate_tree=instance.proposal_tree(evaluated_oid),
        candidate=candidate,
        approvals=(
            _sign(
                client_material(instance.root.parent, instance),
                candidate.candidate_digest,
                base.semantic_root,
            ),
        ),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        proposal_actor_id="owner",
        sequence=2,
    )
    publisher = instance.activation_publisher()
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"
    instance.refresh()
    _assert_gone(instance, "document_modified", _request(instance, workspace=changed_observation))


def _claim_attestation_threshold(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_playbill.test_attestation_consequence_next import threshold_world

    instance, owner, _claim = threshold_world(root, monkeypatch)
    row = _row(instance, "claim_attestation_threshold_met", _request(instance))
    assert row.repair.operation == EXPECTED_OPERATIONS["claim_attestation_threshold_met"]
    current = _current_claim(instance)
    repair = service_propose_playbill_claim(
        instance,
        authoring=DirectClaimAuthoringV1(
            statement=current.statement.model_copy(
                update={"object": LiteralClaimObject(value="blocked")}
            ),
            rationale="Revise the tested statement after reviewing the unsure attestations.",
            claim_id=current.identity.name,
            predecessor_artifact_digest=claim_artifact_digest(current).tagged,
        ),
        actor_id="owner",
        proposal_name="closed-loop-attestation-threshold",
        timestamp="2026-08-24T17:00:03.000000Z",
    )
    activate(instance, owner, repair)
    _assert_gone(instance, "claim_attestation_threshold_met", _request(instance))


CLOSED_LOOP_CASES: dict[ClosedLoopKey, RepairCase] = {
    ("claim_conflicted", None): _claim_conflicted,
    ("claim_uncovered", None): _claim_uncovered,
    ("claim_stale_evidence", None): _claim_stale_evidence,
    ("citation_drifted", "changed"): _citation_drifted_changed,
    ("citation_drifted", "gone"): _citation_drifted_gone,
    ("citation_drifted", "ambiguous"): _citation_drifted_ambiguous,
    ("citation_source_unobserved", None): _citation_source_unobserved,
    ("evidence_expiring", None): _evidence_expiring,
    ("floor_missing", None): _floor_missing,
    ("floor_stale", None): _floor_stale,
    ("floor_invalid", None): _floor_invalid,
    ("projection_dirty", None): _projection_dirty,
    ("projection_backing_stale", None): _projection_backing_stale,
    ("self_published_source_stale", None): _self_published_source_stale,
    ("claim_dependency_stale", None): _claim_dependency_stale,
    ("claim_attestation_threshold_met", None): _claim_attestation_threshold,
    ("document_modified", None): _document_modified,
}


@pytest.mark.parametrize("key", tuple(CLOSED_LOOP_CASES))
def test_every_next_reason_has_an_effective_named_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: ClosedLoopKey,
) -> None:
    # `claim_cites_retired` is RESERVED on the wire and never emitted -- the
    # stranded-citation row was withdrawn pending the copy-edge design. A reason
    # nothing emits has no repair to demonstrate, so it is exempt here; the value
    # rejoins this law when something emits it again.
    reasons = set(get_args(NextReason)) - {"claim_cites_retired"}
    assert {reason for reason, _discriminator in CLOSED_LOOP_CASES} == reasons
    assert {
        discriminator for reason, discriminator in CLOSED_LOOP_CASES if reason == "citation_drifted"
    } == {"changed", "gone", "ambiguous"}
    assert set(EXPECTED_OPERATIONS) == reasons

    case_root = tmp_path / "-".join(part for part in key if part is not None)
    case_root.mkdir()
    CLOSED_LOOP_CASES[key](case_root, monkeypatch)
