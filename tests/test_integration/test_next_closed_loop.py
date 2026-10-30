"""Every Cruxible next row names a repair that can close that row."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_args

import pytest

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    CanonicalDuration,
    DirectForeignSourceSelection,
    capture_contract_digest,
    capture_contract_path,
    foreign_source_capture_contract,
    parse_capture_envelope,
    render_capture_contract,
)
from cruxible_client.contracts.claim_types import (
    ClaimEvidenceFreshness,
    ClaimFreshnessDuration,
    ClaimType,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.claims import (
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
from cruxible_client.contracts.procedures.line_specs import line_spec_path
from cruxible_client.contracts.semantic import ContentSpan
from cruxible_client.contracts.source_references import ExternalSourceReference
from cruxible_client.contracts.subjects import render_subject, subject_path
from cruxible_client.contracts.temporal import format_datetime
from cruxible_core.claims.claim_type_migrations import (
    ClaimTypeDependentDispositionV1,
    ClaimTypeMigrationRequestV1,
    service_migrate_claim_type,
)
from cruxible_core.coverage.contracts import (
    CitationWindowObservation,
    CoverageAccessProfile,
    CoverageCommitmentScanProof,
    CoverageLineOverlay,
    LogicalSourceIdentity,
    occurrence_identity_digest,
)
from cruxible_core.coverage.indexes import WorkingOccurrence
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.procedures.reference_binding import authored_procedure
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.proposals.settlement import ChangeActorBinding
from cruxible_core.service.authoring.documents import (
    service_propose_playbill_document,
    service_submit_playbill_approval,
)
from cruxible_core.service.claims.claims import (
    _claim_from_view,
    service_list_playbill_claims,
)
from cruxible_core.service.discovery.next import (
    NextDriftObservation,
    NextReason,
    NextRequest,
    NextSourceObservation,
    NextWorkspaceObservation,
    service_playbill_next,
)
from cruxible_core.service.proposals.proposals import service_readmit_playbill_proposal
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._adoption_fixture import _Builder
from tests.core_support._claim_authoring_support import (
    DirectClaimAuthoringV1,
    ExistingStatementHandoffV1,
    _activate_direct_claim,
    service_propose_playbill_claim,
    service_reobserve_playbill_claim,
)
from tests.core_support._knowledge_loop_support import (
    activate,
    authoring,
    seed_claims,
    subject_shell,
)
from tests.core_support._published_world import (
    published_world as _published_world,
)
from tests.core_support._published_world import (
    retire_claim as _retire_published_claim,
)
from tests.core_support._subject_kind_support import core_assigned
from tests.core_support._support import client_material, initialize_local
from tests.test_authoring.test_authoring_preflight import _seed_claim_surface
from tests.test_claims.test_claims import _claim_type
from tests.test_evidence.test_evidence_freshness import _activate as _activate_migration
from tests.test_indexes.test_projection_next import (
    _claim_backing,
    _claim_type_backing,
    _instance_with_query,
    _query_backing,
)
from tests.test_indexes.test_projection_next import (
    _request as _projection_request,
)
from tests.test_ledger.test_activation import _sign
from tests.test_query.test_dependency_impact import (
    DERIVED_INDEX,
    SOURCE_PATH,
    _derived,
    _source_v1,
    _source_v2,
)
from tests.test_query.test_dependency_impact import (
    _claim as _dependency_claim,
)
from tests.test_query.test_dependency_impact import (
    _facts as _dependency_facts,
)

EVALUATION_TIME = datetime(2026, 8, 24, 18, tzinfo=UTC)
RepairCase = Callable[[Path, pytest.MonkeyPatch], None]
ClosedLoopKey = tuple[str, str | None]

EXPECTED_OPERATIONS = {
    # A contest offers one option per contender: retire the other (two
    # contenders), or all the others in one write; a qualifier split comes last.
    "claim_conflicted": "cruxible.claim.retire",
    # Stating a Claim again on new evidence is the default-profile set verb.
    "claim_uncovered": "cruxible.set",
    "claim_stale_evidence": "cruxible.set",
    # Re-bind the Claim to the text it rests on now, or (alternative) retire it.
    "citation_drifted": "cruxible.rebind",
    "citation_source_unobserved": "cruxible.set",
    "evidence_expiring": "cruxible.set",
    "floor_invalid": "cruxible.floor.export",
    "projection_dirty": "cruxible.block.repin",
    # Nothing renders a block, so no sync converges one: a drifted block is
    # answered by a repin over the list the author still means to hold. The one
    # exception is a held list with nothing left in it -- every member retired
    # or overturned -- where the registration itself is what has to go.
    "projection_backing_stale": frozenset({"cruxible.block.repin", "cruxible.block.depublish"}),
    # A marker the page has lost is repaired by releasing the registration that
    # demands it; a marker the page has mangled is repaired by restoring it.
    "projection_marker_invalid": frozenset({"cruxible.block.repin", "cruxible.block.depublish"}),
    "claim_dependency_stale": "cruxible.authoring.example",
    "claim_attestation_threshold_met": "cruxible.set",
    "claim_contradicting_evidence_available": "cruxible.authoring.example",
    "claim_new_evidence_supporting": "cruxible.authoring.example",
    "claim_new_evidence_unreviewed": "cruxible.authoring.example",
    "document_modified": "cruxible.files.propose",
    # Restoring a bound file, or fixing its catalog locator, is a workspace edit.
    "workspace_binding_missing": "hand_edit",
    "unregistered_projection_block": "cruxible.block.repin",
    "proposal_stale": "cruxible.proposal.readmit",
    "proposal_awaiting_approval": "cruxible.proposal.approve",
    "proposal_awaiting_activation": "cruxible.proposal.activate",
    "mandate_expiring": "cruxible.authoring.example",
    # A stopped enablement is resumed by enabling again under authority that holds.
    "consumer_stalled": "cruxible.line.enable",
    # A carried Line resumes only after the operator enables its upgraded version.
    "line_disabled_by_upgrade": "cruxible.line.enable",
    # A Line accepted and never enabled starts running on its Triggers once enabled.
    "line_not_enabled": "cruxible.line.enable",
    # A failed run of a Trigger's occurrence: fix its cause; the next good run clears it.
    "line_run_failed": "hand_edit",
    # A restart leaves the downtime unmatched: evaluating that exact range covers it.
    "line_coverage_gap": "cruxible.line.evaluate",
    # Work matched before a restart waits for an explicit dispatch.
    "line_work_pending": "cruxible.line.dispatch",
    # Demoting the Line is a Line successor the row carries in full, ready to submit.
    "line_settled_prior_refusals": "cruxible.authoring.submit",
    # A Line's unread inbox items: the list shows them; judging each clears the row.
    "line_inbox_unread": "cruxible.inbox.list",
    # Restoring a Capture's bytes, or recapturing, is off the daemon's served verbs.
    "evidence_unavailable": "hand_edit",
    # Re-bind onto a fresh Capture under the same contract: the one exit while
    # the Claim still carries the erased Capture.
    "claim_evidence_erased": "cruxible.rebind",
    "prediction_settleable": "cruxible.prediction.settle",
    # Restoring an anchor's material is off the served verbs, like a Capture's.
    "prediction_window_unbindable": "hand_edit",
    # A kind with Subjects and no SubjectKind: authoring one lets it create again.
    "subject_kind_undeclared": "cruxible.authoring.example",
    # A recorded kit add resume whose head moved out from under it: run kit
    # add again, resolving the refusal that recorded the resume.
    "kit_resume_blocked": "cruxible.kit.add",
}


def _expected_operation(key: ClosedLoopKey) -> str:
    if key == ("projection_marker_invalid", "registered_marker_missing"):
        return "cruxible.block.depublish"
    if key[0] == "projection_marker_invalid":
        return "cruxible.block.repin"
    if key[0] == "projection_backing_stale":
        return "cruxible.block.depublish" if key[1] == "exhausted" else "cruxible.block.repin"
    expected = EXPECTED_OPERATIONS[key[0]]
    assert isinstance(expected, str)
    return expected


def _access() -> CoverageAccessProfile:
    return CoverageAccessProfile(
        profile_id="next-closed-loop",
        permitted_access_classes=("instance", "public"),
    )


def _request(
    instance,  # type: ignore[no-untyped-def]
    *,
    evaluation_time: datetime = EVALUATION_TIME,
    expiring_within: int = 604_800_000_000,
    workspace: NextWorkspaceObservation | None = None,
) -> NextRequest:
    return NextRequest(
        at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        evaluation_time=evaluation_time,
        access_profile=_access(),
        expiring_within=CanonicalDuration(microseconds=expiring_within),
        workspace_observation=workspace,
    )


#: The reasons whose rows say when their condition began (``since``); every
#: other reason's row leaves it absent.
SINCE_REASONS = frozenset(
    {
        "line_not_enabled",
        "line_disabled_by_upgrade",
        "line_coverage_gap",
        "line_work_pending",
        "line_run_failed",
        "claim_conflicted",
        "claim_contradicting_evidence_available",
        "claim_new_evidence_supporting",
        "claim_new_evidence_unreviewed",
        "proposal_awaiting_approval",
        "prediction_settleable",
    }
)


def _row(instance, reason: str, request: NextRequest, **caller: Any):  # type: ignore[no-untyped-def]
    row = next(
        item
        for item in service_playbill_next(instance, request=request, **caller).items
        if item.reason == reason
    )
    assert (row.since is not None) is (reason in SINCE_REASONS), (reason, row.since)
    return row


def _first_acceptance_oid(instance, identity: str) -> str:  # type: ignore[no-untyped-def]
    """The generation that first accepted an artifact, read from its member history."""

    kind, _sep, name = identity.partition(":")
    assert kind == "Line"
    with instance.accepted_history_reader() as history:
        first = history.member_history(line_spec_path(name))[0]
        return history.generation(first.sequence).git_oid


def _assert_gone(instance, reason: str, request: NextRequest, **caller: Any) -> None:  # type: ignore[no-untyped-def]
    left = [
        item
        for item in service_playbill_next(instance, request=request, **caller).items
        if item.reason == reason
    ]
    assert not left, left


def _item_key(item: object) -> ClosedLoopKey:
    reason = getattr(item, "reason")
    detail = getattr(item, "detail")
    discriminator = None
    if isinstance(detail, Mapping):
        if reason == "citation_drifted":
            discriminator = detail.get("drift_state")
        elif reason == "projection_marker_invalid":
            status = detail.get("marker_status")
            discriminator = status if status == "registered_marker_missing" else None
        elif reason == "projection_backing_stale":
            # Two producers, two repairs: a member that moved is repinned, a
            # held list with nothing left in it is depublished. The pin has to
            # tell them apart or it claims a vocabulary the reason lacks.
            discriminator = detail.get("backing_state")
    return reason, discriminator


def _row_by_key(
    instance,
    key: ClosedLoopKey,
    request: NextRequest,  # type: ignore[no-untyped-def]
):  # type: ignore[no-untyped-def]
    row = next(
        item
        for item in service_playbill_next(instance, request=request).items
        if _item_key(item) == key
    )
    assert (row.since is not None) is (key[0] in SINCE_REASONS), (key, row.since)
    return row


def _assert_key_gone(
    instance,
    key: ClosedLoopKey,
    request: NextRequest,  # type: ignore[no-untyped-def]
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
        if claim.statement.subject.artifact_path.endswith(f"/{subject_id}.json")
        # A work item's slug is its key Claim, written when core created it.
        and not claim.statement.predicate.endswith(".slug")
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
    # The contest, as judged now, has held since its newest input was accepted:
    # the contradicting Claim's change set.
    assert row.since == "2026-08-24T17:00:03+00:00"

    contenders = tuple(
        claim
        for claim in (
            _claim_from_view(view) for view in service_list_playbill_claims(instance).claims
        )
        if claim.statement.subject.artifact_path.endswith("/wi-42.json")
        and not claim.statement.predicate.endswith(".slug")
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
    direct_digest = capture_contract_digest(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT).tagged
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
        core_assigned(
            instance,
            {
                subject_path(shell.subject_kind, shell.subject_id): render_subject(shell),
                claim_type_path(claim_type.predicate): render_claim_type(claim_type),
                capture_contract_path(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity.name): (
                    render_capture_contract(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT)
                ),
                capture_contract_path(foreign_contract.identity.name): render_capture_contract(
                    foreign_contract
                ),
            },
        ),
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
    selected = DirectForeignSourceSelection(
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
            source_selection=DirectForeignSourceSelection(
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


def _freshness_world(root: Path, *, stale_after: int = 10_000_000):  # type: ignore[no-untyped-def]
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
                "source_selection": DirectForeignSourceSelection(
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
            "evidence_freshness": ClaimEvidenceFreshness(
                stale_after=ClaimFreshnessDuration(microseconds=stale_after)
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
    """Re-observe the still-standing statement: the same content, observed again.

    It changes nothing about the Claim -- no revision, no proposal -- and the
    recorded re-observation is what keeps the cited content fresh.
    """

    del owner
    current = _current_claim(instance)
    body = instance.body_store().store(b"status: ready\n")
    submitted = service_reobserve_playbill_claim(
        instance,
        authoring=DirectClaimAuthoringV1(
            statement=current.statement,
            rationale="Recapture the still-standing statement at a fresh instant.",
            claim_id=current.identity.name,
            predecessor_artifact_digest=claim_artifact_digest(current).tagged,
            source_selection=DirectForeignSourceSelection(
                logical_source_identity="fixture.freshness",
                span=ContentSpan(
                    content_digest=body.digest,
                    start_byte=8,
                    end_byte=13,
                ),
            ),
        ),
        actor_id="owner",
        timestamp=timestamp,
    )
    assert _current_claim(instance) == current
    assert submitted.status.state == "accepted"


def _revise_claim(instance, owner, *, timestamp: str) -> None:  # type: ignore[no-untyped-def]
    """Restate the statement on newly observed bytes: a revision that moves accepted state."""

    current = _current_claim(instance)
    body = instance.body_store().store(b"status: ready\n")
    proposed = service_propose_playbill_claim(
        instance,
        authoring=DirectClaimAuthoringV1(
            statement=current.statement,
            rationale="Recapture the still-standing statement from new bytes.",
            claim_id=current.identity.name,
            predecessor_artifact_digest=claim_artifact_digest(current).tagged,
            source_selection=DirectForeignSourceSelection(
                logical_source_identity="fixture.freshness",
                span=ContentSpan(content_digest=body.digest, start_byte=8, end_byte=14),
            ),
        ),
        actor_id="owner",
        proposal_name="closed-loop-revise-evidence",
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
                "source_selection": DirectForeignSourceSelection(
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
        workspace=NextWorkspaceObservation(
            drift_observations=(
                NextDriftObservation(
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
    assert row.repair.required_change == "rebind_or_retire_drifted_claim"
    assert [item.operation for item in row.repair.alternatives] == ["cruxible.claim.retire"]

    activate(instance, owner, successor)
    _assert_key_gone(
        instance,
        key,
        _request(
            instance,
            workspace=NextWorkspaceObservation(
                drift_observations=(
                    NextDriftObservation(
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
        if isinstance(envelope.source, ExternalSourceReference):
            return current, citation, envelope
    raise AssertionError("the closed-loop world has no foreign citation")


def _v4_citation_observation(
    *,
    instance,  # type: ignore[no-untyped-def]
    source_id: str,
    citation_id: str,
    envelope,  # type: ignore[no-untyped-def]
    state: str,
) -> NextSourceObservation:
    source = LogicalSourceIdentity(plane="external", identity=source_id)
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

    occurrences: tuple[WorkingOccurrence, ...] = ()
    if state in {"ambiguous", "current"}:
        offsets = (0, 6) if state == "ambiguous" else (start,)
        occurrences = tuple(
            WorkingOccurrence(
                source=source,
                observed_commitment_digest=commitment,
                byte_length=byte_length,
                ordinal=ordinal,
                identity_digest=occurrence_identity_digest(
                    source=source,
                    observed_commitment_digest=commitment,
                    ordinal=ordinal,
                ),
                line_overlay=CoverageLineOverlay(
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
            CitationWindowObservation(
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
    return NextSourceObservation(
        source_id=source_id,
        observed_source_digest=source_digest,
        byte_length=len(source_bytes),
        marker_summaries=(),
        occurrences=occurrences,
        commitment_scan_proofs=(
            CoverageCommitmentScanProof(
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
        workspace=NextWorkspaceObservation(source_observations=(before_observation,)),
    )
    row = _row_by_key(instance, key, before)
    assert row.repair.operation == _expected_operation(key)
    assert row.repair.required_change == "rebind_or_retire_drifted_claim"
    assert [item.operation for item in row.repair.alternatives] == ["cruxible.claim.retire"]

    if drift_state == "gone":
        from tests.core_support._retirement_support import retire_claim

        retire_claim(instance, owner, current.identity.name)
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
            source_selection=DirectForeignSourceSelection(
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
            workspace=NextWorkspaceObservation(source_observations=(current_observation,)),
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
        workspace=NextWorkspaceObservation(source_observations=()),
    )
    row = _row(instance, "citation_source_unobserved", before)
    assert row.repair.operation == EXPECTED_OPERATIONS["citation_source_unobserved"]

    _current, citation, envelope = _foreign_citation(instance)
    observed = NextWorkspaceObservation(
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
    before_workspace = NextWorkspaceObservation(floor_status="invalid")
    row = _row(instance, reason, _request(instance, workspace=before_workspace))
    assert row.repair.operation == EXPECTED_OPERATIONS[reason]

    current = NextWorkspaceObservation(
        floor_status="current",
        installed_coordinate=coordinate,
    )
    _assert_gone(instance, reason, _request(instance, workspace=current))


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


def _projection_backing_stale_revised(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    key: ClosedLoopKey = ("projection_backing_stale", "revised")
    instance, _owner = _instance_with_query(root)
    before = _projection_request(instance, backing=(_query_backing(instance, stale=True),))
    row = _row_by_key(instance, key, before)
    assert row.repair.operation == _expected_operation(key)

    _assert_key_gone(
        instance,
        key,
        _projection_request(instance, backing=(_claim_backing(instance),)),
    )


def _projection_backing_stale_retired(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    """One member of a held list retires; the rest of the list is still held."""

    key: ClosedLoopKey = ("projection_backing_stale", "retired")
    instance, owner, claim_id = _published_world(root)
    survivor = _claim_type_backing(instance)
    held = _claim_backing(instance)
    _retire_published_claim(instance, owner, claim_id)
    before = _projection_request(instance, backing=(held, survivor))
    row = _row_by_key(instance, key, before)
    assert row.repair.operation == _expected_operation(key)
    assert row.repair.required_change == "drop_the_retired_backing_then_repin"
    assert row.repair.arguments["claim"] == []
    assert row.repair.arguments["clear_claims"] is True

    _assert_key_gone(instance, key, _projection_request(instance, backing=(survivor,)))


def _projection_backing_stale_exhausted(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    """Every held member is gone, so the registration itself is what has to go."""

    key: ClosedLoopKey = ("projection_backing_stale", "exhausted")
    instance, owner, claim_id = _published_world(root)
    held = _claim_backing(instance)
    _retire_published_claim(instance, owner, claim_id)
    before = _projection_request(instance, backing=(held,))
    row = _row_by_key(instance, key, before)
    assert row.repair.operation == _expected_operation(key)
    assert row.repair.required_change == "depublish_retired_backing_block"

    # Depublishing releases the registration and the marker leaves the page.
    observation = before.workspace_observation
    assert observation is not None
    assert observation.source_observations is not None
    (source,) = observation.source_observations
    _assert_key_gone(
        instance,
        key,
        before.model_copy(
            update={
                "workspace_observation": observation.model_copy(
                    update={
                        "source_observations": (source.model_copy(update={"marker_summaries": ()}),)
                    }
                )
            }
        ),
    )


def _projection_marker_missing(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A registered block the page has lost is repaired by releasing its registration."""

    instance, _owner = _instance_with_query(root)
    monkeypatch.setattr(
        "cruxible_core.service.discovery.next._registered_blocks",
        lambda _instance: {
            ("corpus.runbook", "status"): SimpleNamespace(
                source_id="corpus.runbook",
                block_id="status",
            ),
            ("corpus.runbook", "gone"): SimpleNamespace(
                source_id="corpus.runbook",
                block_id="gone",
            ),
        },
    )
    request = _projection_request(instance, backing=(_claim_backing(instance),))
    row = _row_by_key(
        instance,
        ("projection_marker_invalid", "registered_marker_missing"),
        request,
    )
    assert row.repair.operation == "cruxible.block.depublish"
    assert row.repair.required_change == "depublish_or_restore_the_registered_block"


