"""PC-B Claim projection and canonical/provisional parity."""

from __future__ import annotations

from pathlib import Path

from cruxible_core.playbill.projection import ProvisionalProjectionCoordinate
from cruxible_core.playbill.projection_claims import compile_provisional_claim_projection
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.playbill_claims import (
    service_get_playbill_claim,
)
from tests.test_playbill._claim_authoring_support import (
    TIMESTAMP,
    _activate_direct_claim,
    _authoring,
    service_propose_playbill_claim,
)
from tests.test_playbill._support import initialize_local


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
    statement_fact = next(
        fact.value for fact in provisional.facts if fact.schema_id == "playbill.claim.statement"
    )
    assert canonical.statement.subject.model_dump(mode="json") == statement_fact["subject"]
    assert canonical.statement.predicate == statement_fact["predicate"]
    assert canonical.statement.object.model_dump(mode="json") == statement_fact["object"]
    assert canonical.statement.role == statement_fact["role"]
    assert canonical.statement.qualifier == statement_fact["qualifier"]
    assert canonical.statement.lifecycle == "live"
    assert canonical.statement.predecessor_digest is None
    provisional_facts = {
        (fact.schema_id, fact.fact_key): fact.model_dump(mode="json") for fact in provisional.facts
    }
    canonical_facts = {
        (str(fact["schema_id"]), str(fact["fact_key"])): fact for fact in canonical.facts
    }
    assert {key: canonical_facts[key] for key in provisional_facts} == provisional_facts
    assert {
        "playbill.claim.attestation_coverage",
        "playbill.claim.current_verdict",
        "playbill.claim.evidence_basis",
        "playbill.claim.governance",
        "playbill.claim.history",
        "playbill.claim.provenance",
    }.issubset({key[0] for key in canonical_facts})
