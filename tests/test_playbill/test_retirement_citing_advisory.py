"""Retiring a Claim names the live Claims left standing on its evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import (
    CaptureEnvelopeV1,
    CaptureRunCoordinateV1,
    EvidenceCommitmentV1,
    render_capture_envelope,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.claims import (
    AcceptedClaim,
    ClaimArtifact,
    ClaimArtifactV2,
    ClaimBackingV2,
    LiteralClaimObject,
    build_claim_citation,
    claim_artifact_digest,
    claim_citation_references,
    claim_path,
    claim_statement_address,
    claim_statement_digest,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.errors import PlaybillCasError
from cruxible_client.contracts.source_references import CasSourceReferenceV1
from cruxible_core.playbill.claim_retirement import _citing_claims
from cruxible_core.playbill.coverage.indexes import (
    claim_cited_content_digests,
    live_claim_captures_by_content,
)
from tests.test_playbill.test_claims import _claim

# Two Claims never share a Capture digest -- every builder binds claim_id into
# the envelope it mints -- so each Claim below gets its own Capture, and the
# ones that stand on the same evidence commit to the same CONTENT digest.
CAPTURE_ONE = "sha256:" + "11" * 32
CAPTURE_TWO = "sha256:" + "22" * 32
CAPTURE_THREE = "sha256:" + "33" * 32
CONTENT_A = "sha256:" + "ab" * 32
CONTENT_B = "sha256:" + "ef" * 32
SOURCE_DIGEST = "sha256:" + "cd" * 32
FILLER = "sha256:" + "07" * 32


class _EnvelopeStore:
    """The Capture reads the index makes, served from a digest -> content map."""

    def __init__(self, committed: dict[str, str]) -> None:
        self._objects = {
            capture: render_capture_envelope(_envelope(content))
            for capture, content in committed.items()
        }

    def store(self, content: bytes) -> object:  # pragma: no cover - unused by the index
        raise NotImplementedError

    def verify(self, digest: str) -> bool:  # pragma: no cover - unused by the index
        return digest in self._objects

    def read(self, digest: str, *, access: BodyAccessContext) -> bytes:
        if digest not in self._objects:
            # Exactly what the real CAS raises for a digest it does not hold.
            raise PlaybillCasError("CAS object is missing")
        return self._objects[digest]


def _envelope(content_digest: str) -> CaptureEnvelopeV1:
    return CaptureEnvelopeV1(
        capture_contract_digest=FILLER,
        source=CasSourceReferenceV1(content_digest=content_digest),
        commitment=EvidenceCommitmentV1(
            digest_kind="exact_bytes",
            digest=content_digest,
            byte_length=12,
            materialization="cas",
        ),
        run_coordinate=CaptureRunCoordinateV1(
            run_kind="provider",
            run_id="citation-index-fixture",
            bound_generation=FILLER,
            executable_identity=ArtifactIdentity(kind="CaptureContract", name="fixture"),
            executable_digest=FILLER,
        ),
        run_receipt_digest=FILLER,
        producer=ArtifactIdentity(kind="Principal", name="owner"),
        producer_binding_digest=FILLER,
        observed_at=datetime(2026, 8, 16, 20, 0, tzinfo=UTC),
    )


def _cited_claim(*, claim_id: str, capture: str, state: str = "live") -> ClaimArtifactV2:
    legacy: ClaimArtifact = _claim(
        claim_id=claim_id,
        capture_digest=capture,
        source_digest=SOURCE_DIGEST,
        source_length=12,
    )
    citation = build_claim_citation(
        legacy.identity,
        capture_digest=capture,
        role="evidence",
        origin="independent",
    )
    claim = ClaimArtifactV2(
        identity=legacy.identity,
        statement=legacy.statement,
        backing=ClaimBackingV2(
            referent_context=legacy.backing.referent_context,
            capture_digests=(capture,),
            citations=(citation,),
            source_mappings=legacy.backing.source_mappings,
        ),
        authority=legacy.authority,
        pins=legacy.pins,
    )
    if state == "live":
        return claim
    return ClaimArtifactV2(
        **{**claim.model_dump(), "lifecycle": {**claim.lifecycle.model_dump(), "state": state}}
    )


def _accepted(claim: ClaimArtifactV2) -> AcceptedClaim:
    path = claim_path(claim.identity.name)
    return AcceptedClaim(
        path=path,
        claim=claim,
        statement_digest=claim_statement_digest(claim.statement).tagged,
        artifact_digest=claim_artifact_digest(claim).tagged,
    )


def _tree(*claims: ClaimArtifactV2) -> dict[str, bytes]:
    return {claim_path(claim.identity.name): render_claim(claim) for claim in claims}


def test_the_reverse_index_maps_committed_content_to_its_live_citing_claims() -> None:
    """Three Claims, three distinct Captures, two standing on the same content."""
    first = _cited_claim(claim_id="CLM-" + "1" * 32, capture=CAPTURE_ONE)
    second = _cited_claim(claim_id="CLM-" + "2" * 32, capture=CAPTURE_TWO)
    elsewhere = _cited_claim(claim_id="CLM-" + "3" * 32, capture=CAPTURE_THREE)
    store = _EnvelopeStore(
        {CAPTURE_ONE: CONTENT_A, CAPTURE_TWO: CONTENT_A, CAPTURE_THREE: CONTENT_B}
    )

    index = live_claim_captures_by_content(
        [_accepted(first), _accepted(second), _accepted(elsewhere)],
        store=store,
    )

    assert index[CONTENT_A] == {
        claim_path(first.identity.name): (CAPTURE_ONE,),
        claim_path(second.identity.name): (CAPTURE_TWO,),
    }
    assert index[CONTENT_B] == {claim_path(elsewhere.identity.name): (CAPTURE_THREE,)}


def test_a_retired_claim_never_counts_as_a_live_citer() -> None:
    live = _cited_claim(claim_id="CLM-" + "1" * 32, capture=CAPTURE_ONE)
    retired = _cited_claim(claim_id="CLM-" + "2" * 32, capture=CAPTURE_TWO, state="retired")
    store = _EnvelopeStore({CAPTURE_ONE: CONTENT_A, CAPTURE_TWO: CONTENT_A})

    index = live_claim_captures_by_content([_accepted(live), _accepted(retired)], store=store)

    assert index[CONTENT_A] == {claim_path(live.identity.name): (CAPTURE_ONE,)}


def test_an_unreadable_capture_drops_that_citation_rather_than_the_index() -> None:
    """One missing envelope must not blind the advisory to everything else."""
    readable = _cited_claim(claim_id="CLM-" + "1" * 32, capture=CAPTURE_ONE)
    missing = _cited_claim(claim_id="CLM-" + "2" * 32, capture=CAPTURE_TWO)
    store = _EnvelopeStore({CAPTURE_ONE: CONTENT_A})

    index = live_claim_captures_by_content([_accepted(readable), _accepted(missing)], store=store)

    assert index == {CONTENT_A: {claim_path(readable.identity.name): (CAPTURE_ONE,)}}


def test_retirement_names_the_live_claim_standing_on_the_same_content() -> None:
    """The W1-P7 regression: this citing Claim used to be stranded silently."""
    retiring = _cited_claim(claim_id="CLM-" + "1" * 32, capture=CAPTURE_ONE)
    citing = _cited_claim(claim_id="CLM-" + "2" * 32, capture=CAPTURE_TWO)
    store = _EnvelopeStore({CAPTURE_ONE: CONTENT_A, CAPTURE_TWO: CONTENT_A})
    root_path = claim_path(retiring.identity.name)

    advisory = _citing_claims(
        _tree(retiring, citing), root_path=root_path, root=retiring, store=store
    )

    assert [item.claim_path for item in advisory] == [claim_path(citing.identity.name)]
    assert advisory[0].artifact_identity == citing.identity
    # The citing Claim's OWN Capture over the shared content, not the retiring
    # Claim's -- the two never coincide.
    assert advisory[0].capture_digests == (CAPTURE_TWO,)


def test_a_claim_standing_on_different_content_is_not_advised() -> None:
    retiring = _cited_claim(claim_id="CLM-" + "1" * 32, capture=CAPTURE_ONE)
    unrelated = _cited_claim(claim_id="CLM-" + "2" * 32, capture=CAPTURE_TWO)
    store = _EnvelopeStore({CAPTURE_ONE: CONTENT_A, CAPTURE_TWO: CONTENT_B})
    root_path = claim_path(retiring.identity.name)

    assert (
        _citing_claims(_tree(retiring, unrelated), root_path=root_path, root=retiring, store=store)
        == ()
    )


def test_the_retiring_claim_never_advises_about_itself() -> None:
    retiring = _cited_claim(claim_id="CLM-" + "1" * 32, capture=CAPTURE_ONE)
    store = _EnvelopeStore({CAPTURE_ONE: CONTENT_A})
    root_path = claim_path(retiring.identity.name)

    assert _citing_claims(_tree(retiring), root_path=root_path, root=retiring, store=store) == ()


def test_the_queue_row_names_the_citing_claim_and_the_retired_claim_it_stands_on() -> None:
    """The other half of W1-P7: after the retirement, the strand becomes actionable."""
    from cruxible_core.service.playbill_next import _stranded_citation_items

    citing = _cited_claim(claim_id="CLM-" + "2" * 32, capture=CAPTURE_TWO)
    store = _EnvelopeStore({CAPTURE_TWO: CONTENT_A})
    retired_identity = "Claim:CLM-" + "1" * 32

    (row,) = _stranded_citation_items(
        {CONTENT_A: {retired_identity}},
        live=[_accepted(citing)],
        store=store,
    )

    assert row.reason == "claim_cites_retired"
    assert row.severity == "warning"
    assert row.subject_identity == citing.identity.qualified
    assert row.related_identities == (retired_identity,)
    detail = cast(dict[str, object], row.detail)
    assert detail["citing_claim"] == citing.identity.qualified
    assert detail["retired_claims"] == [retired_identity]
    assert detail["capture_digests"] == [CAPTURE_TWO]
    assert row.repair.target == citing.identity.qualified


def test_a_live_claim_standing_on_no_retired_content_produces_no_row() -> None:
    from cruxible_core.service.playbill_next import _stranded_citation_items

    citing = _cited_claim(claim_id="CLM-" + "2" * 32, capture=CAPTURE_TWO)
    store = _EnvelopeStore({CAPTURE_TWO: CONTENT_A})

    assert (
        _stranded_citation_items({CONTENT_B: {"Claim:gone"}}, live=[_accepted(citing)], store=store)
        == ()
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


def copied_from_world(root: Path):  # type: ignore[no-untyped-def]
    """Build the reachable stranding world and retire the Claim that was copied.

    Two Claims read one span of one working source. Each mints its own Capture
    envelope -- they must, every builder binds claim_id -- and both envelopes
    commit to the same selected bytes. Retiring the Claim that observed the span
    leaves the Claim that copied it standing on evidence whose asserter is gone.

    Returns the instance, its owner, and the coordinator and actor that authored
    it -- callers repairing the strand author through the same coordinator, which
    is what the queue row's `playbill.authoring.create` names.
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

    retire = service_retire_claim(
        instance,
        claim_id=SOURCE_CLAIM_ID,
        request=ClaimRetireRequestV1(
            mode="submit",
            reason="was-rescinded",
            expected_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        ),
        actor=actor,
    )
    _activate_retirement(instance, owner, retire)
    return instance, owner, coordinator, actor


