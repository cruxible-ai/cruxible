"""Candidate Markdown cards are compiler-pinned derivatives with zero authority."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_wire_succession import TIMESTAMP, _document_tree

from cruxible_client.contracts.canonical import canonical_bytes, manifest_root, semantic_projection
from cruxible_client.contracts.errors import ProposalAdmissionError, ProposalIntegrityError
from cruxible_core.playbill.candidate_cards import (
    CARD_RENDERER_DIGEST,
    candidate_card_path,
    derive_candidate_cards,
    verify_candidate_cards,
)
from cruxible_core.playbill.projection_artifacts import P2_C_ARTIFACT_KINDS
from cruxible_core.playbill.proposals import (
    ProposalReceiveLimits,
    evaluate_proposal_tree,
    validate_proposal_tree,
)


def _artifact(name: str) -> bytes:
    return canonical_bytes(
        {
            "tag": "playbill-procedure-card-fixture-v1",
            "identity": {"kind": "Procedure", "name": name},
        }
    )


def test_cards_cover_changes_removals_and_renames_without_semantic_authority() -> None:
    old = "procedures/old.json"
    new = "procedures/new.json"
    kept = "procedures/kept.json"
    base = {old: _artifact("old"), kept: _artifact("kept")}
    candidate = {new: _artifact("new"), kept: _artifact("kept-v2")}

    rendered = derive_candidate_cards(
        base_tree=base,
        candidate_tree=candidate,
        coordinate="a" * 40,
        artifact_kinds=P2_C_ARTIFACT_KINDS,
    )

    assert rendered[candidate_card_path(old)] == b"removed at " + b"a" * 40 + b"\n"
    assert b"# procedure: Procedure:new" in rendered[candidate_card_path(new)]
    assert b"Procedure:kept-v2" in rendered[candidate_card_path(kept)]
    assert manifest_root(semantic_projection(rendered)) == manifest_root(candidate)
    verify_candidate_cards(
        base_tree=base,
        candidate_tree=rendered,
        coordinate="a" * 40,
        artifact_kinds=P2_C_ARTIFACT_KINDS,
    )


def test_card_verifier_rejects_missing_stale_and_extra_derivatives() -> None:
    path = "procedures/demo.json"
    base: dict[str, bytes] = {}
    rendered = derive_candidate_cards(
        base_tree=base,
        candidate_tree={path: _artifact("demo")},
        coordinate="b" * 40,
        artifact_kinds=P2_C_ARTIFACT_KINDS,
    )
    card = candidate_card_path(path)
    variants = (
        {key: value for key, value in rendered.items() if key != card},
        {**rendered, card: b"stale\n"},
        {**rendered, "cards/procedures/extra.md": b"extra\n"},
    )

    for variant in variants:
        with pytest.raises(ProposalIntegrityError, match="do not reproduce"):
            verify_candidate_cards(
                base_tree=base,
                candidate_tree=variant,
                coordinate="b" * 40,
                artifact_kinds=P2_C_ARTIFACT_KINDS,
            )


def test_proposal_admission_allows_card_rederivation_but_rejects_caller_card_bytes() -> None:
    artifact_path = "procedures/demo.json"
    card_path = candidate_card_path(artifact_path)
    base = {
        artifact_path: _artifact("demo"),
        card_path: b"# procedure: demo\n",
    }
    changed = {artifact_path: _artifact("demo-v2")}

    assert (
        validate_proposal_tree(
            changed,
            limits=ProposalReceiveLimits(),
            base_tree=base,
        )
        == changed
    )
    for caller_tree in (
        {**changed, card_path: b"caller-authored\n"},
        {**changed, "cards/procedures/extra.md": b"caller-authored\n"},
    ):
        with pytest.raises(ProposalAdmissionError, match="daemon-controlled"):
            validate_proposal_tree(
                caller_tree,
                limits=ProposalReceiveLimits(),
                base_tree=base,
            )


def test_exact_renderer_digest_controls_candidate_evaluation(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    base, proposed = _document_tree(instance)
    common = {
        "base_tree": base,
        "current_tree": base,
        "proposed_tree": proposed,
        "current": instance.accepted_coordinate(),
        "bodies": instance.body_store(),
        "timestamp": TIMESTAMP,
        "rebased": False,
        "actor_id": "owner",
    }

    evaluation = evaluate_proposal_tree(
        **common,
        candidate_card_renderer_digest=CARD_RENDERER_DIGEST,
    )
    assert evaluation.candidate is not None
    assert "cards/documents/playbill-design.md" in evaluation.tree
    assert evaluation.candidate.candidate.scope == ("documents/playbill-design.json",)

    with pytest.raises(ProposalIntegrityError, match="renderer differs"):
        evaluate_proposal_tree(
            **common,
            candidate_card_renderer_digest="sha256:" + "0" * 64,
        )
