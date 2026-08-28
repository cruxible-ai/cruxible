"""What survives the withdrawal of the stranded-citation laws.

The `claim_cites_retired` queue row and the retirement `citing_claims` advisory
are WITHDRAWN. Retiring a Claim withdraws an assertion, not the evidence under
it, so the Claims that read the same bytes are not stranded by the retirement.
The only key those laws could be built on -- byte equality of the committed
content -- joins unrelated Claims that happen to read identical spans of
different sources. The sound key is an explicit copy edge recorded on the
citation, which is a wire movement belonging to the shared-Capture/copy-edge
design decision.

What is kept here is the evidence for that ruling, not the withdrawn machinery:
the acceptance law that makes a shared Capture impossible, the reachable
copied_from world, and the pins that both surfaces now stay silent.
"""

from __future__ import annotations

from pathlib import Path

from cruxible_client.contracts.claims import (
    LiteralClaimObject,
    build_claim_citation,
    claim_citation_references,
    claim_path,
    claim_statement_address,
    parse_claim,
    render_claim,
)


def test_citing_another_claims_coordinator_capture_is_refused_at_evaluation(
    tmp_path: Path,
) -> None:
    """Why the stranding law has no end-to-end path today, pinned as a law.

    The detector above is correct and the retirement advisory is correct, but
    the state they act on -- two Claims citing one Capture -- is not reachable
    through the surfaces. Two separate rules produce that:

    * every builder in `contracts.captures` binds `claim_id` into the envelope
      it mints, so no authoring call can hand the same Capture to two Claims;
    * for the coordinator self-source contract, proposal evaluation refuses a
      Claim citing a Capture bound to a different Claim outright.

    The second is the harder one and it is what this test pins. The refusal is
    scoped to that one contract (`_citation_origin_refusal`), so a Capture under
    another contract -- a foreign source, a provider run -- could legitimately
    be shared if a builder ever minted one without the claim_id binding. Until
    one does, `claim_cites_retired` is a law with no reachable subject.
    """
    from cruxible_core.playbill.proposals import (
        AuthenticatedActor,
        ProposalAdmissionRequest,
    )
    from tests.test_playbill.test_claim_type_migrations import _accepted_claim_world

    instance, first_id, _owner = _accepted_claim_world(tmp_path)

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    first_path = claim_path(first_id)
    first_claim = parse_claim(tree[first_path], path=first_path)
    (shared,) = {reference.capture_digest for reference in claim_citation_references(first_claim)}

    second_id = "CLM-" + "9" * 32
    second_identity = first_claim.identity.model_copy(update={"name": second_id})
    second = first_claim.model_copy(
        update={
            "identity": second_identity,
            "statement": first_claim.statement.model_copy(
                update={"object": LiteralClaimObject(value="blocked")}
            ),
            "backing": first_claim.backing.model_copy(
                update={
                    "citations": (
                        build_claim_citation(
                            second_identity,
                            capture_digest=shared,
                            role="evidence",
                            origin="independent",
                        ),
                    ),
                    "source_mappings": tuple(
                        mapping.model_copy(
                            update={"subject": claim_statement_address(claim_path(second_id))}
                        )
                        for mapping in first_claim.backing.source_mappings
                    ),
                }
            ),
        }
    )
    tree[claim_path(second_id)] = render_claim(second)

    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/shared-capture-citer",
            proposed_base_oid=instance.accepted_coordinate().git_oid,
        ),
        candidate_tree=tree,
        timestamp="2026-08-16T21:00:00.000000Z",
    )

    assert result.evaluation.verdict == "refused"
    assert result.candidate is None
    assert [item.code for item in result.evaluation.diagnostics] == [
        "playbill.claim.self_source_capture_unbound"
    ]


