"""PC-B Claim projection and canonical/provisional parity."""

from __future__ import annotations

from pathlib import Path

from cruxible_core.playbill.projection import ProvisionalProjectionCoordinate
from cruxible_core.playbill.projection_claims import compile_provisional_claim_projection
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.playbill_claims import (
    service_get_playbill_claim,
    service_propose_playbill_claim,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_direct_claim_authoring import (
    TIMESTAMP,
    _activate_direct_claim,
    _authoring,
)


def test_provisional_and_rebuilt_canonical_claim_facts_match(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    base = instance.accepted_coordinate()
    proposed = service_propose_playbill_claim(
        instance,
        authoring=_authoring(),
        actor_id="owner",
        proposal_name="projection-parity",
        timestamp=TIMESTAMP,
    )
    candidate = proposed.proposal.proposal.candidate
    evaluated_oid = proposed.proposal.proposal.evaluation.evaluated_tree_oid
    assert candidate is not None and evaluated_oid is not None
    coordinate = ProvisionalProjectionCoordinate(
        canonical=base,
        candidate=candidate.candidate,
        candidate_digest=candidate.candidate_digest,
    )
    provisional = compile_provisional_claim_projection(
        instance.proposal_tree(evaluated_oid),
        coordinate=coordinate,
    ).claim(proposed.claim_identity)
    assert provisional is not None
    assert provisional.coordinate_kind == "provisional"
    assert provisional.envelope.artifact_digest == proposed.artifact_digest

    _activate_direct_claim(instance, owner, proposed)
    canonical = service_get_playbill_claim(
        instance,
        identity=proposed.claim_identity,
        at=PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )
    assert canonical.envelope["artifact_digest"] == provisional.envelope.artifact_digest
    assert canonical.facts == tuple(fact.model_dump(mode="json") for fact in provisional.facts)
