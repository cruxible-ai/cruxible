"""Candidate Markdown cards are compiler-pinned derivatives with zero authority."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.test_playbill._support import client_material, initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_wire_succession import DOCUMENT_PATH, TIMESTAMP, _document_tree

from cruxible_client.contracts.canonical import canonical_bytes, manifest_root, semantic_projection
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    render_document,
)
from cruxible_client.contracts.errors import (
    CanonicalEncodingError,
    ProposalIntegrityError,
    SettlementIntegrityError,
)
from cruxible_client.contracts.proposal_models import ProposalResult
from cruxible_core.playbill.candidate_cards import (
    CARD_RENDERER_DIGEST,
    candidate_card_path,
    derive_candidate_cards,
    render_candidate_card,
)
from cruxible_core.playbill.compiler import artifact_kinds_for_compiler
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection_artifacts import P2_C_ARTIFACT_KINDS
from cruxible_core.playbill.proposals import (
    AuthenticatedActor,
    ProposalAdmissionRequest,
    ProposalReceiveLimits,
    evaluate_proposal_tree,
    validate_proposal_tree,
)
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.playbill.settlement import ChangeActorBinding


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


OTHER_DOCUMENT_PATH = "documents/other.json"
INVENTED_CARD_PATH = "cards/procedures/extra.md"
CALLER_MARKER = b"caller-authored"
TOMBSTONE_LIE = b"removed at " + b"0" * 64 + b"\n"
SOURCE_COMPILATION_DIGEST = "sha256:" + "77" * 32


def _submit(instance: PlaybillInstance, tree: dict[str, bytes], *, ref: str) -> ProposalResult:
    """Drive the one ProposalService.submit every service and HTTP route calls."""

    base = instance.accepted_coordinate()
    return instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{ref}",
            proposed_base_oid=base.git_oid,
            source_compilation_digest=SOURCE_COMPILATION_DIGEST,
        ),
        candidate_tree=tree,
        timestamp=TIMESTAMP,
    )


def _accept(instance: PlaybillInstance, result: ProposalResult) -> dict[str, bytes]:
    """Approve and activate through production settlement; return the accepted tree."""

    candidate = result.candidate
    assert candidate is not None
    approver = client_material(instance.root.parent, instance)
    approval = _sign(
        approver,
        candidate.candidate_digest,
        candidate.candidate.parent_semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=result.admission.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter=approver.principal.principal_id,
    )
    receipt = service_activate_playbill_proposal(
        instance,
        proposal_id=result.admission.proposal_id,
        activated_by="owner",
    )
    assert receipt.status == "accepted"
    return instance.proposal_tree(instance.accepted_coordinate().git_oid)


def _accepted_card_bearing_tree(instance: PlaybillInstance) -> dict[str, bytes]:
    """Accept one Document so main already carries a derivative card."""

    _base, proposed = _document_tree(instance)
    accepted = _accept(instance, _submit(instance, proposed, ref="first"))
    assert candidate_card_path(DOCUMENT_PATH) in accepted
    return accepted


def _other_document(instance: PlaybillInstance) -> bytes:
    body = instance.store_document_body(b"other")
    return render_document(
        DocumentShell(
            identity="document:other",
            document_kind="design",
            title="Other design",
            media_type="text/markdown",
            body_digest=body.digest,
            authority=DocumentAuthority(required_tier="graph_write"),
            governance_scope=("project:playbill",),
            lifecycle=DocumentLifecycle(revision=1),
        )
    )


@pytest.mark.parametrize(
    "forgery",
    [
        pytest.param(
            {
                candidate_card_path(DOCUMENT_PATH): CALLER_MARKER + b" existing\n",
                candidate_card_path(OTHER_DOCUMENT_PATH): CALLER_MARKER + b" new\n",
                INVENTED_CARD_PATH: CALLER_MARKER + b" invented\n",
            },
            id="caller-bytes-and-invented-card",
        ),
        pytest.param(
            {
                candidate_card_path(DOCUMENT_PATH): TOMBSTONE_LIE,
                candidate_card_path(OTHER_DOCUMENT_PATH): TOMBSTONE_LIE,
            },
            id="tombstone-lie",
        ),
    ],
)
def test_forged_cards_never_reach_the_evaluated_or_accepted_tree(
    tmp_path: Path, forgery: dict[str, bytes]
) -> None:
    """End-to-end twin of the unit oracle above, through submit -> approve -> activate.

    A caller resubmits the accepted card-bearing tree with one real semantic change
    and forged bytes for the existing card, the new member's card, an invented card
    path, and a card that lies about the members' settlement. The evaluated tree the
    daemon commits and the accepted tree activation settles carry only the base
    card and the re-rendered card; the forged bytes survive nowhere but the
    admitted commit, which is never settled.
    """

    instance, _owner = initialize_local(tmp_path)
    accepted = _accepted_card_bearing_tree(instance)
    card = candidate_card_path(DOCUMENT_PATH)
    other_card = candidate_card_path(OTHER_DOCUMENT_PATH)
    other = _other_document(instance)
    expected_other_card = render_candidate_card(
        OTHER_DOCUMENT_PATH,
        other,
        artifact_kinds=artifact_kinds_for_compiler(instance.accepted_coordinate().compiler),
    )
    forged_markers = (CALLER_MARKER, TOMBSTONE_LIE)

    result = _submit(
        instance,
        {**accepted, OTHER_DOCUMENT_PATH: other, **forgery},
        ref="second",
    )

    assert result.candidate is not None
    assert result.evaluation.evaluated_tree_oid is not None
    assert result.candidate.candidate.scope == (OTHER_DOCUMENT_PATH,)
    evaluated = instance.proposal_tree(result.evaluation.evaluated_tree_oid)
    accepted_after = _accept(instance, result)
    for tree in (evaluated, accepted_after):
        assert tree[DOCUMENT_PATH] == accepted[DOCUMENT_PATH]
        assert tree[OTHER_DOCUMENT_PATH] == other
        assert tree[card] == accepted[card]
        assert tree[other_card] == expected_other_card
        assert INVENTED_CARD_PATH not in tree
        assert not any(marker in value for marker in forged_markers for value in tree.values())
    # The admitted commit keeps the caller's raw bytes and nothing settles it:
    # activation settled the evaluated tree, so the parent tree is not authoritative.
    ledger = instance._ledger
    admitted_oid = ledger.parent_of(result.admission.candidate_commit_oid)
    assert admitted_oid is not None
    admitted = ledger.read_tree(admitted_oid)
    assert all(admitted[path] == content for path, content in forgery.items())
    assert ledger.unreachable_commits() == ()


def test_odd_card_paths_are_refused_typed_before_any_commit(tmp_path: Path) -> None:
    """Card-path normalization runs at admission, so no proposal ref is ever created."""

    instance, _owner = initialize_local(tmp_path)
    accepted = _accepted_card_bearing_tree(instance)
    main = instance._ledger.read_main()
    other = _other_document(instance)
    odd_paths = {
        "Cards/x.md": "case-fold",
        "./cards/x.md": "non-canonical",
        "cards/../cards/x.md": "non-canonical",
    }

    for path, refusal in odd_paths.items():
        with pytest.raises(CanonicalEncodingError, match=refusal):
            _submit(
                instance,
                {**accepted, OTHER_DOCUMENT_PATH: other, path: b"x\n"},
                ref="odd",
            )
        assert instance._ledger.read_proposal_ref("refs/proposals/owner/odd") is None
        assert instance._ledger.read_main() == main


def test_settlement_refuses_a_forged_card_handed_straight_to_prepare_generation(
    tmp_path: Path,
) -> None:
    """The second line: settlement re-derives every card and compares the whole tree."""

    instance, _owner = initialize_local(tmp_path)
    _base, proposed = _document_tree(instance)
    result = _submit(instance, proposed, ref="first")
    assert result.candidate is not None
    assert result.evaluation.evaluated_tree_oid is not None
    evaluated = instance.proposal_tree(result.evaluation.evaluated_tree_oid)
    card = candidate_card_path(DOCUMENT_PATH)
    assert card in evaluated

    with pytest.raises(SettlementIntegrityError, match="derivative cards do not reproduce"):
        instance.prepare_generation(
            base=instance.accepted_coordinate(),
            candidate_tree={**evaluated, card: CALLER_MARKER + b"\n"},
            candidate=result.candidate,
            approvals=(),
            actor_binding=ChangeActorBinding(
                actor_id="owner",
                source_compilation_digest=SOURCE_COMPILATION_DIGEST,
            ),
            proposal_actor_id="owner",
            sequence=1,
        )


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
