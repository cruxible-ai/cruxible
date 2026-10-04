"""The vocabulary reuse law is gone from current ClaimType laws, not from history.

Maintainer ruling ``dev.decision/reuse-removal-laws-0929``: the current ClaimType
law revisions accept adjacent vocabulary without any ``semantic.*`` Claim and
record no reuse evidence. The previous revisions stay installed as historical
laws so that generations and pending proposals judged under them still settle
and replay byte for byte.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cruxible_client.contracts.claim_types import ClaimType, claim_type_path, render_claim_type
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.laws import (
    ACCEPTANCE_LAWS,
    CLAIM_TYPE_LAW_V5_REVISION_4,
    CLAIM_TYPE_LAW_V5_REVISION_5,
    CLAIM_TYPE_V5_ACCEPTANCE_LAW,
    CLAIM_TYPE_V5_REVISION_4_ACCEPTANCE_LAW,
    AcceptanceLawRegistry,
)
from cruxible_core.claims.claim_type_inputs import (
    ClaimTypeInputRecord,
    claim_type_input_template,
    lower_claim_type_input,
)
from cruxible_core.proposals.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
    evaluate_proposal_tree,
)
from cruxible_core.proposals.settlement import ChangeActorBinding, parse_change_set_record
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._support import client_material, initialize_local
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_ledger.test_activation import _sign

TIMESTAMP = "2026-09-29T12:00:00.000000Z"
TAG = "playbill-claim-type-v5"


def _claim_type(
    tree: dict[str, bytes],
    predicate: str,
    subject_kind: str,
    *,
    object_subject_kind: str | None = None,
) -> ClaimType:
    payload = claim_type_input_template().model_dump(mode="json")
    payload.update(predicate=predicate, allowed_subject_kinds=[subject_kind])
    if object_subject_kind is not None:
        payload.pop("literal_schema", None)
        payload.update(object_kind="subject", allowed_object_subject_kinds=[object_subject_kind])
    lowered = lower_claim_type_input(ClaimTypeInputRecord.model_validate(payload), tree=tree)
    assert lowered.artifact_format == TAG
    return lowered


def _with(instance: PlaybillInstance, *types: tuple[str, str, str | None]) -> dict[str, bytes]:
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    for predicate, subject_kind, object_kind in types:
        claim_type = _claim_type(tree, predicate, subject_kind, object_subject_kind=object_kind)
        tree[claim_type_path(predicate)] = render_claim_type(claim_type)
    return tree


_BATCH = (
    ("dev.batch.title", "dev.batch", None),
    ("dev.batch.belongs_to_track", "dev.batch", "dev.track"),
)
_ADJACENT = (
    ("dev.track.title", "dev.track", None),
    ("dev.roadmap_item.belongs_to_track", "dev.roadmap_item", "dev.track"),
)


def _claim_type_evidence(instance: PlaybillInstance) -> dict[str, tuple[str, dict[str, object]]]:
    """Law digest and member result of every ClaimType in the latest generation."""

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    record_path = max(path for path in tree if path.startswith("changesets/"))
    record = parse_change_set_record(tree[record_path], path=record_path)
    return {
        item.path: (item.law_digest, item.result)
        for item in record.law_evidence
        if item.path.startswith("claim-types/")
    }


def _replay_from_genesis(instance: PlaybillInstance) -> None:
    for checkpoint in instance.root.rglob("*checkpoint*"):
        if checkpoint.is_dir():
            shutil.rmtree(checkpoint, ignore_errors=True)
    instance.refresh()


def test_adjacent_vocabulary_is_accepted_without_semantic_claims(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _accept_tree(instance, owner, _with(instance, *_BATCH), timestamp=TIMESTAMP, proposal_name="b")

    tree = _with(instance, *_ADJACENT)
    assert not any("semantic." in path for path in tree)
    _accept_tree(instance, owner, tree, timestamp=TIMESTAMP, proposal_name="adjacent")

    evidence = _claim_type_evidence(instance)
    assert set(evidence) == {
        claim_type_path("dev.track.title"),
        claim_type_path("dev.roadmap_item.belongs_to_track"),
    }
    for law_digest, result in evidence.values():
        assert law_digest == CLAIM_TYPE_LAW_V5_REVISION_5.digest
        assert "reuse" not in result
    _replay_from_genesis(instance)


def test_simultaneous_adjacent_types_are_accepted_together(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    current = instance.accepted_coordinate()
    base_tree = instance.tree_at(current.git_oid)

    evaluation = evaluate_proposal_tree(
        base_tree=base_tree,
        current_tree=base_tree,
        proposed_tree=_with(instance, *_BATCH, *_ADJACENT),
        current=current,
        bodies=instance.body_store(),
        timestamp=TIMESTAMP,
        rebased=False,
        actor_id="owner",
    )

    assert evaluation.diagnostics == ()
    assert evaluation.candidate is not None


def test_historical_reuse_law_generation_replays_byte_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, owner = initialize_local(tmp_path)
    # Judge the first generation under the pre-removal law, as deployed history was.
    monkeypatch.setitem(
        ACCEPTANCE_LAWS._current_by_tag, TAG, CLAIM_TYPE_V5_REVISION_4_ACCEPTANCE_LAW
    )
    _accept_tree(instance, owner, _with(instance, *_BATCH), timestamp=TIMESTAMP, proposal_name="b")
    monkeypatch.setitem(ACCEPTANCE_LAWS._current_by_tag, TAG, CLAIM_TYPE_V5_ACCEPTANCE_LAW)

    historical = _claim_type_evidence(instance)
    assert len(historical) == 2
    for law_digest, result in historical.values():
        assert law_digest == CLAIM_TYPE_LAW_V5_REVISION_4.digest
        reuse = result["reuse"]
        assert isinstance(reuse, dict)
        assert reuse["tag"] == "playbill-vocabulary-reuse-law-evidence-v1"
        assert reuse["verdict"] == "satisfied"

    _replay_from_genesis(instance)
    _accept_tree(
        instance, owner, _with(instance, *_ADJACENT), timestamp=TIMESTAMP, proposal_name="adj"
    )
    for law_digest, result in _claim_type_evidence(instance).values():
        assert law_digest == CLAIM_TYPE_LAW_V5_REVISION_5.digest
        assert "reuse" not in result
    # Both the historical and the current generation reproduce from genesis.
    _replay_from_genesis(instance)


def test_pending_proposal_judged_under_the_historical_law_still_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, owner = initialize_local(tmp_path)
    _accept_tree(instance, owner, _with(instance, *_BATCH), timestamp=TIMESTAMP, proposal_name="b")
    base = instance.accepted_coordinate()
    tree = _with(instance, ("dev.track.owner", "dev.track", None))

    monkeypatch.setitem(
        ACCEPTANCE_LAWS._current_by_tag, TAG, CLAIM_TYPE_V5_REVISION_4_ACCEPTANCE_LAW
    )
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/pending-historical",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp=TIMESTAMP,
    )
    # The upgrade lands between submission and settlement.
    monkeypatch.setitem(ACCEPTANCE_LAWS._current_by_tag, TAG, CLAIM_TYPE_V5_ACCEPTANCE_LAW)
    assert proposed.candidate is not None
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

    _replay_from_genesis(instance)

    ((law_digest, result),) = _claim_type_evidence(instance).values()
    assert law_digest == CLAIM_TYPE_LAW_V5_REVISION_4.digest
    assert isinstance(result["reuse"], dict)


def test_a_registry_without_the_current_law_refuses_its_generations(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    parent = instance.accepted_coordinate()
    parent_tree = instance.tree_at(parent.git_oid)
    _accept_tree(instance, owner, _with(instance, *_BATCH), timestamp=TIMESTAMP, proposal_name="b")
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    record_path = max(path for path in tree if path.startswith("changesets/"))
    record = parse_change_set_record(tree[record_path], path=record_path)

    # A registry as a pre-removal daemon installs it: the old revision is current
    # and the new digest is unknown.
    previous = AcceptanceLawRegistry(
        tuple(
            CLAIM_TYPE_V5_REVISION_4_ACCEPTANCE_LAW.__class__(
                coordinate=law.coordinate,
                artifact_kind=law.artifact_kind,
                artifact_tag=law.artifact_tag,
                current=law is CLAIM_TYPE_V5_REVISION_4_ACCEPTANCE_LAW
                or (law.current and law.artifact_tag != TAG),
            )
            for law in ACCEPTANCE_LAWS._by_coordinate.values()
            if law is not CLAIM_TYPE_V5_ACCEPTANCE_LAW
        )
    )
    with pytest.raises(ProposalIntegrityError, match="cannot be reproduced at its recorded"):
        previous.require_historical(
            identifier=CLAIM_TYPE_LAW_V5_REVISION_5.identifier,
            digest=CLAIM_TYPE_LAW_V5_REVISION_5.digest,
        )
    with pytest.raises(ProposalIntegrityError, match="cannot be reproduced at its recorded"):
        evaluate_proposal_tree(
            base_tree=parent_tree,
            current_tree=parent_tree,
            proposed_tree={
                path: content
                for path, content in tree.items()
                if not path.startswith("changesets/") or path in parent_tree
            },
            current=parent,
            bodies=instance.body_store(),
            timestamp=record.candidate.timestamp,
            rebased=False,
            actor_id="owner",
            acceptance_laws=previous,
            historical_law_coordinates={
                member.path: (member.law_identifier, record.law_digests[member.law_identifier])
                for member in record.members
            },
        )