def _projection_marker_invalid(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _owner = _instance_with_query(root)
    backing = (_claim_backing(instance),)
    before = _projection_request(
        instance,
        backing=backing,
        marker_notes=("projection_marker_invalid",),
    )
    row = _row(instance, "projection_marker_invalid", before)
    assert row.repair.operation == _expected_operation(("projection_marker_invalid", None))

    _assert_gone(
        instance,
        "projection_marker_invalid",
        _projection_request(instance, backing=backing),
    )


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

    # This fixture supplies synthetic dependency facts over an empty ledger.
    # Keep the dependency fold's population consistent with those facts too.
    from cruxible_core.service.discovery import next as next_module

    dependency_rows = next_module._claim_dependency_items

    def dependencies(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["claims"] = tuple(row.accepted.claim for row in facts().claims)
        return dependency_rows(*args, **kwargs)

    monkeypatch.setattr(next_module, "_claim_dependency_items", dependencies)
    monkeypatch.setattr("cruxible_core.service.discovery.next._AcceptedQueryFactsRead.build", facts)
    monkeypatch.setattr("cruxible_core.service.discovery.next._bounded_claim_lineages", lineages)
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
        {"documents/runbook.json": render_document(document)},
        phase="closed-loop-document",
    )
    instance.refresh()
    changed_observation = NextWorkspaceObservation(
        source_observations=(
            NextSourceObservation(
                source_id="corpus.runbook",
                document_id="runbook",
                observed_source_digest=changed_body.digest,
                byte_length=0,
                marker_summaries=(),
                occurrences=(),
                commitment_scan_proofs=(),
                citation_window_observations=(),
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
    from tests.test_evidence.test_attestation_consequence_next import (
        accept_signed_threshold_attestations,
        threshold_world,
    )

    instance, owner, claim = threshold_world(root, monkeypatch)
    accept_signed_threshold_attestations(instance, owner, claim, root)
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


def _claim_attestation_door(
    root: Path,
    _monkeypatch: pytest.MonkeyPatch,
    *,
    stance: str,
    reason: str,
) -> None:
    from tests.test_claims.test_claim_attestation_service import (
        _assert_successor_resolves_attestation_membership,
    )

    _assert_successor_resolves_attestation_membership(
        root,
        stance=stance,
        reason=reason,
    )


def _workspace_binding_missing(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    from cruxible_client.authoring.workspace import observe_next_workspace

    (root / "instance").mkdir()
    instance, _owner = initialize_local(root / "instance")
    workspace = root / "workspace"
    (workspace / ".cruxible").mkdir(parents=True)
    (workspace / ".cruxible/files.yaml").write_text(
        "tag: playbill-source-catalog-v1\n"
        "catalog_kind: portable\n"
        "entries:\n"
        "  - name: program-cards\n"
        "    locator: docs/program/cards.md\n"
        "    document_id: program-cards\n"
        "    document_kind: program_page\n"
        "    title: Cards\n"
        "    media_type: text/markdown\n"
        "    governance_scope: [dev]\n",
        encoding="utf-8",
    )

    def observed() -> NextWorkspaceObservation:
        # What the client observes of its catalog's bindings; the per-source
        # scans are the coverage enrichment's, and not this row's concern.
        return NextWorkspaceObservation.model_validate(
            {
                "source_observations": [],
                "missing_bindings": observe_next_workspace(workspace).get("missing_bindings", []),
            }
        )

    row = _row(instance, "workspace_binding_missing", _request(instance, workspace=observed()))
    assert row.subject_identity == "program-cards"
    assert row.repair.operation == EXPECTED_OPERATIONS["workspace_binding_missing"]
    assert row.repair.target == "docs/program/cards.md"

    # The repair: restore the bound file.
    (workspace / "docs/program").mkdir(parents=True)
    (workspace / "docs/program/cards.md").write_text("# Cards\n", encoding="utf-8")
    _assert_gone(instance, "workspace_binding_missing", _request(instance, workspace=observed()))


def _unregistered_projection_block(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    """A marker no registration knows is reported; declaring it is the repair."""

    from cruxible_core.service.proposals.publications import service_declare_playbill_block

    instance, _owner = _instance_with_query(root)
    request = _projection_request(instance, backing=(_claim_backing(instance),))
    row = _row(instance, "unregistered_projection_block", request)
    assert row.repair.operation == _expected_operation(("unregistered_projection_block", None))
    assert row.repair.required_change == "remove_or_register_projection_block"

    observation = request.workspace_observation
    assert observation is not None and observation.source_observations is not None
    stamp = observation.source_observations[0].marker_summaries[0].stamp
    service_declare_playbill_block(
        instance,
        actor_id="owner",
        stamp=stamp,
        declared_at="2026-09-04T12:00:00+00:00",
    )
    _assert_gone(instance, "unregistered_projection_block", request)


def _proposal_stale(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.core_support._support import every_moved_base_overlaps
    from tests.test_proposals.test_proposal_readmit import _accept, _shell

    # Documents touch only their own paths: an unrelated acceptance leaves the
    # overtaken one open (approval lands it). The repair loop under test is the
    # stale row's, so any moved base counts as stale here.
    every_moved_base_overlaps(_monkeypatch)
    instance, owner = initialize_local(root)
    alpha = instance.store_document_body(b"alpha\n").digest
    beta = instance.store_document_body(b"beta\n").digest
    first = service_propose_playbill_document(
        instance,
        shell=_shell("alpha", alpha, title="Alpha"),
        actor_id="owner",
        proposal_name="closed-loop-first",
        timestamp="2026-08-24T17:00:00.000000Z",
    )
    overtaken = service_propose_playbill_document(
        instance,
        shell=_shell("beta", beta, title="Beta"),
        actor_id="owner",
        proposal_name="closed-loop-overtaken",
        timestamp="2026-08-24T17:00:01.000000Z",
    )
    stale_id = overtaken.proposal.admission.proposal_id
    author = {"caller_principal_id": "owner"}
    assert all(
        item.reason != "proposal_stale"
        for item in service_playbill_next(instance, request=_request(instance), **author).items
    )
    _accept(instance, owner, first)

    row = _row(instance, "proposal_stale", _request(instance), **author)
    assert row.subject_identity == stale_id
    assert row.repair.operation == EXPECTED_OPERATIONS["proposal_stale"]
    assert row.repair.command == f"cruxible proposal readmit {stale_id}"
    assert row.detail["actor_id"] == "owner"
    assert row.detail["stale_repair"] == (
        "The head changed what it touches since its base (stale_paths): readmit "
        "recompiles at the head when the origin lowers; review any new candidate "
        "normally, or withdraw."
    )
    assert row.detail["stale_paths"] == ["<moved base>"]
    # Only the author may readmit, so only the author's queue carries the row.
    _assert_gone(instance, "proposal_stale", _request(instance), caller_principal_id="reviewer")
    _assert_gone(instance, "proposal_stale", _request(instance))
    hidden = NextRequest(
        at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        evaluation_time=EVALUATION_TIME,
        access_profile=CoverageAccessProfile(
            profile_id="next-closed-loop-public", permitted_access_classes=("public",)
        ),
    )
    assert all(
        item.reason != "proposal_stale"
        for item in service_playbill_next(instance, request=hidden, **author).items
    )

    readmitted = service_readmit_playbill_proposal(instance, proposal_id=stale_id, actor_id="owner")
    assert readmitted.proposal.proposal.evaluation.verdict == "candidate"
    _assert_gone(instance, "proposal_stale", _request(instance), **author)


def _proposal_awaiting_approval(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    from cruxible_client.contracts.approval_policy import (
        APPROVAL_POLICY_PATH,
        ApprovalPolicy,
        render_approval_policy,
    )
    from tests.test_proposals.test_approval_policy import _activate as _activate_policy
    from tests.test_proposals.test_approval_policy import _submit_tree
    from tests.test_proposals.test_proposals import DOCUMENT_PATH, _shell

    instance, _owner = initialize_local(root)
    tightening = _submit_tree(
        instance,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            APPROVAL_POLICY_PATH: render_approval_policy(
                ApprovalPolicy(mode="independent_approval_required")
            ),
        },
        name="closed-loop-tighten",
    )
    assert _activate_policy(instance, tightening, tmp_path=root).status == "accepted"
    body = instance.store_document_body(b"# Independent review\n")
    governed = _submit_tree(
        instance,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            DOCUMENT_PATH: render_document(_shell(body.digest)),
        },
        name="closed-loop-governed",
    )
    assert governed.candidate is not None
    proposal_id = governed.admission.proposal_id

    def rows(caller: str | None, request: NextRequest) -> list[object]:
        return [
            item
            for item in service_playbill_next(
                instance, request=request, caller_principal_id=caller
            ).items
            if item.reason == "proposal_awaiting_approval"
        ]

    (row,) = rows("reviewer", _request(instance))
    assert row.subject_identity == proposal_id
    assert row.repair.operation == EXPECTED_OPERATIONS["proposal_awaiting_approval"]
    assert row.repair.command == (f"cruxible proposal approve {proposal_id} --signer-id reviewer")
    assert row.detail["actor_id"] == "owner"
    assert row.detail["minimum_distinct_signers"] == 1
    # The creator cannot approve its own candidate, an unregistered caller has
    # no approval to give, and an unattributed request names no caller at all.
    assert rows("owner", _request(instance)) == []
    assert rows("stranger", _request(instance)) == []
    assert rows(None, _request(instance)) == []
    hidden = NextRequest(
        at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        evaluation_time=EVALUATION_TIME,
        access_profile=CoverageAccessProfile(
            profile_id="next-closed-loop-public", permitted_access_classes=("public",)
        ),
    )
    assert rows("reviewer", hidden) == []
    # A delta is scoped to its caller: another principal diffing against the
    # reviewer's queue gets its own whole queue, never the reviewer's row back
    # as a removal.
    reviewer_queue = service_playbill_next(
        instance, request=_request(instance), caller_principal_id="reviewer"
    )
    other = service_playbill_next(
        instance,
        request=_request(instance).model_copy(
            update={"since_result_digest": reviewer_queue.result_digest}
        ),
        caller_principal_id="owner",
    )
    assert other.delta_since is None
    assert all(item.reason != "proposal_awaiting_approval" for item in other.items)

    signed = _sign(
        client_material(root, instance),
        governed.candidate.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=proposal_id,
        attestation=signed.attestation,
        authenticated_submitter="reviewer",
    )
    assert rows("reviewer", _request(instance)) == []


def _proposal_awaiting_activation(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    """The author's own proposal is listed once activation is all it waits on (C4)."""

    from cruxible_client.contracts.approval_policy import (
        APPROVAL_POLICY_PATH,
        ApprovalPolicy,
        render_approval_policy,
    )
    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
    from tests.test_proposals.test_approval_policy import _activate as _activate_policy
    from tests.test_proposals.test_approval_policy import _submit_tree
    from tests.test_proposals.test_proposals import DOCUMENT_PATH, _shell

    instance, _owner = initialize_local(root)

    def rows(caller: str | None) -> list[object]:
        return [
            item
            for item in service_playbill_next(
                instance, request=_request(instance), caller_principal_id=caller
            ).items
            if item.reason == "proposal_awaiting_activation"
        ]

    # Self-approval: the author's proposal needs nothing but activation.
    body = instance.store_document_body(b"# Ready to activate\n")
    ready = _submit_tree(
        instance,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            DOCUMENT_PATH: render_document(_shell(body.digest)),
        },
        name="closed-loop-ready",
    )
    assert ready.candidate is not None
    proposal_id = ready.admission.proposal_id
    (row,) = rows("owner")
    assert row.subject_identity == proposal_id
    assert row.repair.operation == EXPECTED_OPERATIONS["proposal_awaiting_activation"]
    assert row.repair.command == f"cruxible proposal activate {proposal_id}"
    # Only its author is told; nobody else, and no unattributed request.
    assert rows("reviewer") == []
    assert rows(None) == []

    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"
    assert rows("owner") == []

    # Independent approval: the row waits until another signer has approved.
    tightening = _submit_tree(
        instance,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            APPROVAL_POLICY_PATH: render_approval_policy(
                ApprovalPolicy(mode="independent_approval_required")
            ),
        },
        name="closed-loop-activation-tighten",
    )
    assert _activate_policy(instance, tightening, tmp_path=root).status == "accepted"
    later = instance.store_document_body(b"# Needs review first\n")
    governed = _submit_tree(
        instance,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            "documents/playbill-review.json": render_document(
                _shell(later.digest).model_copy(update={"identity": "document:playbill-review"})
            ),
        },
        name="closed-loop-activation-governed",
    )
    assert governed.candidate is not None
    assert rows("owner") == []
    signed = _sign(
        client_material(root, instance),
        governed.candidate.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=governed.admission.proposal_id,
        attestation=signed.attestation,
        authenticated_submitter="reviewer",
    )
    (approved,) = rows("owner")
    assert approved.subject_identity == governed.admission.proposal_id
    assert approved.detail["approvals"] == 1


def _mandate_expiring(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    from cruxible_client.contracts.procedure_mandates import (
        procedure_mandate_digest,
        procedure_mandate_path,
        render_procedure_mandate,
    )
    from cruxible_client.contracts.procedures.artifacts import render_procedure
    from cruxible_client.contracts.procedures.line_specs import render_line_spec
    from tests.test_indexes.test_resolution_contracts import _accept_tree
    from tests.test_procedures.test_procedure_mandates import _line, _mandate, _procedure

    instance, owner = initialize_local(root)
    procedure = _procedure()
    line = _line(procedure=procedure)
    lapsing = _mandate(line=line).model_copy(
        update={"expires_at": datetime(2026, 8, 27, tzinfo=UTC)}
    )
    _accept_tree(
        instance,
        owner,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            procedure.path: render_procedure(authored_procedure(procedure)),
            f"lines/{line.identity.name}.json": render_line_spec(line),
            procedure_mandate_path("triage"): render_procedure_mandate(lapsing),
        },
        timestamp="2026-08-24T17:00:00.000000Z",
        proposal_name="closed-loop-mandate",
    )
    # Outside the lead time the lapse is not yet work.
    _assert_gone(instance, "mandate_expiring", _request(instance, expiring_within=86_400_000_000))
    row = _row(instance, "mandate_expiring", _request(instance))
    assert row.subject_identity == "ProcedureMandate:triage"
    assert row.repair.operation == EXPECTED_OPERATIONS["mandate_expiring"]
    assert row.repair.command == "cruxible authoring example procedure-mandate"
    assert datetime.fromisoformat(row.detail["expires_at"]) == lapsing.expires_at
    hidden = NextRequest(
        at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        evaluation_time=EVALUATION_TIME,
        access_profile=CoverageAccessProfile(
            profile_id="next-closed-loop-public", permitted_access_classes=("public",)
        ),
    )
    assert all(
        item.reason != "mandate_expiring"
        for item in service_playbill_next(instance, request=hidden).items
    )

    renewed = lapsing.model_copy(
        update={
            "expires_at": datetime(2027, 8, 27, tzinfo=UTC),
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=procedure_mandate_digest(lapsing).tagged
            ),
        }
    )
    _accept_tree(
        instance,
        owner,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            procedure_mandate_path("triage"): render_procedure_mandate(renewed),
        },
        timestamp="2026-08-24T17:00:01.000000Z",
        proposal_name="closed-loop-mandate-renewal",
    )
    _assert_gone(instance, "mandate_expiring", _request(instance))


def _subject_kind_undeclared(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import cruxible_core.proposals.proposals as proposals_module
    from cruxible_client.contracts.authoring.inputs import (
        SubjectKindInput,
        lower_authoring_input,
    )
    from cruxible_client.contracts.subjects import render_subject, subject_path
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
    from tests.core_support._identity_support import shell

    instance, _owner = initialize_local(root)
    # A Subject accepted before Subject identity existed (an older build).
    monkeypatch.setattr(proposals_module, "candidate_identity_issues", lambda *_a, **_k: ())
    base = instance.accepted_coordinate()
    raw = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/pre-subject-kinds", proposed_base_oid=base.git_oid
        ),
        candidate_tree={
            **instance.tree_at(base.git_oid),
            subject_path("dev.legacy", "old"): render_subject(shell("old", "dev.legacy")),
        },
        timestamp="2026-08-24T17:00:00.000000Z",
    )
    assert raw.candidate is not None, raw.evaluation.diagnostics
    service_activate_playbill_proposal(
        instance, proposal_id=raw.admission.proposal_id, activated_by="owner"
    )
    monkeypatch.undo()

    row = _row(instance, "subject_kind_undeclared", _request(instance))
    assert row.subject_identity == "Subject:dev.legacy/old"
    assert row.repair.operation == EXPECTED_OPERATIONS["subject_kind_undeclared"]
    assert row.repair.command == "cruxible authoring example subject-kind"

    submitted = AuthoringIntentCoordinator.for_instance(instance).compile_and_submit(
        actor=AuthenticatedActor(actor_id="owner"),
        payload=lower_authoring_input(
            SubjectKindInput(kind="subject_kind", name="dev.legacy", minted=True)
        ),
        canonical_timestamp="2026-08-24T17:00:01.000000Z",
    )
    assert submitted.status.proposal_id is not None, submitted.intent.last_preflight
    service_activate_playbill_proposal(
        instance, proposal_id=submitted.status.proposal_id, activated_by="owner"
    )
    _assert_gone(instance, "subject_kind_undeclared", _request(instance))


def _line_settled_prior_refusals(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import shlex

    from tests.test_procedures import test_procedure_settlement_delivery as settle

    instance, _root, line, second = settle.settled_refusal_world(root)
    row = _row(instance, "line_settled_prior_refusals", _request(instance))
    assert row.subject_identity == line.identity.qualified
    assert row.related_identities == (f"ProcedureRun:{second.run_id}",)
    assert row.repair.operation == EXPECTED_OPERATIONS["line_settled_prior_refusals"]
    payload = row.repair.arguments["payload"]
    assert (payload["kind"], payload["name"], payload["max_authority"]) == (
        "line",
        line.identity.name,
        "propose",
    )
    # The command carries the whole payload: the here-string is the payload.
    head, here = row.repair.command.split(" <<< ", 1)
    assert head == "cruxible authoring submit -"
    assert json.loads(shlex.split(here)[0]) == payload
    mcp = _row(
        instance,
        "line_settled_prior_refusals",
        _request(instance).model_copy(update={"caller_surface": "mcp"}),
    )
    assert mcp.repair.command == f"cruxible_authoring_submit(payload={json.dumps(payload)})"

    # The repair, run as emitted: the successor's count starts empty.
    settle.submit_line_repair(instance, row)
    _assert_gone(instance, "line_settled_prior_refusals", _request(instance))


def _evidence_unavailable(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import timedelta

    from tests.test_consumers.test_evidence_availability import _drain, _world

    instance, capture = _world(root)
    path = instance.body_store()._path(capture)
    original = path.read_bytes()
    _drain(instance, now=EVALUATION_TIME)
    path.unlink()
    # Out-of-band deletion: the landing log still has the body present at a read's
    # position, and a read never re-checks the file, so next still answers.
    assert all(
        item.reason != "evidence_unavailable"
        for item in service_playbill_next(instance, request=_request(instance)).items
    )
    # The evidence sweep comes due (its daily trigger fires in the drain) and finds
    # it; a derivation that needs the bytes meanwhile reads them as unavailable.
    _drain(instance, now=EVALUATION_TIME + timedelta(days=1))

    row = _row(instance, "evidence_unavailable", _request(instance))
    assert row.subject_identity == f"Capture:{capture}"
    assert row.detail["part"] == "envelope" and row.detail["state"] == "missing"
    assert row.related_identities and all(
        item.startswith("Claim:") for item in row.related_identities
    )
    assert row.repair.operation == EXPECTED_OPERATIONS["evidence_unavailable"]
    hidden = NextRequest(
        at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        evaluation_time=EVALUATION_TIME,
        access_profile=CoverageAccessProfile(
            profile_id="next-closed-loop-public", permitted_access_classes=("public",)
        ),
    )
    assert all(
        item.reason != "evidence_unavailable"
        for item in service_playbill_next(instance, request=hidden).items
    )

    # The named repair: restore the bytes; the worker's next sweep clears the row.
    path.write_bytes(original)
    _drain(instance, now=EVALUATION_TIME + timedelta(days=2))
    _assert_gone(instance, "evidence_unavailable", _request(instance))


def _claim_evidence_erased(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import timedelta

    from tests.support.capture_erasure_world import erasure_world, rebind, rescan
    from tests.test_consumers.test_evidence_availability import _drain
    from tests.test_procedures.test_procedure_source_runs import NOW as WORLD_NOW
    from tests.test_service.test_capture_erasure import _erase

    world = erasure_world(root)
    instance = world.instance

    def request() -> NextRequest:
        return _request(instance, evaluation_time=WORLD_NOW + timedelta(hours=1))

    _drain(instance, now=WORLD_NOW)
    _erase(world, dry_run=False, at=_erase(world).coordinate.digest)
    _drain(instance, now=WORLD_NOW + timedelta(minutes=1))

    row = _row(instance, "claim_evidence_erased", request())
    assert row.subject_identity == f"Claim:{world.claim}"
    assert row.related_identities == (f"Capture:{world.capture}",)
    assert row.severity == "warning"
    assert row.repair.operation == EXPECTED_OPERATIONS["claim_evidence_erased"]
    assert row.repair.command.startswith(f"cruxible rebind {world.claim} --citation ")

    # The named repair, with the fresh observation the re-binder chooses.
    assert rebind(instance, world.claim, rescan(world)).refusal is None
    _drain(instance, now=WORLD_NOW + timedelta(minutes=2))
    _assert_gone(instance, "claim_evidence_erased", request())


def _consumer_stalled(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import timedelta

    from cruxible_core.runtime.line_arms import dispatch_armed_line
    from cruxible_core.service.procedures.line_dispatch import armed_work, service_enable_line
    from tests.test_procedures import test_line_arming as arming

    instance, line, procedure, start = arming._armed_world(root, principal=arming.CREDENTIAL)
    instance_id = instance.descriptor.instance_id
    arming._credential_store(
        monkeypatch,
        arming._credential(instance_id=instance_id, revoked_at="2026-09-02T00:00:00Z"),
    )
    arming.capture(instance, procedure, at=start + timedelta(seconds=1))
    arming._match(instance, start + timedelta(seconds=2))
    (arm,) = armed_work(instance, now=start + timedelta(seconds=2))
    assert (
        dispatch_armed_line(
            arming._manager(instance), instance_id, arm, now=start + timedelta(seconds=4)
        )
        is None
    )
    request = _request(instance)

    row = _row(instance, "consumer_stalled", request)
    assert row.subject_identity == line.identity.qualified
    assert row.detail["stop_reason"] == "credential_revoked"
    assert row.repair.command == f"cruxible line enable {line.identity.name}"

    # The named repair: enable again under a credential that holds.
    service_enable_line(
        instance,
        line.identity.name,
        principal=arming.LOCAL,
        actor=arming._actor(instance),
        now=start + timedelta(seconds=5),
        daemon_id="daemon",
    )
    _assert_gone(instance, "consumer_stalled", request)


def _line_disabled_by_upgrade(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cruxible_client.contracts.procedures.artifacts import (
        parse_procedure,
        procedure_artifact_digest,
    )
    from cruxible_client.contracts.procedures.line_specs import line_spec_digest, parse_line_spec
    from cruxible_client.contracts.provider_installation import ProviderMoveRequest
    from cruxible_client.contracts.triggers import CadenceSchedule
    from tests.test_server.test_kit_providers import (
        PROCEDURE,
        _author,
        _client,
        _install,
        _package,
        _tree,
        _worlds,
    )

    opened = _worlds(root, monkeypatch, historical=True)
    world, _consumer = next(opened)
    try:
        checkout = _PROVIDER_CHECKOUT.get()
        first = _package(root / "before", checkout, name="kit-call", increment=1)
        _install(world, first)
        _author(world)
        intent = (
            world.pb.changes(rationale="Exercise the upgrade repair for an enabled Line.")
            .line(
                name="user.upgrade",
                procedure=PROCEDURE,
                parameters={"n": 2},
                max_authority="observe",
            )
            .trigger(
                name="user.upgrade-clock",
                line="user.upgrade",
                schedule=CadenceSchedule(interval_seconds=3600),
            )
            .prepare()
        )
        assert not intent.refused, intent.diagnostics
        submitted = intent.submit(accept="never")
        world.approve(submitted._candidate_status.proposal_id)
        client = _client(world.http)
        original = client.enable_line(world.instance_id, "user.upgrade")
        assert original.state == "enabled"
        instance = world.instance
        _assert_gone(instance, "line_disabled_by_upgrade", _request(instance))

        second = _package(root / "after", checkout, name="kit-call", increment=2)
        _install(world, second)
        moved = client.move_provider(
            world.instance_id, ProviderMoveRequest(provider="kit-call", dry_run=False)
        )
        assert moved.status == "accepted", moved
        assert any(
            item.identity == "Line:user.upgrade" and item.enablement == "disabled"
            for item in moved.automation_carries
        )
        tree = _tree(world)
        path = "lines/user.upgrade.json"
        upgraded = parse_line_spec(tree[path], path=path)
        procedure_path = f"procedures/{PROCEDURE}.json"
        procedure = parse_procedure(tree[procedure_path], path=procedure_path)
        assert procedure.lifecycle.predecessor_digest is not None
        assert upgraded.lifecycle.predecessor_digest == original.line_artifact_digest
        assert upgraded.procedure.artifact_digest == procedure_artifact_digest(procedure).tagged
        assert upgraded.procedure.artifact_digest != procedure.lifecycle.predecessor_digest

        request = _request(instance)
        row = _row(instance, "line_disabled_by_upgrade", request)
        assert row.subject_identity == upgraded.identity.qualified
        assert row.detail["procedure_version"] == upgraded.procedure.artifact_digest
        assert row.repair.operation == EXPECTED_OPERATIONS["line_disabled_by_upgrade"]
        assert row.repair.command == "cruxible line enable user.upgrade"

        # The named repair runs as the same authenticated operator that enabled it.
        enabled = client.enable_line(world.instance_id, row.repair.arguments["line"])
        assert enabled.state == "enabled"
        assert enabled.enabled_by == original.enabled_by
        assert enabled.line_artifact_digest == line_spec_digest(upgraded).tagged
        _assert_gone(instance, "line_disabled_by_upgrade", request)
    finally:
        for _ in opened:
            pass


def _restarted_line(root: Path):  # type: ignore[no-untyped-def]
    """An enabled Line whose daemon restarted after matching one event, missing another."""

    from datetime import timedelta

    from tests.test_procedures import test_line_arming as arming

    instance, line, procedure, start = arming._armed_world(root)
    arming.capture(instance, procedure, at=start + timedelta(seconds=1))
    arming._match(instance, start + timedelta(seconds=2))
    arming.capture(instance, procedure, at=start + timedelta(seconds=3))  # daemon down
    restarted = start + timedelta(seconds=10)
    arming._match(instance, restarted, daemon_id="restarted")
    return instance, line, start, restarted


def _line_coverage_gap(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import timedelta

    from cruxible_client.contracts.line_dispatch import LineEvaluateRequest
    from cruxible_core.service.procedures.line_dispatch import service_evaluate_line
    from tests.test_procedures import test_line_arming as arming

    instance, line, start, restarted = _restarted_line(root)
    request = _request(instance)

    row = _row(instance, "line_coverage_gap", request)
    assert row.subject_identity == line.identity.qualified
    since, until = row.repair.arguments["since"], row.repair.arguments["until"]
    assert datetime.fromisoformat(until) == restarted
    assert start + timedelta(seconds=2) <= datetime.fromisoformat(since) < restarted
    assert row.repair.command == (
        f"cruxible line evaluate {line.identity.name} --since {since} --until {until} --dispatch"
    )

    # The named repair: evaluate exactly that range; nothing runs.
    service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(
            since=datetime.fromisoformat(since), until=datetime.fromisoformat(until)
        ),
        actor=arming._actor(instance),
        now=restarted + timedelta(seconds=5),
    )
    _assert_gone(instance, "line_coverage_gap", request)


def _line_work_pending(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import timedelta

    from cruxible_client.contracts.line_dispatch import LineDispatchRequest
    from cruxible_core.service.procedures.line_dispatch import service_dispatch_line
    from tests.test_procedures import test_line_arming as arming

    instance, line, _start, restarted = _restarted_line(root)
    request = _request(instance)

    row = _row(instance, "line_work_pending", request)
    assert row.subject_identity == line.identity.qualified and row.detail["due"] == 1
    assert row.repair.command == f"cruxible line dispatch {line.identity.name}"

    # The named repair: dispatch the Line's pending work.
    result = service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequest(),
        actor=arming._actor(instance),
        now=restarted + timedelta(seconds=5),
        caller_rung=3,
    )
    assert [item.status for item in result.items] == ["admitted"]
    _assert_gone(instance, "line_work_pending", request)


def _line_not_enabled(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    from cruxible_client.contracts.triggers import CaptureLandingSchedule
    from cruxible_core.service.procedures.line_dispatch import service_enable_line
    from tests.test_procedures import test_line_arming as arming
    from tests.test_procedures.test_line_triggers import SELECTOR, line_world

    instance, line, _procedure = line_world(root, CaptureLandingSchedule(event=SELECTOR))
    request = _request(instance)

    row = _row(instance, "line_not_enabled", request)
    # A Line nobody enabled yet is a choice, not a problem: setup, since the
    # generation that accepted it.
    assert row.subject_identity == line.identity.qualified and row.severity == "setup"
    assert row.since == format_datetime(
        instance.accepted_evaluation_time(_first_acceptance_oid(instance, line.identity.qualified))
    )
    assert row.repair.command == f"cruxible line enable {line.identity.name}"

    # The named repair: enable it. A later disable owes nothing: it was deliberate.
    service_enable_line(
        instance,
        line.identity.name,
        principal=arming.LOCAL,
        actor=arming._actor(instance),
        now=arming.READ_TIME,
        daemon_id="daemon",
    )
    _assert_gone(instance, "line_not_enabled", request)


def _line_run_failed(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import timedelta

    from cruxible_client.contracts.line_dispatch import LineDispatchRequest, LineEvaluateRequest
    from cruxible_core.procedures import execution
    from cruxible_core.service.procedures.line_dispatch import (
        service_dispatch_line,
        service_evaluate_line,
    )
    from tests.test_procedures import test_line_arming as arming

    instance, line, _start, restarted = _restarted_line(root)
    request = _request(instance)

    def failing(*_args: object, **_kwargs: object) -> object:
        raise execution._OperationalFailure("provider_execution_error", details={"kind": "dns"})

    def dispatch(at):  # type: ignore[no-untyped-def]
        return service_dispatch_line(
            instance,
            line.identity.name,
            LineDispatchRequest(),
            actor=arming._actor(instance),
            now=at,
            caller_rung=3,
        )

    with monkeypatch.context() as patched:
        patched.setattr(execution.ProcedureExecutor, "_walk", failing)
        assert [item.status for item in dispatch(restarted).items] == ["admitted"]
    row = _row(instance, "line_run_failed", request)
    assert row.subject_identity == line.identity.qualified
    assert row.detail["failure_code"] == "provider_execution_error"
    assert row.repair.operation == "hand_edit" and row.repair.command is None

    # The repair: the cause is fixed (here the failure is gone); the Line's
    # next run of a Trigger's occurrence succeeds and clears the row.
    gap = _row(instance, "line_coverage_gap", request)
    since, until = gap.repair.arguments["since"], gap.repair.arguments["until"]
    service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(
            since=datetime.fromisoformat(since), until=datetime.fromisoformat(until)
        ),
        actor=arming._actor(instance),
        now=restarted + timedelta(seconds=5),
    )
    assert [item.status for item in dispatch(restarted + timedelta(seconds=6)).items] == [
        "admitted"
    ]
    _assert_gone(instance, "line_run_failed", request)

    # A manual run's failure is the caller's to see, never a row.
    _line_manual_failure_is_no_row(instance, line, monkeypatch, request)


def _line_manual_failure_is_no_row(instance, line, monkeypatch, request) -> None:  # type: ignore[no-untyped-def]
    from cruxible_core.procedures import execution
    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequest,
        service_run_playbill_line,
    )
    from tests.test_procedures import test_line_arming as arming

    def failing(*_args: object, **_kwargs: object) -> object:
        raise execution._OperationalFailure("provider_execution_error")

    with monkeypatch.context() as patched:
        patched.setattr(execution.ProcedureExecutor, "_walk", failing)
        try:
            service_run_playbill_line(
                instance,
                path_identity_digest=line.identity.name,
                request=LineRunRequest(line=line.identity.name),
                actor_context=arming._actor(instance),
                caller_rung=3,
            )
        except Exception:  # noqa: BLE001 - a refused manual run is no row either
            pass
    _assert_gone(instance, "line_run_failed", request)


def _line_inbox_unread(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    # This case exercises inbox queue repairs, independent of source decoding.
    # Keep it on the current authored graph and exact Line fixture.
    from cruxible_client.contracts.procedures.artifacts import (
        AcceptedProcedure,
        procedure_artifact_digest,
        render_procedure,
    )
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
    from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
    from cruxible_client.contracts.procedures.models import InboxEgressNode
    from cruxible_core.service.procedures.line_inbox import (
        service_acknowledge_inbox_item,
        service_dismiss_inbox_item,
        service_list_inbox,
    )
    from tests.test_indexes.test_resolution_contracts import _accept_tree
    from tests.test_procedures.test_manual_line_runs import manual_run
    from tests.test_procedures.test_procedure_run_surface import _slotless_procedure
    from tests.test_procedures.test_procedure_source_runs import NOW, _actor
    from tests.test_server.test_playbill_line_run_refusals import _served_line

    instance, owner = initialize_local(root)
    template = _slotless_procedure("next-inbox").procedure
    (shape,) = template.definition.nodes
    definition = template.definition.model_copy(
        update={
            "nodes": (
                shape.model_copy(update={"next": "inbox"}),
                InboxEgressNode(
                    node_id="inbox",
                    input={
                        "items": [
                            {"cve": "CVE-2026-0001"},
                            {"cve": "CVE-2026-0002"},
                            {"cve": "CVE-2026-0003"},
                        ]
                    },
                ),
            )
        }
    )
    procedure = template.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
        }
    )
    accepted = AcceptedProcedure(
        path="procedures/next-inbox.json",
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    line = _served_line("next-inbox", accepted=accepted).model_copy(
        update={"max_authority": "observe"}
    )
    _accept_tree(
        instance,
        owner,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            accepted.path: render_procedure(procedure),
            line_spec_path(line.identity.name): render_line_spec(line),
        },
        timestamp="2026-08-21T12:00:00.000000Z",
        proposal_name="next-inbox",
    )
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    assert manual_run(instance, line, at=NOW, root=workspace).status == "succeeded"
    request = _request(instance)

    row = _row(instance, "line_inbox_unread", request)
    assert row.subject_identity == line.identity.qualified and row.detail["unread"] == 3
    assert row.repair.command == (
        f"cruxible inbox list --line {line.identity.name} --status unread"
    )

    # The named repair lists exactly the Line's unread items; judging each clears the row.
    listed = service_list_inbox(instance, line=line.identity.name, status="unread", limit=50)
    assert len(listed.entries) == 3
    first, *rest = (entry.item.item_id for entry in listed.entries)
    service_dismiss_inbox_item(
        instance, item=first, reason="noise", actor_context=_actor(instance), decided_at=NOW
    )
    assert _row(instance, "line_inbox_unread", request).detail["unread"] == 2
    for item in rest:
        service_acknowledge_inbox_item(
            instance, item=item, actor_context=_actor(instance), decided_at=NOW
        )
    _assert_gone(instance, "line_inbox_unread", request)


def _prediction_settleable(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import timedelta

    from cruxible_client.contracts.predictions import SettleRequest
    from cruxible_core.service.procedures.predictions import PredictionRefused
    from tests.test_consumers import test_prediction_settlement as worker

    instance, owner, capture, contract = worker.fixed_world(root)
    worker.drain(instance, now=worker.FIXED_CLOSES)

    row = _row(
        instance,
        "prediction_settleable",
        _request(instance).model_copy(update={"evaluation_time": worker.FIXED_CLOSES}),
    )
    assert row.subject_identity == contract.identity.qualified
    assert row.related_identities and row.related_identities[0].startswith("Claim:")
    assert row.detail["anchor_event"] is None and row.detail["bound_contract_id"]
    assert row.repair.operation == EXPECTED_OPERATIONS["prediction_settleable"]
    hidden = NextRequest(
        at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        evaluation_time=worker.FIXED_CLOSES,
        access_profile=CoverageAccessProfile(
            profile_id="next-closed-loop-public", permitted_access_classes=("public",)
        ),
    )
    assert all(
        item.reason != "prediction_settleable"
        for item in service_playbill_next(instance, request=hidden).items
    )

    # The named repair: a short command naming the bound window; the daemon
    # resolves its exact contract and window, and the settler adds only the
    # observation's Claim ID.
    bound = row.detail["bound_contract_id"]
    assert row.repair.command == f"cruxible prediction settle {bound}"
    observation = worker.observe(instance, owner, capture, at="2026-09-02T12:02:00.000000Z")
    filled = SettleRequest(observation=observation.identity.name)
    # A window id the worker does not hold names no prediction.
    with pytest.raises(PredictionRefused, match="No bound prediction window") as refused:
        worker.served.service_settle_playbill_prediction(
            instance,
            prediction_id="RSC-" + "0" * 32,
            request=filled,
            actor_context=worker.served._actor(),
            recorded_at=worker.FIXED_CLOSES + timedelta(minutes=1),
        )
    assert refused.value.code == "prediction_window_unknown"
    worker.served.service_settle_playbill_prediction(
        instance,
        prediction_id=bound,
        request=filled,
        actor_context=worker.served._actor(),
        recorded_at=worker.FIXED_CLOSES + timedelta(minutes=1),
    )
    worker.drain(instance, now=worker.FIXED_CLOSES + timedelta(minutes=1))
    _assert_gone(
        instance,
        "prediction_settleable",
        _request(instance).model_copy(update={"evaluation_time": worker.FIXED_CLOSES}),
    )


def _prediction_window_unbindable(root: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import timedelta

    from tests.test_consumers import test_prediction_settlement as worker

    instance, event, restore = worker.unbindable_world(root)
    now = worker.served.PREDICTED_AT + timedelta(minutes=5)
    worker.drain(instance, now=now)

    row = _row(instance, "prediction_window_unbindable", _request(instance))
    assert row.subject_identity == "ResolutionContract:event-test"
    assert row.detail["anchor_event"] == event.model_dump(mode="json")
    assert row.detail["code"] == "trigger_capture_unavailable"
    assert row.repair.operation == EXPECTED_OPERATIONS["prediction_window_unbindable"]
    hidden = NextRequest(
        at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        evaluation_time=EVALUATION_TIME,
        access_profile=CoverageAccessProfile(
            profile_id="next-closed-loop-public", permitted_access_classes=("public",)
        ),
    )
    assert all(
        item.reason != "prediction_window_unbindable"
        for item in service_playbill_next(instance, request=hidden).items
    )

    # The named repair: restore the anchor's material; the worker's retry binds it.
    restore()
    from tests.support.internal_triggers import fire_internal_triggers

    retry_at = now + timedelta(hours=1)
    fire_internal_triggers(instance, now=retry_at)
    worker.drain(instance, now=retry_at)
    _assert_gone(instance, "prediction_window_unbindable", _request(instance))


#: Provider upgrade and kit resume cases need the session-scoped ``provider_checkout`` fixture,
#: which the ``RepairCase`` signature has no room for; the test fetches it lazily
#: (``request.getfixturevalue``, only for these cases) and hands it over here,
#: so every other case's run pays nothing towards building the provider wheels.
_PROVIDER_CHECKOUT: ContextVar[Any] = ContextVar("next_closed_loop_provider_checkout")


def _kit_resume_blocked(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A recorded kit add resume whose head moved out from under it (a downgrade):

    next names the stuck resume's repair, and a commit kit add at the live
    release -- running that repair -- replaces the stale record and clears the
    row.
    """

    from cruxible_client.contracts.kits import KitAddRequest
    from cruxible_client.kits import stage_kit_providers
    from cruxible_core.runtime.playbill_manager import get_playbill_manager
    from cruxible_core.service import kit_resume
    from tests.test_server.test_kit_providers import (
        PROCEDURE,
        _add,
        _build,
        _client,
        _install,
        _package,
        _tree,
    )
    from tests.test_server.test_kit_resume import _open_worlds, _release, _resumes

    provider_checkout = _PROVIDER_CHECKOUT.get()
    opened = _open_worlds(root, monkeypatch, independent=False)
    publisher, consumer = next(opened)
    try:
        bundled = _package(root / "kit-source", provider_checkout, name="kit-call")
        release = _release(publisher, bundled)
        older = _build(publisher, bundled, version="0.9.0")
        instance = get_playbill_manager().get(consumer.instance_id)

        # The install is already live here, as after a kit add that landed.
        _install(consumer, bundled)
        staged = stage_kit_providers(_client(consumer.http), consumer.instance_id, release)
        kit_resume.record_kit_resume(
            instance,
            KitAddRequest(bundle=staged, source="test"),
            actor_id="operator",
            waiting_on=staged.manifest.providers,
        )
        kit_resume.resume_kits_after_activation(
            instance, (f"providers/{release.manifest.providers[0].build}.json",)
        )
        # No approval round to run here: the resume's own commit lands at
        # once, the same self-resolving activation ``_add`` gets directly.
        (resumed,) = _resumes(consumer)
        assert resumed.state == "proposed" and resumed.proposal_id is not None
        assert f"procedures/{PROCEDURE}.json" in _tree(consumer)

        # A stale recorded resume for an older release: its head no longer
        # agrees with what landed, so the resume blocks instead of applying.
        older_staged = stage_kit_providers(_client(consumer.http), consumer.instance_id, older)
        kit_resume.record_kit_resume(
            instance,
            KitAddRequest(bundle=older_staged, source="test"),
            actor_id="operator",
            waiting_on=older_staged.manifest.providers,
        )
        kit_resume.resume_kits_after_activation(
            instance, (f"providers/{release.manifest.providers[0].build}.json",)
        )
        (blocked,) = _resumes(consumer)
        assert blocked.state == "blocked" and "older than installed" in (blocked.detail or "")

        request = _request(instance)
        row = _row(instance, "kit_resume_blocked", request)
        assert row.subject_identity == "kit:acme"
        assert row.detail["detail"] == blocked.detail
        assert row.repair.operation == EXPECTED_OPERATIONS["kit_resume_blocked"]

        # The named repair: a commit kit add at the live release replaces the
        # stale record outright.
        assert _add(consumer, release, dry_run=False).status == "unchanged"
        assert _resumes(consumer) == ()
        _assert_gone(instance, "kit_resume_blocked", _request(instance))
    finally:
        for _ in opened:
            pass


CLOSED_LOOP_CASES: dict[ClosedLoopKey, RepairCase] = {
    ("claim_conflicted", None): _claim_conflicted,
    ("claim_uncovered", None): _claim_uncovered,
    ("claim_stale_evidence", None): _claim_stale_evidence,
    ("citation_drifted", "changed"): _citation_drifted_changed,
    ("citation_drifted", "gone"): _citation_drifted_gone,
    ("citation_drifted", "ambiguous"): _citation_drifted_ambiguous,
    ("citation_source_unobserved", None): _citation_source_unobserved,
    ("evidence_expiring", None): _evidence_expiring,
    ("floor_invalid", None): _floor_invalid,
    ("projection_dirty", None): _projection_dirty,
    ("projection_backing_stale", "revised"): _projection_backing_stale_revised,
    ("projection_backing_stale", "retired"): _projection_backing_stale_retired,
    ("projection_backing_stale", "exhausted"): _projection_backing_stale_exhausted,
    ("projection_marker_invalid", "registered_marker_missing"): _projection_marker_missing,
    ("projection_marker_invalid", None): _projection_marker_invalid,
    ("claim_dependency_stale", None): _claim_dependency_stale,
    ("claim_attestation_threshold_met", None): _claim_attestation_threshold,
    ("claim_contradicting_evidence_available", None): lambda root, monkeypatch: (
        _claim_attestation_door(
            root,
            monkeypatch,
            stance="contradict",
            reason="claim_contradicting_evidence_available",
        )
    ),
    ("claim_new_evidence_supporting", None): lambda root, monkeypatch: _claim_attestation_door(
        root,
        monkeypatch,
        stance="support",
        reason="claim_new_evidence_supporting",
    ),
    ("claim_new_evidence_unreviewed", None): lambda root, monkeypatch: _claim_attestation_door(
        root,
        monkeypatch,
        stance="unsure",
        reason="claim_new_evidence_unreviewed",
    ),
    ("document_modified", None): _document_modified,
    ("workspace_binding_missing", None): _workspace_binding_missing,
    ("unregistered_projection_block", None): _unregistered_projection_block,
    ("proposal_stale", None): _proposal_stale,
    ("proposal_awaiting_approval", None): _proposal_awaiting_approval,
    ("proposal_awaiting_activation", None): _proposal_awaiting_activation,
    ("mandate_expiring", None): _mandate_expiring,
    ("consumer_stalled", None): _consumer_stalled,
    ("line_disabled_by_upgrade", None): _line_disabled_by_upgrade,
    ("line_not_enabled", None): _line_not_enabled,
    ("line_run_failed", None): _line_run_failed,
    ("line_coverage_gap", None): _line_coverage_gap,
    ("line_work_pending", None): _line_work_pending,
    ("line_settled_prior_refusals", None): _line_settled_prior_refusals,
    ("line_inbox_unread", None): _line_inbox_unread,
    ("subject_kind_undeclared", None): _subject_kind_undeclared,
    ("evidence_unavailable", None): _evidence_unavailable,
    ("claim_evidence_erased", None): _claim_evidence_erased,
    ("prediction_settleable", None): _prediction_settleable,
    ("prediction_window_unbindable", None): _prediction_window_unbindable,
    ("kit_resume_blocked", None): _kit_resume_blocked,
}


@pytest.mark.parametrize(
    "key", tuple(CLOSED_LOOP_CASES), ids=lambda key: "-".join(x for x in key if x)
)
def test_every_next_reason_has_an_effective_named_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    key: ClosedLoopKey,
) -> None:
    reasons = set(get_args(NextReason))
    assert {reason for reason, _discriminator in CLOSED_LOOP_CASES} == reasons
    assert {
        discriminator for reason, discriminator in CLOSED_LOOP_CASES if reason == "citation_drifted"
    } == {"changed", "gone", "ambiguous"}
    # One reason, two repairs: every branch of it is registered, so the pin
    # cannot claim a repair vocabulary only one producer actually has.
    assert {
        discriminator
        for reason, discriminator in CLOSED_LOOP_CASES
        if reason == "projection_backing_stale"
    } == {"revised", "retired", "exhausted"}
    assert set(EXPECTED_OPERATIONS) == reasons

    case_root = tmp_path / "-".join(part for part in key if part is not None)
    case_root.mkdir()
    if key[0] in {"kit_resume_blocked", "line_disabled_by_upgrade"}:
        # These cases need provider wheels; fetched lazily so every
        # other case's run never builds them.
        _PROVIDER_CHECKOUT.set(request.getfixturevalue("provider_checkout"))
    CLOSED_LOOP_CASES[key](case_root, monkeypatch)


def test_next_withholds_the_settle_repair_from_a_caller_who_cannot_settle(tmp_path: Path) -> None:
    """R07, option (b): the row stays; a repair the caller cannot run is withheld."""

    from tests.test_consumers import test_prediction_settlement as worker

    instance, _owner, _capture, _contract = worker.fixed_world(tmp_path)
    worker.drain(instance, now=worker.FIXED_CLOSES)
    request = _request(instance).model_copy(update={"evaluation_time": worker.FIXED_CLOSES})

    def settle_rows(result):  # type: ignore[no-untyped-def]
        return [item for item in result.items if item.reason == "prediction_settleable"]

    governed = service_playbill_next(instance, request=request, caller_rung=1)
    (row,) = settle_rows(governed)
    bound = row.detail["bound_contract_id"]
    assert row.repair.command == f"cruxible prediction settle {bound}"
    assert "hidden" not in governed.status.model_dump(mode="json")

    read_only = service_playbill_next(instance, request=request, caller_rung=0)
    (withheld,) = settle_rows(read_only)
    assert withheld.repair is None
    assert withheld.repair_requires.tool == "cruxible_prediction_settle"
    assert withheld.repair_requires.because == ("tier",)

    mcp = request.model_copy(
        update={
            "caller_surface": "mcp",
            "caller_tools": ("cruxible_next", "cruxible_prediction_settle"),
        }
    )
    (mcp_row,) = settle_rows(service_playbill_next(instance, request=mcp, caller_rung=1))
    assert mcp_row.repair.command == f'cruxible_prediction_settle(prediction_id="{bound}")'

    default_profile = mcp.model_copy(update={"caller_tools": ("cruxible_next",)})
    profiled = service_playbill_next(instance, request=default_profile, caller_rung=1)
    (kept,) = settle_rows(profiled)
    assert kept.repair is None and kept.repair_requires.because == ("profile",)
    assert kept.repair_requires.profile == "full"
    assert profiled.total_items == governed.total_items


def test_next_applies_prediction_window_time_at_read_without_a_worker_tick(tmp_path: Path) -> None:
    from datetime import timedelta

    from tests.test_consumers import test_prediction_settlement as worker

    instance, _owner, _capture, _contract = worker.fixed_world(tmp_path)
    worker.drain(instance, now=worker.served.PREDICTED_AT)
    request = _request(instance)
    before = request.model_copy(
        update={"evaluation_time": worker.FIXED_CLOSES - timedelta(microseconds=1)}
    )
    after = request.model_copy(update={"evaluation_time": worker.FIXED_CLOSES})
    assert all(
        item.reason != "prediction_settleable"
        for item in service_playbill_next(instance, request=before).items
    )
    rows = [
        item
        for item in service_playbill_next(instance, request=after).items
        if item.reason == "prediction_settleable"
    ]
    assert len(rows) == 1
    assert "evaluated_at" not in rows[0].detail
    assert tuple(worker.WORKER.due(instance, now=worker.FIXED_CLOSES)) == ()


def test_prediction_row_identity_stays_stable_after_the_window_closes(tmp_path: Path) -> None:
    from datetime import timedelta

    from tests.test_consumers import test_prediction_settlement as worker

    instance, _owner, _capture, _contract = worker.fixed_world(tmp_path)
    worker.drain(instance, now=worker.served.PREDICTED_AT)
    request = _request(instance)
    items = []
    for instant in (worker.FIXED_CLOSES, worker.FIXED_CLOSES + timedelta(hours=1)):
        result = service_playbill_next(
            instance, request=request.model_copy(update={"evaluation_time": instant})
        )
        (row,) = [item for item in result.items if item.reason == "prediction_settleable"]
        items.append(row)
    assert items[0] == items[1]
    assert items[0].item_id == items[1].item_id
    assert "evaluated_at" not in items[0].detail


def test_unbindable_prediction_row_identity_survives_an_unchanged_retry(tmp_path: Path) -> None:
    from datetime import timedelta

    from tests.support.internal_triggers import fire_internal_triggers
    from tests.test_consumers import test_prediction_settlement as worker

    instance, _event, _restore = worker.unbindable_world(tmp_path)
    worker.drain(instance, now=worker.served.PREDICTED_AT)
    request = _request(instance)
    first = _row(instance, "prediction_window_unbindable", request)
    later = worker.served.PREDICTED_AT + timedelta(hours=1)
    fire_internal_triggers(instance, now=later)
    worker.drain(instance, now=later)
    second = _row(instance, "prediction_window_unbindable", request)
    assert first == second and first.item_id == second.item_id
    assert "evaluated_at" not in second.detail


def test_an_approver_sees_a_stale_proposal_awaiting_its_authors_readmit(tmp_path: Path) -> None:
    """A stale candidate is never approvable; its readmit is the author's alone."""

    from cruxible_client.contracts.approval_policy import (
        APPROVAL_POLICY_PATH,
        ApprovalPolicy,
        render_approval_policy,
    )
    from tests.test_proposals.test_approval_policy import _activate as _activate_policy
    from tests.test_proposals.test_approval_policy import _submit_tree
    from tests.test_proposals.test_proposals import DOCUMENT_PATH, _shell

    instance, _owner = initialize_local(tmp_path)
    tightening = _submit_tree(
        instance,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            APPROVAL_POLICY_PATH: render_approval_policy(
                ApprovalPolicy(mode="independent_approval_required")
            ),
        },
        name="stale-approver-tighten",
    )
    assert _activate_policy(instance, tightening, tmp_path=tmp_path).status == "accepted"
    base = instance.tree_at(instance.accepted_coordinate().git_oid)
    first = instance.store_document_body(b"# First\n")
    second = instance.store_document_body(b"# Second\n")
    overtaken = _submit_tree(
        instance,
        {**base, DOCUMENT_PATH: render_document(_shell(first.digest))},
        name="stale-approver-overtaken",
    )
    winner = _submit_tree(
        instance,
        {**base, DOCUMENT_PATH: render_document(_shell(second.digest, title="Second"))},
        name="stale-approver-winner",
    )
    stale_id = overtaken.admission.proposal_id

    def reviewer_rows(reason: str) -> list[Any]:
        return [
            item
            for item in service_playbill_next(
                instance, request=_request(instance), caller_principal_id="reviewer"
            ).items
            if item.reason == reason and item.subject_identity == stale_id
        ]

    assert len(reviewer_rows("proposal_awaiting_approval")) == 1
    assert _activate_policy(instance, winner, tmp_path=tmp_path, approve=True).status == "accepted"

    # The head moved past it: no approve; stale, awaiting the author's readmit.
    assert reviewer_rows("proposal_awaiting_approval") == []
    (row,) = reviewer_rows("proposal_stale")
    assert row.severity == "warning" and row.repair is None
    assert row.repair_requires.operation == "cruxible.proposal.readmit"
    assert row.repair_requires.because == ("author",)
    assert row.detail["awaiting"] == "author_readmit"
    assert row.detail["actor_id"] == "owner"
    # The author's own row still names the readmit.
    author = _row(instance, "proposal_stale", _request(instance), caller_principal_id="owner")
    assert author.repair.command == f"cruxible proposal readmit {stale_id}"
