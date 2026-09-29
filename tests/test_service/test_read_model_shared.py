"""The three read verbs share one answer for what they all show.

``orient``, ``query`` and ``get`` each show a ClaimType's accepted evidence as
CaptureContract names, and each shows verdict problems as flags. Both come from
one shared derivation, so on the same state the verbs agree.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.compact_query import PlaybillQueryRequestV1
from cruxible_client.contracts.get_reads import PlaybillGetRequestV1
from cruxible_core.service.discovery.compact_query import service_playbill_query
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._knowledge_loop_support import (
    EVALUATION_TIME,
    PREDICATE,
    SUBJECT_KIND,
    seed_claims,
)

_WHEN = datetime.fromisoformat(EVALUATION_TIME)
_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)


@pytest.fixture
def instance(tmp_path: Path) -> Any:
    seeded, _owner = seed_claims(tmp_path)
    return seeded


def test_every_verb_names_the_same_capture_contracts(instance: Any) -> None:
    query_row = service_playbill_query(
        instance,
        request=PlaybillQueryRequestV1.model_validate(
            {"kind": "ClaimType", "where": [{"field": "namespace", "eq": SUBJECT_KIND}]}
        ),
    ).rows[0]
    orient_kind = next(
        item for item in service_playbill_orient(instance).kinds if item.kind == SUBJECT_KIND
    )
    orient_evidence = orient_kind.evidence or next(
        item.evidence for item in orient_kind.predicates if item.predicate == PREDICATE
    )
    card = service_playbill_get(
        instance,
        request=PlaybillGetRequestV1(ref=f"ClaimType:{PREDICATE}", evaluation_time=_WHEN),
        access=_ACCESS,
    ).card
    assert card is not None

    assert query_row["evidence"]
    assert tuple(query_row["evidence"]) == orient_evidence
    assert card.model_dump()["evidence"] == tuple(
        f"CaptureContract:{name}" for name in orient_evidence
    )
    assert not any(name.startswith("sha256:") for name in orient_evidence)


def test_query_rows_and_get_cards_carry_the_same_flags_for_a_held_conflict(
    tmp_path: Path,
) -> None:
    from datetime import timedelta

    from cruxible_client.contracts.get_reads import (
        PlaybillGetClaimCardV1,
        PlaybillGetSubjectCardV1,
    )
    from tests.core_support._candidate_support import submit_query_definition_candidate
    from tests.core_support._knowledge_loop_support import (
        QUERY_NAME,
        TIMESTAMP,
        accept_proposal,
        work_item_query,
    )
    from tests.test_integration.test_next_closed_loop import EVALUATION_TIME as LATER
    from tests.test_integration.test_next_closed_loop import _current_claim
    from tests.test_integration.test_next_holds import _all_claims, _attest
    from tests.test_service.test_get_reads import _contend

    instance, owner = seed_claims(tmp_path)
    accept_proposal(
        instance,
        owner,
        submit_query_definition_candidate(
            instance,
            query=work_item_query(),
            actor_id="owner",
            proposal_name="work-item-query",
            timestamp=TIMESTAMP,
        ),
    )
    first = _current_claim(instance)
    _contend(instance, owner, first, "blocked", "hold-conflict")
    contenders = [
        claim
        for claim in _all_claims(instance)
        if claim.statement.subject.artifact_path.endswith("wi-42.json")
    ]
    for offset, claim in enumerate(contenders):
        _attest(instance, owner, claim, tmp_path, at=LATER - timedelta(minutes=2 - offset))

    def get_card(ref: str) -> Any:
        return service_playbill_get(
            instance,
            request=PlaybillGetRequestV1(ref=ref, evaluation_time=LATER),
            access=_ACCESS,
        ).card

    def query_row(**fields: Any) -> dict[str, Any]:
        result = service_playbill_query(
            instance,
            request=PlaybillQueryRequestV1.model_validate({**fields, "evaluation_time": LATER}),
        )
        return next(
            row for row in result.rows if row.get("subject_id", row.get("item_id")) == "wi-42"
        )

    claim_card = get_card(first.identity.name)
    subject_card = get_card(f"{SUBJECT_KIND}/wi-42")
    assert isinstance(claim_card, PlaybillGetClaimCardV1)
    assert isinstance(subject_card, PlaybillGetSubjectCardV1)
    assert "unsure_hold" in claim_card.flags

    compact = query_row(kind=SUBJECT_KIND, select=["status"])
    named = query_row(name=QUERY_NAME)
    (subject_row,) = subject_card.claims

    assert tuple(compact["flags"]) == subject_row.flags
    assert tuple(named["flags"]) == subject_row.flags
    assert set(claim_card.flags) <= set(subject_row.flags)


def test_governed_query_flags_are_derived_over_the_whole_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named or spec query reads some Claims; their flags see every slot contender."""

    from cruxible_core.service.discovery import compact_query as compact_module
    from tests.test_integration.test_next_closed_loop import _current_claim
    from tests.test_integration.test_next_holds import _all_claims
    from tests.test_service.test_get_reads import _contend

    instance, owner = seed_claims(tmp_path)
    first = _current_claim(instance)
    _contend(instance, owner, first, "blocked", "slot-contender")
    slot = {
        claim.identity.qualified
        for claim in _all_claims(instance)
        if claim.statement.subject.artifact_path.endswith("wi-42.json")
    }
    assert len(slot) == 2
    seen: list[set[str]] = []

    def capture(*_args: Any, identities: Any, **_kwargs: Any) -> dict[str, tuple[str, ...]]:
        seen.append(set(identities))
        return {identity: ("unsure_hold",) for identity in identities}

    monkeypatch.setattr(compact_module, "claim_flags", capture)
    from cruxible_client.contracts.claims import claim_path

    flags = compact_module._flags_for_paths(
        instance,
        instance.accepted_coordinate(),
        {claim_path(first.identity.name)},
        _WHEN,
    )

    assert seen == [slot]
    assert flags == {claim_path(first.identity.name): ("unsure_hold",)}


