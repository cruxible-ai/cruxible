"""One authoring intent is one changeset: mixed members, atomicity, replay."""

from __future__ import annotations

import base64
import hashlib
import itertools
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.authoring.insertions import apply_playbill_publication
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatementV1,
    AuthoringReferenceExpectationV1,
    ChangeSetAuthoringPayloadV1,
    ClaimAuthoringPayloadV1,
    ClaimRetirementMemberV1,
    ClaimTypeAuthoringPayloadV1,
    InsertionAnchorWindowV1,
    InsertionConfirmationObservationV2,
    InsertionTargetV2,
    PublicationSourceObservationV2,
    SelfSourceBodyV1,
    SubjectAuthoringPayloadV1,
    WorkingDigestCoordinateV1,
    authoring_member_identity,
)
from cruxible_client.contracts.claim_types import ClaimType, claim_type_path
from cruxible_client.contracts.claims import (
    ClaimRetireDependentV1,
    LiteralClaimObject,
    claim_artifact_digest,
    claim_path,
    parse_claim,
)
from cruxible_client.contracts.declared_blocks import parse_projection_blocks
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.proposal_models import ProposalReceiveLimits
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_path
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
