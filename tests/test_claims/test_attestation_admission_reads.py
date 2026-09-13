"""Served attestation admission uses accepted indexes; replay remains independent."""

from datetime import timedelta
from pathlib import Path

from cruxible_client.contracts.accepted_attestations import (
    attestation_path,
    render_accepted_attestation,
)
from cruxible_client.contracts.authoring.models import (
    AttestationAuthoringPayloadV1,
    ChangeSetAuthoringPayloadV1,
    authoring_member_identity,
)
from cruxible_client.contracts.claim_attestations import (
    accepted_referent_coordinates_from_tree,
    claim_attestation_v2_envelope_digest,
)
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.proposals.proposals import AuthenticatedActor
from tests.test_claims.test_claim_attestation_service import RECORDED_AT, _request
from tests.test_claims.test_claim_type_migrations import _accepted_claim_world
from tests.test_indexes.test_resolution_contracts import _accept_tree


def test_historical_attestation_batch_reuses_exact_principal_and_indexed_referents(
    tmp_path: Path, monkeypatch
) -> None:
    import cruxible_core.evidence.attestation_verification as verification
    import cruxible_core.proposals.proposals as proposals

    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    original = instance.accepted_coordinate()
    records = tuple(
        _request(
            instance,
            owner,
            claim_id,
            tmp_path,
            stance=stance,
            attested_at=RECORDED_AT + timedelta(seconds=i),
        ).attestation
        for i, stance in enumerate(("support", "contradict", "unsure"))
    )
    tree = instance.tree_at(original.git_oid)
    tree[attestation_path(claim_attestation_v2_envelope_digest(records[0]))] = (
        render_accepted_attestation(records[0])
    )
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp="2026-08-28T15:01:00.000000Z",
        proposal_name="advance",
    )
    current = instance.accepted_coordinate()
    # Same referents as retained law evidence, with an explicit historical cutoff.
    expected = accepted_referent_coordinates_from_tree(
        instance.tree_at(current.git_oid), current=AcceptedCoordinate.from_internal(current)
    )
    assert instance.accepted_referent_coordinates(current) == expected
    assert AcceptedCoordinate.from_internal(current) not in (
        instance.accepted_referent_coordinates(original)
    )

    lookups = []
    verified = []
    original_lookup = instance.accepted_principal
    original_verify = verification.verify_attestation_binding

    def principal(at, name):
        if at == AcceptedCoordinate.from_internal(original):
            lookups.append((at, name))
        return original_lookup(at, name)

    def verify(*args, **kwargs):
        verified.append(args[0])
        return original_verify(*args, **kwargs)

    def no_scan(*args, **kwargs):
        raise AssertionError("served admission must not parse the full historical registry/ledger")

    payload = ChangeSetAuthoringPayloadV1(
        members=tuple(
            sorted(
                (AttestationAuthoringPayloadV1(attestation=item) for item in records[1:]),
                key=authoring_member_identity,
            )
        )
    )
    actor = AuthenticatedActor(actor_id="owner")
    with monkeypatch.context() as patch:
        patch.setattr(proposals, "accepted_referent_coordinates_from_tree", no_scan)
        patch.setattr(proposals, "principal_registry_from_tree", no_scan)
        patch.setattr(instance, "accepted_principal", principal)
        patch.setattr(verification, "verify_attestation_binding", verify)
        coordinator = AuthoringIntentCoordinator.for_instance(instance)
        intent = coordinator.create(
            actor=actor, payload=payload, canonical_timestamp="2026-08-28T15:02:00.000000Z"
        ).intent
        result = coordinator.submit(intent.intent_id, actor=actor)
        assert result.status.state == "ready_to_activate", result.model_dump(mode="json")
    assert verified
    assert len(lookups) * 2 == len(verified)
    assert set(lookups) == {(AcceptedCoordinate.from_internal(original), "owner")}