def _accept_tree(instance: Any, tree: dict[str, bytes], name: str) -> None:
    """Accept a hand-built tree as one generation (no authoring reuse review)."""

    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
    from cruxible_core.proposals.settlement import ChangeActorBinding
    from tests.core_support._support import client_material
    from tests.test_ledger.test_activation import _sign

    base = instance.accepted_coordinate()
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{name}", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp="2026-08-24T16:00:00.000000Z",
    )
    assert proposed.candidate is not None, proposed.evaluation
    assert proposed.evaluation.evaluated_tree_oid is not None
    bundle = instance.prepare_generation(
        base=base,
        candidate_tree=instance.proposal_tree(proposed.evaluation.evaluated_tree_oid),
        candidate=proposed.candidate,
        approvals=(
            _sign(
                client_material(instance.root.parent, instance),
                proposed.candidate.candidate_digest,
                base.semantic_root,
            ),
        ),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        proposal_actor_id="owner",
        sequence=len(instance.accepted_history()),
    )
    publisher = instance.activation_publisher()
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"
    instance.refresh()


def test_get_cards_name_predicates_by_the_shared_rule_as_orient_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Addendum 2 on get: no last-segment names, and the same names orient shows."""

    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.claim_types import (
        ClaimType,
        claim_type_digest,
        claim_type_path,
        render_claim_type,
    )
    from cruxible_client.contracts.get_reads import (
        PlaybillGetClaimCardV1,
        PlaybillGetSubjectCardV1,
    )
    from cruxible_core.proposals import proposals as proposals_module
    from cruxible_core.service.discovery.field_names import resolve_field
    from tests.core_support._claim_authoring_support import service_propose_playbill_claim
    from tests.core_support._knowledge_loop_support import activate, authoring

    instance, owner = seed_claims(tmp_path)
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        template = projection.typed.source(f"ClaimType:{PREDICATE}")
    assert isinstance(template, ClaimType)
    collisions = {
        predicate: template.model_copy(
            update={
                "identity": ArtifactIdentity(kind="ClaimType", name=predicate),
                "predicate": predicate,
            }
        )
        for predicate in (
            "other.status",
            "third.status",
            f"{SUBJECT_KIND}.other.status",
            f"{SUBJECT_KIND}.subject_id",
        )
    }
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    for predicate, claim_type in collisions.items():
        tree[claim_type_path(predicate)] = render_claim_type(claim_type)
    # The collision fixture is deliberately near-duplicate vocabulary (every leaf
    # is ``status``); the reuse law's distinction review is not under test here.
    reuse = proposals_module.evaluate_vocabulary_reuse
    with monkeypatch.context() as patch:
        patch.setattr(
            proposals_module,
            "evaluate_vocabulary_reuse",
            lambda request, **kw: reuse(request, **{**kw, "accepted_interfaces": ()}),
        )
        _accept_tree(instance, tree, "collision-claim-types")
    for index, (predicate, claim_type) in enumerate(collisions.items()):
        request = authoring("wi-42", "ready", with_claim_type=False)
        request = request.model_copy(
            update={
                "statement": request.statement.model_copy(
                    update={
                        "claim_type": claim_type.identity,
                        "claim_type_digest": claim_type_digest(claim_type).tagged,
                        "predicate": predicate,
                    }
                )
            }
        )
        activate(
            instance,
            owner,
            service_propose_playbill_claim(
                instance,
                authoring=request,
                actor_id="owner",
                proposal_name=f"collision-{index}",
                timestamp=f"2026-08-24T17:00:0{index}.000000Z",
            ),
        )

    subject = service_playbill_get(
        instance,
        request=PlaybillGetRequestV1(ref=f"{SUBJECT_KIND}/wi-42", evaluation_time=_WHEN),
        access=_ACCESS,
    ).card
    assert isinstance(subject, PlaybillGetSubjectCardV1)
    orient_kind = next(
        item for item in service_playbill_orient(instance).kinds if item.kind == SUBJECT_KIND
    )
    advertised = {item.predicate: item.name for item in orient_kind.predicates}

    assert advertised == {
        PREDICATE: "status",
        "other.status": "other.status",
        "third.status": "third.status",
        f"{SUBJECT_KIND}.other.status": f"{SUBJECT_KIND}.other.status",
        f"{SUBJECT_KIND}.subject_id": f"{SUBJECT_KIND}.subject_id",
    }
    assert sorted(row.predicate for row in subject.claims) == sorted(advertised.values())
    for predicate, name in advertised.items():
        assert resolve_field(name, SUBJECT_KIND, frozenset(advertised)) == predicate

    for row in subject.claims:
        assert isinstance(row.claim, str)
        claim = service_playbill_get(
            instance,
            request=PlaybillGetRequestV1(ref=row.claim, evaluation_time=_WHEN),
            access=_ACCESS,
        ).card
        assert isinstance(claim, PlaybillGetClaimCardV1)
        assert claim.predicate == advertised[claim.predicate_full] == row.predicate
