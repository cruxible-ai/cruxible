"""Candidate Markdown cards are compiler-pinned derivatives with zero authority."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_wire_succession import TIMESTAMP, _document_tree

from cruxible_client.contracts.canonical import canonical_bytes, manifest_root, semantic_projection
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_core.playbill.candidate_cards import (
    CARD_RENDERER_DIGEST,
    candidate_card_path,
    derive_candidate_cards,
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
    assert b"# procedure: new" in rendered[candidate_card_path(new)]
    assert b"# procedure: kept" in rendered[candidate_card_path(kept)]
    assert manifest_root(semantic_projection(rendered)) == manifest_root(candidate)
    # Re-derivation is idempotent, which is the property settlement and recovery
    # rely on when they compare the whole re-evaluated tree.
    assert (
        derive_candidate_cards(
            base_tree=base,
            candidate_tree=rendered,
            coordinate="a" * 40,
            artifact_kinds=P2_C_ARTIFACT_KINDS,
        )
        == rendered
    )


def test_card_rederivation_replaces_missing_stale_and_extra_derivatives() -> None:
    """Settlement and recovery compare the re-derived tree, so this is the check.

    HOLD-class: `verify_candidate_cards` had no production caller -- both sites
    compare the entire re-evaluated tree, which subsumes it -- so it was removed
    and this oracle now exercises the derivation those sites really run.
    """

    path = "procedures/demo.json"
    base: dict[str, bytes] = {}
    candidate = {path: _artifact("demo")}
    rendered = derive_candidate_cards(
        base_tree=base,
        candidate_tree=candidate,
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
        assert variant != rendered
        assert (
            derive_candidate_cards(
                base_tree=base,
                candidate_tree=variant,
                coordinate="b" * 40,
                artifact_kinds=P2_C_ARTIFACT_KINDS,
            )
            == rendered
        )


def test_derivation_skips_a_member_the_evaluator_will_refuse_typed() -> None:
    """Cards are derived before member evaluation, so they must not pre-empt it.

    A member whose bytes are not canonical artifact JSON is owed the evaluator's
    typed format diagnostic and its inventory row; aborting derivation first turns
    that into an untyped integrity error the caller cannot repair.
    """

    good = "procedures/good.json"
    bad = "procedures/bad.json"
    candidate = {good: _artifact("good"), bad: b"not canonical artifact bytes\n"}

    rendered = derive_candidate_cards(
        base_tree={},
        candidate_tree=candidate,
        coordinate="c" * 40,
        artifact_kinds=P2_C_ARTIFACT_KINDS,
    )

    assert candidate_card_path(bad) not in rendered
    assert b"# procedure: good" in rendered[candidate_card_path(good)]
    assert rendered[bad] == candidate[bad]


def test_proposal_admission_admits_card_paths_and_re_derives_their_bytes() -> None:
    """Admission accepts a tree that already carries cards; derivation owns the bytes.

    HOLD-class: the round-0 oracle "rejects caller card bytes" is SUPERSEDED. Its
    changed-path refusal made fetch -> edit -> resubmit impossible, because the
    evaluated candidate tree, the accepted projection and `playbill review` all
    hand the caller a tree with cards in it, and every one of those cards is a
    changed daemon-controlled path. The changed-path guard now carries the same
    card exemption its removed-path twin already had. Caller card bytes keep zero
    authority for the reason the retraction relies on: derive_candidate_cards
    strips every card path and re-derives it, which is what this oracle asserts.
    """

    artifact_path = "procedures/demo.json"
    card_path = candidate_card_path(artifact_path)
    base = {
        artifact_path: _artifact("demo"),
        card_path: b"# procedure: demo\n",
    }
    changed = {artifact_path: _artifact("demo-v2")}
    resubmitted = {**changed, card_path: b"caller-authored\n"}
    invented = {**changed, "cards/procedures/extra.md": b"caller-authored\n"}

    for caller_tree in (changed, resubmitted, invented):
        assert (
            validate_proposal_tree(
                caller_tree,
                limits=ProposalReceiveLimits(),
                base_tree=base,
            )
            == caller_tree
        )
        derived = derive_candidate_cards(
            base_tree=base,
            candidate_tree=caller_tree,
            coordinate="d" * 40,
            artifact_kinds=P2_C_ARTIFACT_KINDS,
        )
        assert b"# procedure: demo-v2" in derived[card_path]
        assert b"caller-authored" not in derived[card_path]
        assert "cards/procedures/extra.md" not in derived


def test_an_evaluated_card_bearing_tree_can_be_edited_and_resubmitted(tmp_path: Path) -> None:
    """The fetch -> edit -> resubmit loop must survive its own derivative cards."""

    instance, _owner = initialize_local(tmp_path)
    base, proposed = _document_tree(instance)
    evaluation = evaluate_proposal_tree(
        base_tree=base,
        current_tree=base,
        proposed_tree=proposed,
        current=instance.accepted_coordinate(),
        bodies=instance.body_store(),
        timestamp=TIMESTAMP,
        rebased=False,
        actor_id="owner",
        candidate_card_renderer_digest=CARD_RENDERER_DIGEST,
    )
    assert evaluation.candidate is not None
    fetched = dict(evaluation.tree)
    assert [path for path in fetched if path.startswith("cards/")]

    assert (
        validate_proposal_tree(
            fetched,
            limits=ProposalReceiveLimits(),
            base_tree=base,
        )
        == fetched
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
