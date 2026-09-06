"""Frozen successor proofs and checkpoint boundaries for the warm handoff."""

from dataclasses import replace

import pytest

from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    render_document,
)
from cruxible_client.contracts.errors import SettlementIntegrityError
from cruxible_core.playbill import instance as instance_module
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.proposals import evaluate_proposal_tree
from cruxible_core.playbill.recovery import prepared_generation_for_handoff
from cruxible_core.playbill.settlement import (
    ChangeActorBinding,
    build_change_set_record,
    change_set_path,
    render_change_set,
)
from tests.test_playbill.test_activation import TIMESTAMP, _instance, _sign

VERSIONS = (
    "playbill-validated-candidate-v1",
    "playbill-validated-candidate-v2",
    "playbill-validated-candidate-v3",
)


def _input(instance, reviewer, index=1, version=VERSIONS[-1]):
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    body = instance.store_document_body(f"# Document {index}\n".encode())
    shell = DocumentShell(
        identity=f"document:doc-{index}",
        document_kind="design",
        title=f"Document {index}",
        media_type="text/markdown",
        body_digest=body.digest,
        authority=DocumentAuthority(required_tier="graph_write"),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    result = evaluate_proposal_tree(
        base_tree=tree,
        current_tree=tree,
        proposed_tree={**tree, f"documents/doc-{index}.json": render_document(shell)},
        current=base,
        bodies=instance.body_store(),
        timestamp=TIMESTAMP,
        rebased=False,
        actor_id="owner",
        wire_version=version,
    )
    assert result.candidate is not None, result.diagnostics
    return dict(
        base=base,
        candidate_tree=dict(result.tree),
        candidate=result.candidate,
        approvals=(_sign(reviewer, result.candidate.candidate_digest, base.semantic_root),),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        proposal_actor_id="owner",
    )


def _resign(instance, bundle, tree):
    oid = instance._ledger.create_signed_generation(
        tree,
        parent_oid=bundle.settlement.base_oid,
        sequence=bundle.record.sequence,
        timestamp=TIMESTAMP,
        message="Handoff adversarial fixture",
    )
    return replace(bundle, oid=oid, tree=tree)


@pytest.mark.parametrize("version", VERSIONS)
def test_handoff_keeps_frozen_successor_identical_to_restart(tmp_path, version):
    instance, _owner, reviewer = _instance(tmp_path)
    result = instance.settle_and_activate(**_input(instance, reviewer, version=version))
    assert result.status == "accepted"
    before = instance.accepted_history()
    assert before[-1].record.tag == version.replace("validated-candidate", "changeset")
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_history() == before
    assert reopened.accepted_coordinate() == result.accepted
    assert reopened._recovered.projection.manifest == instance._recovered.projection.manifest


def test_handoff_authenticates_with_predecessor_daemon_key(tmp_path, monkeypatch):
    instance, _owner, reviewer = _instance(tmp_path)
    inputs = _input(instance, reviewer)
    bundle = instance.prepare_generation(**inputs, sequence=1)
    parent = instance.accepted_history()[-1]
    calls = []

    def reject(oid, *, principal_id, public_key_hex):
        calls.append((oid, principal_id, public_key_hex))
        return False

    # General allowed-signers verification is insufficient at this boundary.
    monkeypatch.setattr(instance._ledger, "verify_commit", lambda oid: True)
    monkeypatch.setattr(instance._ledger, "verify_commit_with_public_key", reject)
    with pytest.raises(SettlementIntegrityError, match="daemon signature"):
        prepared_generation_for_handoff(
            instance._ledger, parent=parent, bundle=bundle, candidate=inputs["candidate"]
        )
    assert calls == [(bundle.oid, "daemon", parent.principals.require_active("daemon").public_key)]


@pytest.mark.parametrize("mutation", ["modify", "delete", "add", "rename"])
def test_handoff_refuses_changed_predecessor_receipts(tmp_path, mutation):
    instance, _owner, reviewer = _instance(tmp_path)
    instance.settle_and_activate(**_input(instance, reviewer))
    parent = instance.accepted_history()[-1]
    inputs = _input(instance, reviewer, index=2)
    bundle = instance.prepare_generation(**inputs, sequence=2)
    tree = dict(bundle.tree)
    previous_path = "changesets/cs-00000000000000000001.json"
    if mutation == "modify":
        tree[previous_path] = b"{}\n"
    elif mutation == "delete":
        del tree[previous_path]
    elif mutation == "add":
        tree["changesets/extra.json"] = tree[previous_path]
    else:
        tree["changesets/renamed.json"] = tree.pop(previous_path)
    changed = _resign(instance, bundle, tree)
    with pytest.raises(SettlementIntegrityError, match="append exactly one"):
        prepared_generation_for_handoff(
            instance._ledger, parent=parent, bundle=changed, candidate=inputs["candidate"]
        )
    assert instance.accepted_coordinate().git_oid == parent.oid


def test_handoff_refuses_noncontiguous_sequence(tmp_path):
    instance, _owner, reviewer = _instance(tmp_path)
    inputs = _input(instance, reviewer)
    bundle = instance.prepare_generation(**inputs, sequence=1)
    record = build_change_set_record(
        inputs["candidate"],
        sequence=2,
        approvals=inputs["approvals"],
        actor_binding=inputs["actor_binding"],
    )
    tree = dict(bundle.tree)
    del tree[bundle.record_path]
    path = change_set_path(record)
    tree[path] = render_change_set(record)
    bundle = _resign(instance, replace(bundle, record=record, record_path=path), tree)
    with pytest.raises(SettlementIntegrityError, match="not contiguous"):
        prepared_generation_for_handoff(
            instance._ledger,
            parent=instance.accepted_history()[-1],
            bundle=bundle,
            candidate=inputs["candidate"],
        )


def test_handoff_defers_possible_retired_content_to_full_recovery(tmp_path):
    instance, _owner, reviewer = _instance(tmp_path)
    inputs = _input(instance, reviewer)
    bundle = instance.prepare_generation(**inputs, sequence=1)
    suspect = replace(
        bundle,
        tree={
            **bundle.tree,
            "claims/suspect.json": b'{"statement":{"predicate":"knowledge.brief"}}',
        },
    )
    assert (
        prepared_generation_for_handoff(
            instance._ledger,
            parent=instance.accepted_history()[-1],
            bundle=suspect,
            candidate=inputs["candidate"],
        )
        is None
    )


def test_handoff_checkpoint_stride_and_uncheckpointed_suffix_reopen(tmp_path, monkeypatch):
    instance, _owner, reviewer = _instance(tmp_path)
    monkeypatch.setattr(instance_module, "DEFAULT_CHECKPOINT_INTERVAL", 2)
    checkpoint_dir = instance._checkpoint_directory(instance.root)
    for index in range(1, 4):
        instance.settle_and_activate(**_input(instance, reviewer, index=index))
        if index == 2:
            checkpoint_files = {p: p.read_bytes() for p in checkpoint_dir.iterdir() if p.is_file()}
            assert checkpoint_files
    assert {p: p.read_bytes() for p in checkpoint_dir.iterdir() if p.is_file()} == checkpoint_files
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_history() == instance.accepted_history()
    assert reopened.accepted_coordinate() == instance.accepted_coordinate()