def _cited_captures(instance, claim_id: str) -> dict[str, str]:  # type: ignore[no-untyped-def]
    path = claim_path(claim_id)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    return claim_cited_content_digests(
        parse_claim(tree[path], path=path),
        store=instance.body_store(),
    )


def test_retiring_a_source_claim_makes_the_queue_name_the_claim_that_copied_it(
    tmp_path: Path,
) -> None:
    """The copied_from world, end to end: retire the source, the copy is named."""
    from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
    from cruxible_core.service.playbill_next import (
        PlaybillNextRequestV1,
        service_playbill_next,
    )

    instance, _owner, _coordinator, _actor = copied_from_world(tmp_path)
    source_id, copy_id = SOURCE_CLAIM_ID, COPY_CLAIM_ID

    # Distinct Captures, one commitment: exactly the shape the join is keyed on,
    # and the reason a Capture-keyed match would return nothing here.
    source_captures = _cited_captures(instance, source_id)
    copy_captures = _cited_captures(instance, copy_id)
    assert set(source_captures) & set(copy_captures) == set()
    assert set(source_captures.values()) & set(copy_captures.values())

    result = service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            evaluation_time="2026-08-20T00:00:00.000000Z",
            access_profile=CoverageAccessProfileV1(
                profile_id="stranded-citation-test",
                permitted_access_classes=("instance", "public"),
            ),
        ),
    )
    stranded = [item for item in result.items if item.reason == "claim_cites_retired"]

    assert [item.subject_identity for item in stranded] == [f"Claim:{copy_id}"]
    (row,) = stranded
    assert row.related_identities == (f"Claim:{source_id}",)
    assert row.repair.operation == "playbill.claim.retire"
    assert row.repair.arguments == {"claim_id": copy_id}
    detail = cast(dict[str, object], row.detail)
    assert detail["capture_digests"] == sorted(copy_captures)