def _activate_intent(instance: object, submitted: object) -> None:
    """Approve and activate one submitted authoring intent."""
    from cruxible_core.playbill.service.documents import (
        service_activate_playbill_proposal,
        service_submit_playbill_approval,
    )
    from tests.test_playbill._support import client_material
    from tests.test_playbill.test_activation import _sign

    proposal_id = submitted.status.proposal_id  # type: ignore[attr-defined]
    candidate_digest = submitted.status.candidate_digest  # type: ignore[attr-defined]
    assert proposal_id is not None
    assert candidate_digest is not None
    approval = _sign(
        client_material(instance.root.parent, instance),  # type: ignore[attr-defined]
        candidate_digest,
        instance.accepted_coordinate().semantic_root,  # type: ignore[attr-defined]
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    assert (
        service_activate_playbill_proposal(
            instance,
            proposal_id=proposal_id,
            activated_by="owner",
        ).status
        == "accepted"
    )


SOURCE_CLAIM_ID = "CLM-" + "1" * 32
COPY_CLAIM_ID = "CLM-" + "2" * 32


def copied_from_world(root: Path, *, retire: bool = True):  # type: ignore[no-untyped-def]
    """Build the reachable stranding world and retire the Claim that was copied.

    Two Claims read one span of one working source. Each mints its own Capture
    envelope -- they must, every builder binds claim_id -- and both envelopes
    commit to the same selected bytes. The observing Claim is then retired.

    This world is kept because it is the one that made the ruling decidable: it
    is genuinely reachable, and the surfaces are now pinned silent on it.
    Returns the instance, its owner, and the coordinator and actor that built it.
    """
    from cruxible_client.contracts.authoring.models import (
        AuthoringExistingClaimDispositionV1,
    )
    from cruxible_client.contracts.captures import foreign_source_capture_contract
    from cruxible_client.contracts.claims import ClaimRetireRequestV1
    from cruxible_client.contracts.projection import AcceptedCoordinate
    from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.playbill.authoring.store import AuthoringIntentStore
    from cruxible_core.playbill.claim_retirement import service_retire_claim
    from cruxible_core.playbill.proposals import AuthenticatedActor
    from tests.test_playbill._support import initialize_local
    from tests.test_playbill.test_authoring_preflight import (
        TIMESTAMP,
        _seed_claim_surface,
        _working_payload,
    )
    from tests.test_playbill.test_claim_retirement import _activate as _activate_retirement

    instance, owner = initialize_local(root)
    _seed_claim_surface(
        instance,
        owner,
        contract=foreign_source_capture_contract("repo.work-items"),
    )
    tokens = iter(("a" * 32, "b" * 32, "c" * 32))
    claim_ids = iter((SOURCE_CLAIM_ID, COPY_CLAIM_ID))
    coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(
            instance.root / instance.descriptor.storage.exhaust,
            token_factory=lambda: next(tokens),
        ),
        claim_id_factory=lambda: next(claim_ids),
    )
    actor = AuthenticatedActor(actor_id="owner")

    observing = coordinator.create(
        actor=actor,
        payload=_working_payload(occurrence_count=1).model_copy(
            update={"citation_role": "evidence"}
        ),
        canonical_timestamp=TIMESTAMP,
    ).intent
    _activate_intent(instance, coordinator.submit(observing.intent_id, actor=actor))

    copying = coordinator.create(
        actor=actor,
        payload=_working_payload(occurrence_count=1).model_copy(
            update={
                "citation_role": "copy",
                "existing_claim_dispositions": (
                    AuthoringExistingClaimDispositionV1(
                        claim_id=SOURCE_CLAIM_ID,
                        disposition="not_tested",
                    ),
                ),
            }
        ),
        canonical_timestamp=TIMESTAMP,
    ).intent
    _activate_intent(instance, coordinator.submit(copying.intent_id, actor=actor))

    if not retire:
        return instance, owner, coordinator, actor
    retirement = service_retire_claim(
        instance,
        claim_id=SOURCE_CLAIM_ID,
        request=ClaimRetireRequestV1(
            mode="submit",
            reason="was-rescinded",
            expected_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        ),
        actor=actor,
    )
    _activate_retirement(instance, owner, retirement)
    return instance, owner, coordinator, actor


def test_retiring_a_claim_says_nothing_about_who_else_read_the_same_bytes(
    tmp_path: Path,
) -> None:
    """The retirement preflight advisory is withdrawn; the field stays empty."""
    from cruxible_client.contracts.claims import ClaimRetireRequestV1
    from cruxible_client.contracts.projection import AcceptedCoordinate
    from cruxible_core.playbill.claim_retirement import (
        ClaimRetirePreflightV1,
        service_retire_claim,
    )
    from cruxible_core.playbill.proposals import AuthenticatedActor

    instance, _owner, _coordinator, _actor = copied_from_world(tmp_path, retire=False)

    preflight = service_retire_claim(
        instance,
        claim_id=SOURCE_CLAIM_ID,
        request=ClaimRetireRequestV1(
            mode="preflight",
            reason="was-rescinded",
            expected_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        ),
        actor=AuthenticatedActor(actor_id="owner"),
    )

    assert isinstance(preflight, ClaimRetirePreflightV1)
    assert preflight.citing_claims == ()


def test_the_queue_stays_silent_about_the_claim_that_copied_a_retired_one(
    tmp_path: Path,
) -> None:
    """The reachable stranding world produces no row. That is the ruling."""
    from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
    from cruxible_core.service.playbill_next import (
        PlaybillNextRequestV1,
        service_playbill_next,
    )

    instance, _owner, _coordinator, _actor = copied_from_world(tmp_path)

    result = service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            evaluation_time="2026-08-20T00:00:00.000000Z",
            access_profile=CoverageAccessProfileV1(
                profile_id="withdrawn-stranded-citation",
                permitted_access_classes=("instance", "public"),
            ),
        ),
    )

    assert all(item.reason != "claim_cites_retired" for item in result.items)


def test_the_withdrawn_reason_stays_reserved_on_the_wire() -> None:
    """Reserved, not removed: the copy-edge design must not move this twice."""
    from typing import get_args

    from cruxible_core.service.playbill_next import NextReason

    assert "claim_cites_retired" in get_args(NextReason)
