"""The ClaimType laws run no vocabulary reuse law.

Maintainer ruling ``dev.decision/reuse-removal-laws-0929``: the ClaimType law
revisions accept adjacent vocabulary without any ``semantic.*`` Claim and record
no reuse evidence. The earlier revisions that ran it are not installed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.captures import (
    capture_contract_digest,
    foreign_source_capture_contract,
)
from cruxible_client.contracts.claim_types import (
    V7_FIELDS,
    ClaimType,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
    render_claim_type,
)
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.laws import (
    ACCEPTANCE_LAWS,
    CLAIM_TYPE_LAW_V5_REVISION_5,
    CLAIM_TYPE_V5_ACCEPTANCE_LAW,
    AcceptanceLawRegistry,
)
from cruxible_client.contracts.policies import ClaimEvidenceAdmissionPolicyV2
from cruxible_core.claims.claim_type_inputs import ClaimTypeInputRecord
from cruxible_core.proposals.proposals import (
    evaluate_proposal_tree,
)
from cruxible_core.proposals.settlement import parse_change_set_record
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._claim_type_support import defaulted_claim_type_input_example
from tests.core_support._support import initialize_local
from tests.test_indexes.test_resolution_contracts import _accept_tree

TIMESTAMP = "2026-09-29T12:00:00.000000Z"
TAG = "playbill-claim-type-v5"


def _claim_type(
    tree: dict[str, bytes],
    predicate: str,
    subject_kind: str,
    *,
    object_subject_kind: str | None = None,
) -> ClaimType:
    payload = defaulted_claim_type_input_example().model_dump(mode="json")
    payload.update(predicate=predicate, allowed_subject_kinds=[subject_kind])
    if object_subject_kind is not None:
        payload.pop("literal_schema", None)
        payload.update(object_kind="subject", allowed_object_subject_kinds=[object_subject_kind])
    return _historical_v5(ClaimTypeInputRecord.model_validate(payload), tree)


def _historical_v5(value: ClaimTypeInputRecord, tree: dict[str, bytes]) -> ClaimType:
    """The v5 ClaimType the reuse law judged, built as the historical fixture it is.

    Current authoring writes only v7; these laws govern v5 history, so the
    fixture states the v5 artifact the pre-v7 lowering produced for this input:
    each rule names its anticipated foreign-source contract by exact digest.
    """

    digests = {
        foreign_source_capture_contract(source_id).identity.qualified: capture_contract_digest(
            foreign_source_capture_contract(source_id)
        ).tagged
        for source_id in value.anticipated_source_ids
    }
    payload = value.model_dump(mode="json", exclude={"anticipated_source_ids"})
    for field in V7_FIELDS:
        payload.pop(field, None)
    rules = []
    for rule in payload["evidence_admission_policy"]["rules"]:
        rule = dict(rule)
        rule["capture_contract_digests"] = sorted(
            digests[item] for item in rule.pop("capture_contracts")
        )
        rule["evidence_kinds"] = ["self_asserted"]
        rules.append(rule)
    path = claim_type_path(value.predicate)
    predecessor = None if path not in tree else parse_claim_type(tree[path], path=path)
    lowered = ClaimType.model_validate(
        {
            **payload,
            "artifact_format": TAG,
            "identity": {"kind": "ClaimType", "name": value.predicate},
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV2.model_validate(
                {"rules": rules}
            ).model_dump(mode="json"),
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=(
                    None if predecessor is None else claim_type_digest(predecessor).tagged
                )
            ).model_dump(mode="json"),
        }
    )
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


def test_a_registry_without_the_current_law_refuses_its_generations(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    parent = instance.accepted_coordinate()
    parent_tree = instance.tree_at(parent.git_oid)
    _accept_tree(instance, owner, _with(instance, *_BATCH), timestamp=TIMESTAMP, proposal_name="b")
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    record_path = max(path for path in tree if path.startswith("changesets/"))
    record = parse_change_set_record(tree[record_path], path=record_path)

    # A registry that does not install the recorded law (another release's):
    # the recorded digest is unknown, so its generations refuse.
    previous = AcceptanceLawRegistry(
        tuple(
            law
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
