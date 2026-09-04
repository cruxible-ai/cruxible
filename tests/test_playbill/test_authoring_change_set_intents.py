"""One authoring intent is one changeset: mixed members, atomicity, replay."""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.authoring.insertions import apply_playbill_publication
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.authoring.inputs import (
    ChangeSetInput,
    ClaimRetirementInput,
    lower_authoring_input,
)
from cruxible_client.contracts.authoring.models import (
    AUTHORING_CHANGE_SET_MEMBERSHIP_DIGEST_DOMAIN,
    AuthoringClaimStatementV1,
    AuthoringDiagnosticV1,
    AuthoringReferenceExpectationV1,
    ChangeSetAuthoringPayloadV1,
    ClaimAuthoringPayloadV1,
    ClaimAuthoringPayloadV2,
    ClaimDependencyDraftsV1,
    ClaimRetirementMemberV1,
    ClaimTypeAuthoringPayloadV1,
    InsertionAnchorWindowV1,
    InsertionConfirmationObservationV2,
    InsertionTargetV2,
    PublicationSourceObservationV2,
    SelfSourceBodyV1,
    SubjectAuthoringPayloadV1,
    WorkingDigestCoordinateV1,
    authoring_change_set_membership,
    authoring_member_identity,
    authoring_payload_digest,
)
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.claim_types import ClaimType, claim_type_path
from cruxible_client.contracts.claims import (
    ClaimFormatError,
    ClaimRetireDependentV1,
    LiteralClaimObject,
    claim_artifact_digest,
    claim_path,
    parse_claim,
)
from cruxible_client.contracts.declared_blocks import parse_projection_blocks
from cruxible_client.contracts.errors import PlaybillExecutionError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.proposal_models import (
    CHANGE_SET_RECORD_BYTES_PER_MEMBER,
    ProposalReceiveLimits,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_path
from cruxible_core.playbill.authoring import preflight as preflight_module
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.insertions import PublicationAnchorStale
from cruxible_core.playbill.authoring.lowering import AuthoringLoweringError, lower_authoring
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.service.playbill_next import (
    PlaybillNextRequestV1,
    PlaybillNextSourceObservationV3,
    PlaybillNextWorkspaceObservationV1,
    service_playbill_next,
)
from cruxible_core.service.playbill_proposal_receive import load_proposal_receive_config
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_authoring_preflight import (
    TIMESTAMP,
    _seed_claim_surface,
)
from tests.test_playbill.test_claims import _claim_type
from tests.test_playbill.test_resolution_contracts import _accept_tree

SUBJECT_KIND = "project.work_item"


def _claim_ids() -> Iterator[str]:
    for index in itertools.count(1):
        yield f"CLM-{index:032x}"


def _coordinator(instance: PlaybillInstance) -> AuthoringIntentCoordinator:
    """A coordinator whose minted Claim IDs are distinct and deterministic."""

    exhaust = instance.root / instance.descriptor.storage.exhaust
    identities = _claim_ids()
    tokens = (f"{index:032x}" for index in itertools.count(1))
    return AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(exhaust, token_factory=lambda: next(tokens)),
        claim_id_factory=lambda: next(identities),
        # Inside the publication expiry window that starts at TIMESTAMP.
        clock=lambda: datetime(2026, 8, 22, 12, tzinfo=UTC),
    )


def _shell(subject_id: str) -> SubjectShell:
    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=f"{SUBJECT_KIND}/{subject_id}"),
        subject_kind=SUBJECT_KIND,
        subject_id=subject_id,
    )


def _predicate_type(predicate: str) -> ClaimType:
    values = _claim_type().model_dump(mode="python")
    values["identity"] = ArtifactIdentity(kind="ClaimType", name=predicate)
    values["predicate"] = predicate
    return ClaimType.model_validate(values)


def _claim(
    *,
    subject_id: str = "wi-42",
    predicate: str = "project.work_item.status",
    value: str = "ready",
    qualifier: str | None = None,
    rationale: str = "The writer observed the current work status.",
    body: str = "status: ready\n",
    claim_ref: str | None = None,
    insertion_target: object | None = None,
    dispositions: tuple[object, ...] = (),
) -> ClaimAuthoringPayloadV1:
    return ClaimAuthoringPayloadV1(
        statement=AuthoringClaimStatementV1(
            subject=SemanticAddress.whole_artifact(subject_path(SUBJECT_KIND, subject_id)),
            predicate=predicate,
            qualifier=qualifier,
            object=LiteralClaimObject(value=value),
            role="observation",
        ),
        rationale=rationale,
        source=SelfSourceBodyV1(content_base64=base64.b64encode(body.encode("utf-8")).decode()),
        claim_ref=claim_ref,
        existing_claim_dispositions=dispositions,  # type: ignore[arg-type]
        insertion_target=insertion_target,  # type: ignore[arg-type]
    )


def _change_set(*members: object) -> ChangeSetAuthoringPayloadV1:
    return ChangeSetAuthoringPayloadV1(
        members=tuple(  # type: ignore[arg-type]
            sorted(
                members,  # type: ignore[type-var]
                key=lambda member: authoring_member_identity(member).encode("utf-8"),  # type: ignore[arg-type]
            )
        )
    )


def _accept(
    instance: PlaybillInstance,
    owner: object,
    coordinator: AuthoringIntentCoordinator,
    intent_id: str,
    actor: AuthenticatedActor,
) -> None:
    submitted = coordinator.submit(intent_id, actor=actor)
    assert submitted.status.proposal_id is not None, submitted.status.model_dump(mode="json")
    assert submitted.status.candidate_digest is not None
    approval = _sign(
        owner,
        submitted.status.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=submitted.status.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    activated = service_activate_playbill_proposal(
        instance,
        proposal_id=submitted.status.proposal_id,
        activated_by="owner",
    )
    assert activated.status == "accepted"


def test_a_mixed_change_set_lands_every_member_in_one_generation(tmp_path: Path) -> None:
    """Claims, a Subject, a ClaimType and a revision admit or refuse together."""

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    generations_before = len(instance.accepted_history())

    payload = _change_set(
        SubjectAuthoringPayloadV1(subject=_shell("wi-2")),
        ClaimTypeAuthoringPayloadV1(claim_type=_predicate_type("project.work_item.owner")),
        _claim(subject_id="wi-42", value="ready"),
        _claim(subject_id="wi-2", value="blocked"),
        _claim(subject_id="wi-2", predicate="project.work_item.owner", value="done"),
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent
    assert intent.semantic_identity.startswith("ChangeSet:")
    assert len(intent.change_set_claim_identities) == 3

    result = coordinator.submit(intent.intent_id, actor=actor)
    assert result.status.proposal_id is not None
    assert len(result.members) == 5
    assert {member.identity.partition(":")[0] for member in result.members} == {
        "Claim",
        "ClaimType",
        "Subject",
    }

    assert result.status.candidate_digest is not None
    approval = _sign(
        owner,
        result.status.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=result.status.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    activated = service_activate_playbill_proposal(
        instance,
        proposal_id=result.status.proposal_id,
        activated_by="owner",
    )
    assert activated.status == "accepted"
    assert len(instance.accepted_history()) == generations_before + 1

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert subject_path(SUBJECT_KIND, "wi-2") in tree
    assert claim_type_path("project.work_item.owner") in tree
    minted = {item.claim_id for item in intent.change_set_claim_identities}
    assert minted
    for claim_id in minted:
        assert claim_path(claim_id) in tree


def test_one_malformed_member_refuses_the_whole_intent_at_its_index(tmp_path: Path) -> None:
    """Atomic by construction: no proposal, no accepted change, a typed index."""

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    before = instance.accepted_coordinate()

    # This member's ClaimType is neither accepted at the base nor defined here.
    malformed = _claim(subject_id="wi-2", predicate="project.work_item.owner", value="done")
    payload = _change_set(
        SubjectAuthoringPayloadV1(subject=_shell("wi-2")),
        _claim(subject_id="wi-2", value="ready"),
        malformed,
    )
    index = payload.members.index(malformed)
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent

    result = coordinator.submit(intent.intent_id, actor=actor)
    assert result.status.proposal_id is None
    assert result.members == ()
    diagnostic = result.intent.last_preflight
    assert diagnostic is not None and diagnostic.verdict == "refused"
    offending = {(item.code, item.offending_element) for item in diagnostic.frontier.diagnostics}
    assert offending == {
        (
            "playbill.authoring.claim_type_not_found",
            f"members[{index}].statement.predicate",
        )
    }
    assert instance.accepted_coordinate() == before


def test_a_member_reads_a_sibling_definition_only_when_the_set_carries_it(
    tmp_path: Path,
) -> None:
    """A Claim sees the Subject and ClaimType its own set defines, and only those."""

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")

    carried = coordinator.create(
        actor=actor,
        payload=_change_set(
            SubjectAuthoringPayloadV1(subject=_shell("wi-sibling")),
            ClaimTypeAuthoringPayloadV1(claim_type=_predicate_type("project.work_item.owner")),
            _claim(subject_id="wi-sibling", predicate="project.work_item.owner", value="ready"),
        ),
        canonical_timestamp=TIMESTAMP,
    ).intent
    lowered = lower_authoring(instance, intent=carried, actor_id=actor.actor_id)
    changed = {path for path, _content in lowered.changed_members}
    assert subject_path(SUBJECT_KIND, "wi-sibling") in changed
    assert claim_type_path("project.work_item.owner") in changed

    missing = coordinator.create(
        actor=actor,
        payload=_change_set(
            ClaimTypeAuthoringPayloadV1(claim_type=_predicate_type("project.work_item.owner")),
            _claim(subject_id="wi-absent", predicate="project.work_item.owner", value="ready"),
        ),
        canonical_timestamp=TIMESTAMP,
    ).intent
    with pytest.raises(AuthoringLoweringError) as refusal:
        lower_authoring(instance, intent=missing, actor_id=actor.actor_id)
    assert refusal.value.code == "playbill.authoring.referent_not_found"
    assert refusal.value.offending_element.startswith("members[")


def test_two_members_on_one_path_and_one_slot_name_their_member_indices(
    tmp_path: Path,
) -> None:
    """Collisions are refused typed, and say which members collided."""

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")

    accepted_claim_id = _accept_one_claim(instance, owner, coordinator, actor)
    collided = coordinator.create(
        actor=actor,
        payload=_change_set(
            _claim(claim_ref=accepted_claim_id, value="done", rationale="Revise the status."),
            ClaimRetirementMemberV1(
                claim_ref=accepted_claim_id,
                reason="was-wrong",
            ),
        ),
        canonical_timestamp=TIMESTAMP,
    ).intent
    with pytest.raises(AuthoringLoweringError) as collision:
        lower_authoring(instance, intent=collided, actor_id=actor.actor_id)
    assert collision.value.code == "playbill.authoring.change_set_member_path_collision"
    assert collision.value.repairs[0].replacement == {
        "members": [0, 1],
        "path": claim_path(accepted_claim_id),
    }

    # A slot the accepted Claim above does not contend for, so the only Claims
    # the law demands a disposition on are the two siblings themselves.
    siblings = coordinator.create(
        actor=actor,
        payload=_change_set(
            _claim(value="ready", qualifier="q1", body="status: ready\n"),
            _claim(value="blocked", qualifier="q1", body="status: blocked\n"),
        ),
        canonical_timestamp=TIMESTAMP,
    ).intent
    with pytest.raises(AuthoringLoweringError) as slot:
        lower_authoring(instance, intent=siblings, actor_id=actor.actor_id)
    assert slot.value.code == "playbill.authoring.existing_claim_dispositions_incomplete"
    assert slot.value.offending_element.startswith("members[")
    replacement = slot.value.repairs[0].replacement
    assert isinstance(replacement, dict)
    assert replacement["sibling_members"] != []


def _accept_one_claim(
    instance: PlaybillInstance,
    owner: object,
    coordinator: AuthoringIntentCoordinator,
    actor: AuthenticatedActor,
) -> str:
    """Accept one singular Claim so later sets have a lineage to revise or retire."""

    seed = coordinator.create(
        actor=actor,
        payload=_claim(value="ready"),
        canonical_timestamp=TIMESTAMP,
    ).intent
    _accept(instance, owner, coordinator, seed.intent_id, actor)
    return seed.semantic_identity


def test_a_retirement_member_carries_its_closure_and_lands_with_the_set(
    tmp_path: Path,
) -> None:
    """Retirement is an ordinary member, and demands the same closure it always did."""

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    claim_id = _accept_one_claim(instance, owner, coordinator, actor)

    payload = _change_set(
        SubjectAuthoringPayloadV1(subject=_shell("wi-retire")),
        ClaimRetirementMemberV1(claim_ref=claim_id, reason="was-rescinded"),
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent
    _accept(instance, owner, coordinator, intent.intent_id, actor)

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    retired = parse_claim(tree[claim_path(claim_id)], path=claim_path(claim_id))
    assert retired.lifecycle.state == "retired"
    assert subject_path(SUBJECT_KIND, "wi-retire") in tree


def test_a_retirement_member_refuses_an_incomplete_closure(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    claim_id = _accept_one_claim(instance, owner, coordinator, actor)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    claim = parse_claim(tree[claim_path(claim_id)], path=claim_path(claim_id))

    intent = coordinator.create(
        actor=actor,
        payload=_change_set(
            SubjectAuthoringPayloadV1(subject=_shell("wi-noise")),
            ClaimRetirementMemberV1(
                claim_ref=claim_id,
                reason="was-wrong",
                dependents=(
                    ClaimRetireDependentV1(
                        artifact_identity=claim.identity,
                        predecessor_digest=claim_artifact_digest(claim).tagged,
                        reason="was-wrong",
                    ),
                ),
            ),
        ),
        canonical_timestamp=TIMESTAMP,
    ).intent
    with pytest.raises(AuthoringLoweringError) as refusal:
        lower_authoring(instance, intent=intent, actor_id=actor.actor_id)
    assert refusal.value.code == "playbill.authoring.claim_retirement_closure_incomplete"
    assert refusal.value.offending_element.endswith(".dependents")


def test_the_changed_member_ceiling_is_an_operator_knob_that_ignores_cards(
    tmp_path: Path,
) -> None:
    """A lowered ceiling refuses the set typed; derivative cards never count."""

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    payload = _change_set(
        SubjectAuthoringPayloadV1(subject=_shell("wi-2")),
        SubjectAuthoringPayloadV1(subject=_shell("wi-3")),
        _claim(subject_id="wi-2", value="ready"),
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent

    lowered = lower_authoring(instance, intent=intent, actor_id=actor.actor_id)
    authored = len(lowered.changed_members)
    assert authored == 3
    # The compiler emits one derivative card per authored member; none of them
    # counts, so the ceiling that exactly fits the authored members admits.
    instance.bind_receive_limits(ProposalReceiveLimits(max_changed_members=authored))
    assert coordinator.preflight(intent.intent_id, actor=actor).verdict == "passed"

    instance.bind_receive_limits(ProposalReceiveLimits(max_changed_members=authored - 1))
    refused = coordinator.preflight(intent.intent_id, actor=actor)
    assert refused.verdict == "refused"
    assert {item.code for item in refused.frontier.diagnostics} == {
        "playbill.authoring.proposal_receive_refused"
    }


def _page_target(page: bytes, anchor: bytes) -> InsertionTargetV2:
    start = page.index(anchor)
    return InsertionTargetV2(
        source_id="repo.work-items",
        coordinate=WorkingDigestCoordinateV1(
            source_content_digest=_digest(page),
            source_byte_length=len(page),
        ),
        initial_preimage_digest=_digest(page),
        initial_preimage_byte_length=len(page),
        selector=InsertionAnchorWindowV1(
            anchor_content_base64=base64.b64encode(anchor).decode("ascii"),
            anchor_bytes_digest=_digest(anchor),
            start_byte=start,
            end_byte=start + len(anchor),
            insertion_offset=start + len(anchor),
            observed_occurrence_count=1,
        ),
        operation="insert_after",
    )


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _observation(content: bytes) -> PublicationSourceObservationV2:
    return PublicationSourceObservationV2(
        source_id="repo.work-items",
        content_base64=base64.b64encode(content).decode("ascii"),
        content_digest=_digest(content),
        byte_length=len(content),
    )


PAGE = b"# work item\n## alpha\n## beta\n## gamma\n"


def _publishing_set(*anchors: bytes) -> tuple[ChangeSetAuthoringPayloadV1, dict[str, bytes]]:
    """One change set that publishes one Claim per anchor into the same page."""

    bodies: dict[str, bytes] = {}
    members = []
    for index, anchor in enumerate(anchors):
        body = f"status: ready ({index})\n".encode()
        member = _claim(
            qualifier=f"q{index}",
            body=body.decode("utf-8"),
            insertion_target=_page_target(PAGE, anchor),
        )
        bodies[authoring_member_identity(member)] = body
        members.append(member)
    return _change_set(*members), bodies


def test_three_publications_into_one_page_apply_in_anchor_order(tmp_path: Path) -> None:
    """One set publishes three Claims into one source, each seeing the fresh bytes."""

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")

    payload, bodies = _publishing_set(b"## alpha\n", b"## beta\n", b"## gamma\n")
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent
    _accept(instance, owner, coordinator, intent.intent_id, actor)

    resumed = coordinator.resume(intent.intent_id, actor=actor).intent
    assert resumed.insertion_expectation is None
    assert len(resumed.insertion_expectations) == 3
    assert {item.state for item in resumed.insertion_expectations} == {"pending"}

    claim_ids = {
        item.member_identity: item.claim_id for item in resumed.change_set_claim_identities
    }
    by_claim = {
        expectation.claim_identity: expectation for expectation in resumed.insertion_expectations
    }
    ordered = sorted(
        (
            (member.insertion_target.selector.insertion_offset, member)  # type: ignore[union-attr]
            for member in payload.members
            if isinstance(member, ClaimAuthoringPayloadV1)
        ),
        key=lambda item: item[0],
    )

    current = PAGE
    for _offset, member in ordered:
        identity = authoring_member_identity(member)
        expectation = by_claim[claim_ids[identity]]
        prepared = coordinator.prepare_publication(
            intent.intent_id,
            actor=actor,
            observation=_observation(current),
            expectation_id=expectation.expectation_id,
        )
        assert prepared.expectation.state == "prepared"
        landed = apply_playbill_publication(
            current,
            intent_id=intent.intent_id,
            expectation=prepared.expectation.model_dump(mode="json"),
            retained_body=bodies[identity],
        )
        current = landed.content
        confirmed = coordinator.confirm_insertion(
            intent.intent_id,
            actor=actor,
            observation=InsertionConfirmationObservationV2.model_validate(landed.observation),
            expectation_id=expectation.expectation_id,
        )
        assert confirmed.expectation.state == "bound"

    final = coordinator.resume(intent.intent_id, actor=actor).intent
    assert {item.state for item in final.insertion_expectations} == {"bound"}
    for index, anchor in enumerate((b"## alpha\n", b"## beta\n", b"## gamma\n")):
        assert current.index(f"status: ready ({index})\n".encode()) > current.index(anchor)


def test_a_stale_anchor_on_one_member_leaves_the_bound_member_alone(tmp_path: Path) -> None:
    """One member's stale anchor refuses that member, not the whole page."""

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")

    payload, bodies = _publishing_set(b"## alpha\n", b"## beta\n")
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent
    _accept(instance, owner, coordinator, intent.intent_id, actor)
    resumed = coordinator.resume(intent.intent_id, actor=actor).intent
    claim_ids = {
        item.member_identity: item.claim_id for item in resumed.change_set_claim_identities
    }
    by_claim = {item.claim_identity: item for item in resumed.insertion_expectations}
    members = sorted(
        (member for member in payload.members if isinstance(member, ClaimAuthoringPayloadV1)),
        key=lambda member: member.insertion_target.selector.insertion_offset,  # type: ignore[union-attr]
    )

    first_identity = authoring_member_identity(members[0])
    first = by_claim[claim_ids[first_identity]]
    prepared = coordinator.prepare_publication(
        intent.intent_id,
        actor=actor,
        observation=_observation(PAGE),
        expectation_id=first.expectation_id,
    )
    landed = apply_playbill_publication(
        PAGE,
        intent_id=intent.intent_id,
        expectation=prepared.expectation.model_dump(mode="json"),
        retained_body=bodies[first_identity],
    )
    coordinator.confirm_insertion(
        intent.intent_id,
        actor=actor,
        observation=InsertionConfirmationObservationV2.model_validate(landed.observation),
        expectation_id=first.expectation_id,
    )

    second = by_claim[claim_ids[authoring_member_identity(members[1])]]
    rewritten = landed.content.replace(b"## beta\n", b"")
    with pytest.raises(PublicationAnchorStale):
        coordinator.prepare_publication(
            intent.intent_id,
            actor=actor,
            observation=_observation(rewritten),
            expectation_id=second.expectation_id,
        )
    after = coordinator.resume(intent.intent_id, actor=actor).intent
    states = {item.expectation_id: item.state for item in after.insertion_expectations}
    assert states[first.expectation_id] == "bound"
    assert states[second.expectation_id] == "pending"


def _next_is_clean_after(instance: PlaybillInstance, source: bytes) -> bool:
    """Say whether `next` reports no unregistered block in the published page."""

    markers = tuple(
        sorted(
            (
                block.summary()
                for block in parse_projection_blocks(source, source_id="repo.work-items")
            ),
            key=lambda summary: summary.stamp.block_id.encode("ascii"),
        )
    )
    request = PlaybillNextRequestV1(
        at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        evaluation_time=datetime(2026, 8, 23, 12, tzinfo=UTC),
        access_profile=CoverageAccessProfileV1(
            profile_id="change-set-publication",
            permitted_access_classes=("instance", "public"),
        ),
        workspace_observation=PlaybillNextWorkspaceObservationV1(
            source_observations=(
                PlaybillNextSourceObservationV3(
                    tag="playbill-next-source-observation-v3",
                    source_id="repo.work-items",
                    observed_source_digest=_digest(source),
                    byte_length=len(source),
                    marker_summaries=markers,
                    occurrences=(),
                    scanned_commitment_digests=(),
                    scan_complete=True,
                    scan_notes=(),
                    marker_notes=(),
                ),
            )
        ),
    )
    return not [
        item
        for item in service_playbill_next(instance, request=request).items
        if item.reason == "unregistered_projection_block"
    ]


def _publish_every_expectation(
    coordinator: AuthoringIntentCoordinator,
    actor: AuthenticatedActor,
    intent_id: str,
    *,
    payload: ChangeSetAuthoringPayloadV1,
    bodies: dict[str, bytes],
    page: bytes,
) -> bytes:
    """Prepare, apply and confirm every publication in one set, in anchor order."""

    resumed = coordinator.resume(intent_id, actor=actor).intent
    claim_ids = {
        item.member_identity: item.claim_id for item in resumed.change_set_claim_identities
    }
    by_claim = {item.claim_identity: item for item in resumed.insertion_expectations}
    publishing = sorted(
        (
            member
            for member in payload.members
            if isinstance(member, ClaimAuthoringPayloadV1) and member.insertion_target is not None
        ),
        key=lambda member: member.insertion_target.selector.insertion_offset,  # type: ignore[union-attr]
    )
    current = page
    for member in publishing:
        identity = authoring_member_identity(member)
        expectation = by_claim[claim_ids[identity]]
        prepared = coordinator.prepare_publication(
            intent_id,
            actor=actor,
            observation=_observation(current),
            expectation_id=expectation.expectation_id,
        )
        landed = apply_playbill_publication(
            current,
            intent_id=intent_id,
            expectation=prepared.expectation.model_dump(mode="json"),
            retained_body=bodies[identity],
        )
        current = landed.content
        coordinator.confirm_insertion(
            intent_id,
            actor=actor,
            observation=InsertionConfirmationObservationV2.model_validate(landed.observation),
            expectation_id=expectation.expectation_id,
        )
    return current


def test_eighty_members_of_every_kind_become_exactly_one_generation(tmp_path: Path) -> None:
    """The maximal intent: claims, definitions, retirements, a revision, publications."""

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")

    seed = coordinator.create(
        actor=actor,
        payload=_change_set(*(_claim(qualifier=f"seed{index}") for index in range(3))),
        canonical_timestamp=TIMESTAMP,
    ).intent
    _accept(instance, owner, coordinator, seed.intent_id, actor)
    seeded = sorted(item.claim_id for item in seed.change_set_claim_identities)
    generations_before = len(instance.accepted_history())

    subjects = [SubjectAuthoringPayloadV1(subject=_shell(f"wi-b{index}")) for index in range(5)]
    predicates = ["project.work_item.owner", "project.work_item.reviewer"]
    claim_types = [
        ClaimTypeAuthoringPayloadV1(claim_type=_predicate_type(predicate))
        for predicate in predicates
    ]
    retirements = [
        ClaimRetirementMemberV1(claim_ref=seeded[0], reason="was-rescinded"),
        ClaimRetirementMemberV1(claim_ref=seeded[1], reason="was-wrong"),
    ]
    revision = _claim(
        qualifier="seed2",
        value="done",
        claim_ref=seeded[2],
        rationale="Revise the seeded status in place.",
    )
    published, bodies = [], {}
    for index, anchor in enumerate((b"## alpha\n", b"## beta\n", b"## gamma\n")):
        body = f"status: ready (pub{index})\n".encode()
        member = _claim(
            qualifier=f"pub{index}",
            body=body.decode("utf-8"),
            insertion_target=_page_target(PAGE, anchor),
        )
        bodies[authoring_member_identity(member)] = body
        published.append(member)
    plain = [
        _claim(
            subject_id=f"wi-b{index % 5}",
            predicate=predicates[index % 2],
            qualifier=f"p{index}",
        )
        for index in range(67)
    ]
    payload = _change_set(*subjects, *claim_types, *retirements, revision, *published, *plain)
    assert len(payload.members) == 80

    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent
    result = coordinator.submit(intent.intent_id, actor=actor)
    assert result.status.proposal_id is not None, result.intent.last_preflight
    assert len(result.members) == 80
    amending = {member.identity for member in result.members if member.identity_stable}
    assert amending == {
        f"Claim:{seeded[2]}",
        f"ClaimRetirement:{seeded[0]}",
        f"ClaimRetirement:{seeded[1]}",
    }

    _accept(instance, owner, coordinator, intent.intent_id, actor)
    assert len(instance.accepted_history()) == generations_before + 1

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    for subject in subjects:
        assert subject_path(SUBJECT_KIND, subject.subject.subject_id) in tree
    for predicate in predicates:
        assert claim_type_path(predicate) in tree
    for item in coordinator.resume(
        intent.intent_id, actor=actor
    ).intent.change_set_claim_identities:
        assert claim_path(item.claim_id) in tree
    for retired_id in seeded[:2]:
        retired = parse_claim(tree[claim_path(retired_id)], path=claim_path(retired_id))
        assert retired.lifecycle.state == "retired"

    final = _publish_every_expectation(
        coordinator,
        actor,
        intent.intent_id,
        payload=payload,
        bodies=bodies,
        page=PAGE,
    )
    assert _next_is_clean_after(instance, final)


def test_an_indexed_member_reference_expectation_resolves_and_refuses(
    tmp_path: Path,
) -> None:
    """`members[n].statement.subject` addresses exactly the member that owns it."""

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())

    payload = _change_set(
        SubjectAuthoringPayloadV1(subject=_shell("wi-2")),
        _claim(qualifier="a"),
        _claim(qualifier="b"),
    )
    index = next(
        position
        for position, member in enumerate(payload.members)
        if isinstance(member, ClaimAuthoringPayloadV1) and member.statement.qualifier == "b"
    )
    honest = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
        reference_expectations=(
            AuthoringReferenceExpectationV1(
                payload_path=f"members[{index}].statement.subject",
                artifact_kind="Subject",
                address=f"{SUBJECT_KIND}/wi-42",
                minted_coordinate=coordinate,
            ),
        ),
    ).intent
    resolved = coordinator.preflight(honest.intent_id, actor=actor)
    assert not [
        item
        for item in resolved.frontier.diagnostics
        if item.code.startswith("playbill.authoring.reference_")
    ]

    misaddressed = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp="2026-08-21T12:05:00.000000Z",
        reference_expectations=(
            AuthoringReferenceExpectationV1(
                payload_path=f"members[{index}].statement.subject",
                artifact_kind="Subject",
                address=f"{SUBJECT_KIND}/wi-2",
                minted_coordinate=coordinate,
            ),
        ),
    ).intent
    refused = coordinator.preflight(misaddressed.intent_id, actor=actor)
    assert {
        (item.code, item.offending_element)
        for item in refused.frontier.diagnostics
        if item.code.startswith("playbill.authoring.reference_")
    } == {
        (
            "playbill.authoring.reference_payload_mismatch",
            f"members[{index}].statement.subject",
        )
    }


def test_a_refused_set_rebases_and_its_generation_replays_byte_identically(
    tmp_path: Path,
) -> None:
    """A set advances over an unrelated acceptance, then replays from disk unchanged."""

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")

    payload = _change_set(
        _claim(subject_id="wi-late", qualifier="late"),
        _claim(qualifier="here"),
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent
    assert coordinator.preflight(intent.intent_id, actor=actor).verdict == "refused"

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[subject_path(SUBJECT_KIND, "wi-late")] = render_subject(_shell("wi-late"))
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp="2026-08-21T12:01:00.000000Z",
        proposal_name="advance-for-change-set-rebase",
    )

    rebased = coordinator.rebase(intent.intent_id, actor=actor).intent
    assert rebased.base_coordinate == AcceptedCoordinate.from_internal(
        instance.accepted_coordinate()
    )
    assert rebased.change_set_claim_identities == intent.change_set_claim_identities
    _accept(instance, owner, coordinator, intent.intent_id, actor)

    accepted = instance.accepted_coordinate()
    before = instance.tree_at(accepted.git_oid)
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    replayed = reopened.accepted_coordinate()
    assert replayed == accepted
    assert reopened.tree_at(replayed.git_oid) == before


def test_the_daemon_receive_ceiling_reads_its_file_or_refuses_loudly(tmp_path: Path) -> None:
    """The admission knob is operator-owned daemon state, never a caller's input."""

    assert load_proposal_receive_config(tmp_path).limits() == ProposalReceiveLimits()

    daemon = tmp_path / "daemon"
    daemon.mkdir()
    config = daemon / "proposal-receive.json"
    config.write_text(
        json.dumps(
            {
                "tag": "cruxible-proposal-receive-operational-config-v1",
                "max_changed_members": 12,
            }
        ),
        encoding="utf-8",
    )
    assert load_proposal_receive_config(tmp_path).limits().max_changed_members == 12

    config.write_text("{", encoding="utf-8")
    with pytest.raises(PlaybillExecutionError):
        load_proposal_receive_config(tmp_path)


def _refused_diagnostics(
    coordinator: AuthoringIntentCoordinator,
    intent_id: str,
    actor: AuthenticatedActor,
) -> dict[str, AuthoringDiagnosticV1]:
    """Submit one intent expected to refuse, and return its diagnostics by code."""

    result = coordinator.submit(intent_id, actor=actor)
    assert result.status.proposal_id is None, result.status.model_dump(mode="json")
    assert result.members == ()
    preflight = result.intent.last_preflight
    assert preflight is not None and preflight.verdict == "refused"
    return {item.code: item for item in preflight.frontier.diagnostics}


def _repair_replacement(diagnostic: AuthoringDiagnosticV1) -> dict[str, object]:
    replacement = diagnostic.repairs[0].replacement
    assert isinstance(replacement, dict)
    return replacement


def test_compiler_stage_refusals_are_addressed_to_the_offending_member(
    tmp_path: Path,
) -> None:
    """ "Typed to the offending member" covers the laws that run after lowering.

    Lowering refusals were already re-addressed to `members[n]`, but the laws
    the compiler runs on the candidate tree -- succession for both artifact
    kinds, the ClaimType's permitted roles, the ClaimType's literal schema --
    name only the artifact path they refused. With eighty members that is a
    path, not an index. Each case below is a compiler-stage refusal, and each
    asserts the member index alongside the artifact path.

    The four cases are illustrative, not exhaustive: the re-address is generic
    over every compiler code whose artifact path this lowering attributes to
    exactly one member. Evidence admission is the case worth naming, because it
    does refuse -- corroboration issues and a ClaimType admission policy's
    `refusal_codes` become diagnostics at the Claim's own artifact path, which
    is a member path, so they are re-addressed by the same generic rule. Only
    cardinality has no compiler-stage refusal to re-address: the cardinality-one
    slot law is decided in lowering (`existing_claim_dispositions_incomplete`,
    pinned above) and is member-addressed there.
    """

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")

    # Succession, ClaimType: rewriting an accepted ClaimType in place.
    rewritten = _predicate_type("project.work_item.status").model_copy(
        update={"literal_schema": {"type": "string"}}
    )
    claim_type_member = ClaimTypeAuthoringPayloadV1(claim_type=rewritten)
    # Succession, Subject: rewriting an accepted Subject in place.
    subject_member = SubjectAuthoringPayloadV1(
        subject=_shell("wi-42").model_copy(
            update={"lifecycle": ArtifactLifecycle(predecessor_digest="sha256:" + "0" * 64)}
        )
    )
    # Permitted roles: a ClaimType this set defines admits normative Claims only.
    normative_only = _predicate_type("project.work_item.owner").model_copy(
        update={"permitted_roles": ("normative",)}
    )
    role_type_member = ClaimTypeAuthoringPayloadV1(claim_type=normative_only)
    role_claim_member = _claim(predicate="project.work_item.owner", value="ready")
    # Literal schema: a value the accepted ClaimType's enum does not admit.
    schema_claim_member = _claim(value="not-a-status")

    cases: tuple[tuple[str, tuple[object, ...], object, str, str], ...] = (
        (
            "claim_type_succession",
            (claim_type_member, SubjectAuthoringPayloadV1(subject=_shell("wi-2"))),
            claim_type_member,
            "playbill.claim_type.stale_predecessor",
            claim_type_path("project.work_item.status"),
        ),
        (
            "subject_succession",
            (subject_member, SubjectAuthoringPayloadV1(subject=_shell("wi-2"))),
            subject_member,
            "playbill.subject.stale_predecessor",
            subject_path(SUBJECT_KIND, "wi-42"),
        ),
        (
            "permitted_roles",
            (role_type_member, role_claim_member),
            role_claim_member,
            "playbill.claim.statement_contract_mismatch",
            "",
        ),
        (
            "literal_schema",
            (schema_claim_member, SubjectAuthoringPayloadV1(subject=_shell("wi-2"))),
            schema_claim_member,
            "playbill.claim.literal_schema_invalid",
            "",
        ),
    )

    for label, members, offender, code, artifact_path in cases:
        payload = _change_set(*members)
        index = payload.members.index(offender)  # type: ignore[arg-type]
        intent = coordinator.create(
            actor=actor,
            payload=payload,
            canonical_timestamp=TIMESTAMP,
        ).intent
        path = artifact_path or claim_path(
            next(
                item.claim_id
                for item in intent.change_set_claim_identities
                if item.member_identity == authoring_member_identity(offender)  # type: ignore[arg-type]
            )
        )
        before = instance.accepted_coordinate()
        offending = _refused_diagnostics(coordinator, intent.intent_id, actor)
        assert code in offending, (label, sorted(offending))
        diagnostic = offending[code]
        assert diagnostic.stage == "proposal_evaluation", label
        assert diagnostic.offending_element == f"members[{index}].{path}", label
        assert _repair_replacement(diagnostic) == {
            "artifact_path": path,
            "member": index,
            "offending_element": f"members[{index}].{path}",
        }, label
        assert instance.accepted_coordinate() == before, label


def test_a_singular_intent_keeps_its_compiler_refusal_at_the_artifact_path(
    tmp_path: Path,
) -> None:
    """A singular intent owns every path it writes, so nothing is re-addressed."""

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")

    intent = coordinator.create(
        actor=actor,
        payload=_claim(value="not-a-status"),
        canonical_timestamp=TIMESTAMP,
    ).intent

    offending = _refused_diagnostics(coordinator, intent.intent_id, actor)
    diagnostic = offending["playbill.claim.literal_schema_invalid"]
    assert diagnostic.offending_element == claim_path(intent.semantic_identity)
    assert _repair_replacement(diagnostic) == {
        "offending_element": claim_path(intent.semantic_identity)
    }


def _claim_with_subject_draft(subject_id: str, shell: SubjectShell) -> ClaimAuthoringPayloadV2:
    """One Claim carrying its own Subject as a dependency draft."""

    authored = _claim(subject_id=subject_id)
    return ClaimAuthoringPayloadV2(
        statement=authored.statement,
        rationale=authored.rationale,
        source=authored.source,
        dependency_drafts=ClaimDependencyDraftsV1(subject=shell),
    )


def test_a_dependency_draft_that_carries_a_succession_still_refuses_typed(
    tmp_path: Path,
) -> None:
    """The successor laws for the withdrawn `dependency_not_one_claim` refusal.

    Before this batch a dependency draft that named a predecessor, or that was
    born retired, was refused at authoring as
    `playbill.authoring.dependency_not_one_claim` on the grounds that a
    succession meant a second change. A change set IS one change, so the staged
    tree decides instead and the draft is installed. What refuses it now is the
    ordinary compiler law on the artifact it wrote, and this pins both codes
    for a succession carried inside a set:

    - a draft naming a predecessor for a Subject that has none accepted refuses
      `playbill.subject.unexpected_predecessor`, addressed to the member that
      installed the draft;
    - a born-retired draft refuses `playbill.change_set.unresolved_pin`, which
      the compiler raises against the whole candidate rather than one artifact,
      so it stays addressed at `payload`.

    Neither draft reaches the accepted tree, and neither leaves a proposal.
    """

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    before = instance.accepted_coordinate()

    succeeding = _claim_with_subject_draft(
        "wi-succession",
        _shell("wi-succession").model_copy(
            update={"lifecycle": ArtifactLifecycle(predecessor_digest="sha256:" + "0" * 64)}
        ),
    )
    payload = _change_set(succeeding, SubjectAuthoringPayloadV1(subject=_shell("wi-2")))
    index = payload.members.index(succeeding)
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent
    offending = _refused_diagnostics(coordinator, intent.intent_id, actor)
    draft_path = subject_path(SUBJECT_KIND, "wi-succession")
    diagnostic = offending["playbill.subject.unexpected_predecessor"]
    assert diagnostic.stage == "proposal_evaluation"
    assert diagnostic.offending_element == f"members[{index}].{draft_path}"
    assert _repair_replacement(diagnostic) == {
        "artifact_path": draft_path,
        "member": index,
        "offending_element": f"members[{index}].{draft_path}",
    }

    retired = _claim_with_subject_draft(
        "wi-born-retired",
        _shell("wi-born-retired").model_copy(
            update={"lifecycle": ArtifactLifecycle(state="retired")}
        ),
    )
    born_retired = coordinator.create(
        actor=actor,
        payload=_change_set(retired, SubjectAuthoringPayloadV1(subject=_shell("wi-3"))),
        canonical_timestamp=TIMESTAMP,
    ).intent
    pinned = _refused_diagnostics(coordinator, born_retired.intent_id, actor)
    unresolved = pinned["playbill.change_set.unresolved_pin"]
    assert unresolved.stage == "proposal_evaluation"
    assert unresolved.offending_element == "payload"
    assert _repair_replacement(unresolved) == {"offending_element": "payload"}

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert draft_path not in tree
    assert subject_path(SUBJECT_KIND, "wi-born-retired") not in tree
    assert instance.accepted_coordinate() == before


def test_a_one_member_change_set_lands_and_keeps_its_change_set_identity(
    tmp_path: Path,
) -> None:
    """`min_length` 1 is a landing set, not just a payload the validator admits.

    The withdrawn `at least 2 items` pin said a one-member set was not a set at
    all; the ruling is that a set of one is the ordinary case the SDK builder
    emits when a program adds a single member. This drives one end to end and
    compares it against the equivalent singular Claim intent: the set identity
    is a ChangeSet over its member, the singular identity is the Claim itself,
    and the two payloads differ only in being a set.
    """

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    generations_before = len(instance.accepted_history())

    member = _claim(subject_id="wi-42", value="ready")
    payload = _change_set(member)
    assert len(payload.members) == 1
    created = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent
    assert created.semantic_identity.startswith("ChangeSet:")
    assert len(created.change_set_claim_identities) == 1
    minted = created.change_set_claim_identities[0].claim_id

    result = coordinator.submit(created.intent_id, actor=actor)
    assert result.status.proposal_id is not None
    assert [item.identity for item in result.members] == [authoring_member_identity(member)]
    assert result.status.candidate_digest is not None
    approval = _sign(
        owner,
        result.status.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=result.status.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    activated = service_activate_playbill_proposal(
        instance,
        proposal_id=result.status.proposal_id,
        activated_by="owner",
    )
    assert activated.status == "accepted"
    assert len(instance.accepted_history()) == generations_before + 1
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert claim_path(minted) in tree

    # The same member as a singular intent: same authored Claim, different
    # identity kind, and a payload digest that differs only by the wrapper.
    singular = (
        AuthoringIntentCoordinator(
            instance=instance,
            store=AuthoringIntentStore(
                instance.root / instance.descriptor.storage.exhaust,
                token_factory=lambda: "b" * 32,
            ),
            claim_id_factory=lambda: "CLM-" + "c" * 32,
            clock=lambda: datetime(2026, 8, 22, 12, tzinfo=UTC),
        )
        .create(
            actor=actor,
            payload=member,
            canonical_timestamp=TIMESTAMP,
        )
        .intent
    )
    assert singular.semantic_identity == "CLM-" + "c" * 32
    assert singular.change_set_claim_identities == ()
    # Where they differ: set identity is a ChangeSet over the member's (kind,
    # identity), Claim identity is the minted Claim ID, and the payload digest
    # carries the set wrapper.
    assert created.semantic_identity == "ChangeSet:" + typed_digest(
        Sha256Value,
        AUTHORING_CHANGE_SET_MEMBERSHIP_DIGEST_DOMAIN,
        {
            "members": [
                {"kind": kind, "identity": identity}
                for kind, identity in authoring_change_set_membership(payload.members)
            ]
        },
    ).tagged.removeprefix("sha256:")
    assert singular.payload_digest != created.payload_digest
    assert singular.create_fingerprint != created.create_fingerprint
    # Where they must not differ: the authored member is the same bytes either way.
    assert singular.payload.model_dump(mode="json") == created.payload.members[0].model_dump(  # type: ignore[union-attr]
        mode="json"
    )
    assert authoring_payload_digest(singular.payload) == authoring_payload_digest(
        created.payload.members[0]  # type: ignore[union-attr]
    )


def test_a_retirement_member_spells_its_claim_ref_the_way_a_claim_does(
    tmp_path: Path,
) -> None:
    """One retirement has one spelling, so create-dedup cannot miss it.

    `Claim:CLM-...` and `CLM-...` name the same Claim, so both gave the same
    member identity -- and therefore the same `ChangeSet:` semantic identity --
    while digesting to different payloads. Two live intents could then carry one
    semantic identity and the create fingerprint would never match. The Claim
    member kind has never tolerated the prefix; the retirement member now agrees.
    """

    claim_id = "CLM-" + "1" * 32
    member = ClaimRetirementMemberV1(claim_ref=claim_id, reason="was-rescinded")
    assert member.claim_id == claim_id
    assert authoring_member_identity(member) == f"ClaimRetirement:{claim_id}"

    # Both Claim-addressing member kinds refuse the prefixed spelling, the same
    # way, with the same message.
    with pytest.raises(ClaimFormatError, match="Claim ID must be CLM-"):
        ClaimRetirementMemberV1(claim_ref=f"Claim:{claim_id}", reason="was-rescinded")
    with pytest.raises(ClaimFormatError, match="Claim ID must be CLM-"):
        _claim(claim_ref=f"Claim:{claim_id}")

    # The tagless surface carries the same rule through its own lowering.
    prefixed = ChangeSetInput(
        kind="change_set",
        members=(
            ClaimRetirementInput(
                kind="claim_retirement",
                claim_id=f"Claim:{claim_id}",
                reason="was-rescinded",
            ),
        ),
    )
    with pytest.raises(ClaimFormatError, match="Claim ID must be CLM-"):
        lower_authoring_input(prefixed, tree={})

    # And the one admissible spelling still creates exactly one intent twice.
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    accepted = _accept_one_claim(instance, owner, coordinator, actor)
    payload = _change_set(
        ClaimRetirementMemberV1(claim_ref=accepted, reason="was-rescinded"),
        SubjectAuthoringPayloadV1(subject=_shell("wi-dedup")),
    )
    first = coordinator.create(actor=actor, payload=payload, canonical_timestamp=TIMESTAMP).intent
    again = coordinator.create(actor=actor, payload=payload, canonical_timestamp=TIMESTAMP).intent
    assert again.intent_id == first.intent_id
    assert again.create_fingerprint == first.create_fingerprint
    assert again.payload_digest == first.payload_digest


def accepted_change_set_record(instance: PlaybillInstance) -> tuple[int, int]:
    """The entry count and byte size of the newest accepted change-set record."""

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = max(item for item in tree if item.startswith("changesets/cs-"))
    content = tree[path]
    return len(json.loads(content)["members"]), len(content)


def _measured_subject_set(
    instance: PlaybillInstance,
    owner: object,
    coordinator: AuthoringIntentCoordinator,
    actor: AuthenticatedActor,
    count: int,
) -> None:
    payload = _change_set(
        *(
            SubjectAuthoringPayloadV1(subject=_shell(f"wi-fresh-{index:03d}"))
            for index in range(count)
        )
    )
    intent = coordinator.create(actor=actor, payload=payload, canonical_timestamp=TIMESTAMP).intent
    _accept(instance, owner, coordinator, intent.intent_id, actor)


def _measured_claim_type_set(
    instance: PlaybillInstance,
    owner: object,
    coordinator: AuthoringIntentCoordinator,
    actor: AuthenticatedActor,
    count: int,
) -> None:
    payload = _change_set(
        *(
            ClaimTypeAuthoringPayloadV1(
                claim_type=_predicate_type(f"project.work_item.p{index:03d}")
            )
            for index in range(count)
        )
    )
    intent = coordinator.create(actor=actor, payload=payload, canonical_timestamp=TIMESTAMP).intent
    _accept(instance, owner, coordinator, intent.intent_id, actor)


def _measured_claim_set(
    instance: PlaybillInstance,
    owner: object,
    coordinator: AuthoringIntentCoordinator,
    actor: AuthenticatedActor,
    count: int,
) -> list[str]:
    payload = _change_set(
        *(_claim(subject_id=f"wi-cost-{index:03d}", value="ready") for index in range(count))
    )
    intent = coordinator.create(actor=actor, payload=payload, canonical_timestamp=TIMESTAMP).intent
    _accept(instance, owner, coordinator, intent.intent_id, actor)
    return [item.claim_id for item in intent.change_set_claim_identities]


def _measured_retirement_set(
    instance: PlaybillInstance,
    owner: object,
    coordinator: AuthoringIntentCoordinator,
    actor: AuthenticatedActor,
    count: int,
) -> None:
    claim_ids = _measured_claim_set(instance, owner, coordinator, actor, count)
    payload = _change_set(
        *(
            ClaimRetirementMemberV1(claim_ref=claim_id, reason="was-rescinded")
            for claim_id in claim_ids
        )
    )
    intent = coordinator.create(actor=actor, payload=payload, canonical_timestamp=TIMESTAMP).intent
    _accept(instance, owner, coordinator, intent.intent_id, actor)


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param(_measured_subject_set, id="subject"),
        pytest.param(_measured_claim_type_set, id="claim_type"),
        pytest.param(_measured_claim_set, id="claim"),
        pytest.param(_measured_retirement_set, id="retirement"),
    ],
)
def test_the_advertised_record_ceiling_is_the_measured_one(
    tmp_path: Path,
    kind: object,
) -> None:
    """The advertised per-entry cost bounds what each member kind actually writes.

    Card 110's reading was one corpus: a 1,002-entry change-set record measured
    7,046,087 bytes, the same to the byte on a corpus carrying a third of the
    evidence, because the record holds digests and paths and not evidence. One
    corpus is one member mix, though, and an entry's cost is the cost of the
    laws evaluated against the artifact that wrote it -- a Claim entry costs
    four times a Subject entry. A constant taken from one mix is an average, and
    an average is not a bound.

    So this settles a real change set of each kind on a hermetic instance, reads
    the accepted record back off the tree, and asserts the ADVERTISED cost is
    never smaller than the one measured. The fan-out kind, whose dependents are
    the most expensive entries of all, is measured where successions live:
    `test_claim_type_migrations_in_change_sets`.
    """

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(
        instance,
        owner,
        additional_subjects=tuple(_shell(f"wi-cost-{index:03d}") for index in range(8)),
    )
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    limits = ProposalReceiveLimits()

    kind(instance, owner, coordinator, actor, 8)  # type: ignore[operator]

    entries, size = accepted_change_set_record(instance)
    assert entries == 8
    # The whole point of the advertisement: a projection computed from it is
    # never smaller than the record it predicts, so a set it admits is a set the
    # ledger can write.
    assert limits.projected_change_set_record_bytes(entries) >= size
    assert limits.max_change_set_record_bytes == 4 * 1024 * 1024
    assert limits.change_set_record_bytes_per_member == 11 * 1024
    assert limits.max_change_set_members == 372
    # Advertised next to the member and byte budgets, and deliberately far below
    # the changed-member budget: receive would take this set, the ledger cannot.
    assert limits.max_change_set_members < limits.max_changed_members


def test_a_thousand_member_change_set_refuses_before_it_is_compiled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Card 106 + 110: the refusal arrives before lowering, naming the limit.

    The durability acceptance for a 1,000-member set is that it either
    completes or refuses typed BEFORE compiling, and that the process survives
    either way. Lowering is the step that built the whole candidate tree in
    memory and took a daemon out; this asserts it is never entered, so there is
    no allocation to survive rather than a larger one that happened to fit.
    """

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    payload = _change_set(
        *(
            SubjectAuthoringPayloadV1(subject=_shell(f"wi-bulk-{index:04d}"))
            for index in range(1_000)
        )
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent

    def _never(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a set over the record ceiling was compiled anyway")

    monkeypatch.setattr(preflight_module, "lower_authoring", _never)
    pid_before = os.getpid()

    result = coordinator.preflight(intent.intent_id, actor=actor)

    assert os.getpid() == pid_before
    assert result.verdict == "refused"
    diagnostic = next(
        item
        for item in result.frontier.diagnostics
        if item.code == "playbill.authoring.change_set_record_too_large"
    )
    limits = ProposalReceiveLimits()
    # The refusal names the measured limit, in members and in bytes, and the
    # size this set would have written.
    assert str(limits.max_change_set_members) in diagnostic.message
    assert str(limits.max_change_set_record_bytes) in diagnostic.message
    assert str(limits.projected_change_set_record_bytes(1_000)) in diagnostic.message
    assert "projects to at least 1000 record entries" in diagnostic.message
    assert diagnostic.repairs[0].replacement == {
        "record_entries": 1_000,
        "record_entries_measured": False,
        "max_change_set_members": limits.max_change_set_members,
        "max_change_set_record_bytes": limits.max_change_set_record_bytes,
        "projected_change_set_record_bytes": limits.projected_change_set_record_bytes(1_000),
    }
    assert [item.blocked_by for item in result.frontier.blocked_checks] == [
        ("playbill.authoring.change_set_record_too_large",)
    ]


def test_a_set_at_the_record_ceiling_still_compiles(tmp_path: Path) -> None:
    """The bound refuses the sets that cannot activate, and only those."""

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    per_member = CHANGE_SET_RECORD_BYTES_PER_MEMBER
    instance.bind_receive_limits(ProposalReceiveLimits(max_change_set_record_bytes=3 * per_member))
    assert instance.proposal_service().receive_limits.max_change_set_members == 3
    payload = _change_set(
        SubjectAuthoringPayloadV1(subject=_shell("wi-2")),
        SubjectAuthoringPayloadV1(subject=_shell("wi-3")),
        _claim(subject_id="wi-2", value="ready"),
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent

    assert coordinator.preflight(intent.intent_id, actor=actor).verdict == "passed"

    instance.bind_receive_limits(ProposalReceiveLimits(max_change_set_record_bytes=2 * per_member))
    assert instance.proposal_service().receive_limits.max_change_set_members == 2
    refused = coordinator.preflight(intent.intent_id, actor=actor)

    assert refused.verdict == "refused"
    assert {item.code for item in refused.frontier.diagnostics} == {
        "playbill.authoring.change_set_record_too_large"
    }


def test_a_set_of_retirements_at_the_old_ceiling_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound is a bound for the kind that costs the most, not only the least.

    582 members was the ceiling this build first advertised, computed from an
    average per-member cost that Claim retirements exceed by a fifth. A set of
    582 of them would have passed preflight, compiled, and been refused at
    activation for a record the ledger could not write -- which is the whole of
    card 110. The advertised cost now bounds the most expensive kind, so the
    set is refused where every other over-large set is: before lowering.
    """

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    payload = _change_set(
        *(
            ClaimRetirementMemberV1(claim_ref=f"CLM-{index:032x}", reason="was-rescinded")
            for index in range(582)
        )
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent

    def _never(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a set over the record ceiling was compiled anyway")

    monkeypatch.setattr(preflight_module, "lower_authoring", _never)

    result = coordinator.preflight(intent.intent_id, actor=actor)

    assert result.verdict == "refused"
    diagnostic = next(
        item
        for item in result.frontier.diagnostics
        if item.code == "playbill.authoring.change_set_record_too_large"
    )
    assert "projects to at least 582 record entries" in diagnostic.message
    assert str(ProposalReceiveLimits().max_change_set_members) in diagnostic.message


def test_a_set_that_lowers_to_more_entries_than_it_authors_refuses_before_evaluation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The record bound bounds the record, not the member list that projects it.

    A dependency draft is an entry in the record and not a member of the set:
    two `ClaimAuthoringPayloadV2` members each carrying a Subject draft author
    two members and write four paths. The pre-lowering projection counts two and
    lets them through; the exact count is known once lowering has run, and the
    refusal still arrives before the evaluation that costs the ten minutes card
    110 reported. `evaluate_proposal_tree` is replaced with a function that
    fails, so "never evaluated" is proved rather than inferred from a timing.
    """

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    per_member = CHANGE_SET_RECORD_BYTES_PER_MEMBER
    instance.bind_receive_limits(ProposalReceiveLimits(max_change_set_record_bytes=3 * per_member))
    assert instance.proposal_service().receive_limits.max_change_set_members == 3
    payload = _change_set(
        _claim_with_subject_draft("wi-draft-a", _shell("wi-draft-a")),
        _claim_with_subject_draft("wi-draft-b", _shell("wi-draft-b")),
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent

    def _never(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a set over the record ceiling was evaluated anyway")

    monkeypatch.setattr(preflight_module, "evaluate_proposal_tree", _never)

    result = coordinator.preflight(intent.intent_id, actor=actor)

    assert result.verdict == "refused"
    diagnostic = next(
        item
        for item in result.frontier.diagnostics
        if item.code == "playbill.authoring.change_set_record_too_large"
    )
    # Four: two Claim cards and the two Subject drafts that carry them.
    assert "lowers to 4 record entries" in diagnostic.message
    assert diagnostic.repairs[0].replacement == {
        "record_entries": 4,
        "record_entries_measured": True,
        "max_change_set_members": 3,
        "max_change_set_record_bytes": 3 * per_member,
        "projected_change_set_record_bytes": 4 * per_member,
    }
    assert [item.blocked_by for item in result.frontier.blocked_checks] == [
        ("playbill.authoring.change_set_record_too_large",)
    ]


def test_a_compile_that_exhausts_memory_refuses_typed_instead_of_propagating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Card 106: the allocation failure the process CAN see is never untyped."""

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    payload = _change_set(
        SubjectAuthoringPayloadV1(subject=_shell("wi-2")),
        _claim(subject_id="wi-2", value="ready"),
    )
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent

    def _exhausted(*_args: object, **_kwargs: object) -> object:
        raise MemoryError()

    monkeypatch.setattr(preflight_module, "lower_authoring", _exhausted)
    pid_before = os.getpid()

    result = coordinator.preflight(intent.intent_id, actor=actor)

    assert os.getpid() == pid_before
    assert result.verdict == "refused"
    diagnostic = next(
        item
        for item in result.frontier.diagnostics
        if item.code == "playbill.authoring.compile_budget_exceeded"
    )
    assert diagnostic.owner == "daemon"
    assert str(ProposalReceiveLimits().max_change_set_members) in diagnostic.message
