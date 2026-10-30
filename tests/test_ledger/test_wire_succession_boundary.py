"""Every accepted generation settles in the one receipt, chained from genesis.

Every generation, genesis included, is named by the one semantic-root
derivation, `playbill-sroot-v2`, over a Merkle manifest root. Genesis's parent
is the bootstrap anchor, entering typed as `bootstrap`; every accepted
generation after it is a `playbill-changeset-v3` receipt whose parent is the
preceding semantic root. The ledger built here must replay from genesis and
from a checkpoint, and accepted projection must read it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.canonical import BootstrapRoot, semantic_projection
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    render_document,
)
from cruxible_client.contracts.merkle import merkle_manifest_root
from cruxible_client.contracts.subjects import SubjectShell, render_subject
from cruxible_core.compiler.projection_artifacts import parse_projection_tree
from cruxible_core.ledger.bootstrap import bootstrap_changeset_digest
from cruxible_core.ledger.checkpoints import (
    checkpoint_body,
    checkpoint_path,
    load_checkpoint_file,
    write_checkpoint,
)
from cruxible_core.ledger.semantic_root import compute_semantic_root
from cruxible_core.proposals.settlement import ChangeSetRecord
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._adoption_fixture import _Builder
from tests.core_support._support import initialize_local
from tests.test_proposals.test_change_set_closure import claim_type, subject

_GENERATIONS = 4


def _members(builder: _Builder, index: int) -> dict[str, bytes]:
    """Author one multi-member change set: a Document, a Subject and a ClaimType."""

    body = builder.instance.store_document_body(f"# Note {index}\n".encode())
    shell = DocumentShell(
        identity=f"document:note-{index}",
        document_kind="design",
        title=f"Note {index}",
        media_type="text/markdown",
        body_digest=body.digest,
        authority=DocumentAuthority(
            required_tier="graph_write",
        ),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    members = {f"documents/note-{index}.json": render_document(shell)}
    shell_subject: SubjectShell = subject()
    return {
        **members,
        f"subjects/project.work_item/wi-{index}.json": render_subject(
            shell_subject.model_copy(
                update={
                    "subject_id": f"wi-{index}",
                    "identity": shell_subject.identity.model_copy(
                        update={"name": f"project.work_item/wi-{index}"}
                    ),
                }
            )
        ),
        f"claim-types/project.work_item/status{index}.json": render_claim_type_for(index),
    }


def render_claim_type_for(index: int) -> bytes:
    from cruxible_client.contracts.claim_types import render_claim_type

    return render_claim_type(claim_type(f"project.work_item.status{index}"))


@pytest.fixture(scope="module")
def crossed_ledger(tmp_path_factory: pytest.TempPathFactory) -> tuple[PlaybillInstance, _Builder]:
    root = tmp_path_factory.mktemp("succession-boundary")
    instance, owner = initialize_local(root)
    builder = _Builder(instance, owner, checkpoint_interval=10_000)
    for index in range(1, _GENERATIONS + 1):
        builder.accept(_members(builder, index), phase="boundary")
    return instance, builder


def _reopen(instance: PlaybillInstance) -> PlaybillInstance:
    """Reopen through a full genesis-rooted replay of the ledger."""

    return PlaybillInstance.open(instance.root, trust_root=instance.trust_root)


def _records(instance: PlaybillInstance) -> list[object]:
    return [generation.record for generation in instance.accepted_history()[1:]]


def test_every_accepted_generation_settles_in_the_one_receipt(
    crossed_ledger: tuple[PlaybillInstance, _Builder],
) -> None:
    instance, _builder = crossed_ledger
    records = _records(_reopen(instance))
    assert [type(record) for record in records] == [ChangeSetRecord] * _GENERATIONS
    assert {record.tag for record in records} == {"playbill-changeset-v3"}
    assert {record.candidate.tag for record in records} == {"playbill-candidate-v2"}


def test_genesis_and_every_receipt_share_the_one_derivation(
    crossed_ledger: tuple[PlaybillInstance, _Builder],
) -> None:
    instance, _builder = crossed_ledger
    reopened = _reopen(instance)
    history = reopened.accepted_history()
    records = _records(reopened)
    genesis = history[0]
    bootstrap = BootstrapRoot.from_tagged(reopened.descriptor.genesis.bootstrap_root)
    assert isinstance(bootstrap, BootstrapRoot)

    # Generation zero: its Merkle manifest, the bootstrap change set, no
    # approvals, and the bootstrap anchor as a typed parent.
    genesis_tree = reopened.tree_at(genesis.oid)
    assert genesis.semantic_root == compute_semantic_root(
        manifest_root=merkle_manifest_root(semantic_projection(genesis_tree)).tagged,
        changeset_digest=bootstrap_changeset_digest(bootstrap).tagged,
        approval_digests=(),
        parent=bootstrap,
    )
    assert genesis.descriptor.parent_generation_root == bootstrap.value

    # Every later generation: its receipt's inputs, parented by the preceding root.
    for parent, generation, record in zip(history[:-1], history[1:], records, strict=True):
        assert isinstance(record, ChangeSetRecord)
        assert generation.semantic_root == compute_semantic_root(
            manifest_root=record.candidate.candidate_manifest_root,
            changeset_digest=record.changeset_digest,
            approval_digests=tuple(sorted(_approval_digests(record))),
            parent=parent.semantic_root,
        )
        assert record.candidate.parent_semantic_root == parent.semantic_root.tagged

    # The genesis root read as a bootstrap-kind parent yields a different child,
    # which is what stops the chain being re-narrated.
    first, record = history[1], records[0]
    assert isinstance(record, ChangeSetRecord)
    assert first.semantic_root != compute_semantic_root(
        manifest_root=record.candidate.candidate_manifest_root,
        changeset_digest=record.changeset_digest,
        approval_digests=tuple(sorted(_approval_digests(record))),
        parent=BootstrapRoot(genesis.semantic_root.value),
    )


def _approval_digests(record: ChangeSetRecord) -> list[str]:
    from cruxible_client.contracts.attestations import approval_digest

    return [approval_digest(item.attestation).tagged for item in record.approvals]


def test_the_ledger_replays_from_genesis_and_from_a_mid_history_checkpoint(
    crossed_ledger: tuple[PlaybillInstance, _Builder],
) -> None:
    instance, builder = crossed_ledger
    directory = instance._checkpoint_directory(instance.root)
    checkpoint_path(directory).unlink(missing_ok=True)

    genesis_rooted = _reopen(instance)
    expected = genesis_rooted.accepted_coordinate()
    assert len(genesis_rooted.accepted_history()) == _GENERATIONS + 1

    # Seed a checkpoint mid-history, so replay re-derives the suffix from it.
    boundary_parent = genesis_rooted.accepted_history()[3]
    write_checkpoint(
        directory,
        checkpoint_body(
            instance_id=expected.instance_id,
            object_format=expected.git_object_format,
            compiler=expected.compiler,
            genesis=genesis_rooted.descriptor.genesis,
            sequence=boundary_parent.sequence,
            git_oid=boundary_parent.oid,
            semantic_root=boundary_parent.semantic_root.tagged,
            generation_root=boundary_parent.generation_root.tagged,
            parent_generation_root=genesis_rooted.accepted_history()[2].generation_root.tagged,
            tree=genesis_rooted.tree_at(boundary_parent.oid),
        ),
        written_at="2026-08-19T00:00:00.000000Z",
    )

    checkpointed = _reopen(instance)
    assert checkpointed.accepted_coordinate() == expected
    assert [item.semantic_root for item in checkpointed.accepted_history()] == [
        item.semantic_root for item in genesis_rooted.accepted_history()
    ]
    assert [item.generation_root for item in checkpointed.accepted_history()] == [
        item.generation_root for item in genesis_rooted.accepted_history()
    ]


def test_a_head_checkpoint_verifies_against_its_own_merkle_root(
    crossed_ledger: tuple[PlaybillInstance, _Builder],
) -> None:
    instance, _builder = crossed_ledger
    directory = instance._checkpoint_directory(instance.root)
    checkpoint_path(directory).unlink(missing_ok=True)

    genesis_rooted = _reopen(instance)
    expected = genesis_rooted.accepted_coordinate()
    head = genesis_rooted.accepted_history()[-1]
    body = checkpoint_body(
        instance_id=expected.instance_id,
        object_format=expected.git_object_format,
        compiler=expected.compiler,
        genesis=genesis_rooted.descriptor.genesis,
        sequence=head.sequence,
        git_oid=head.oid,
        semantic_root=head.semantic_root.tagged,
        generation_root=head.generation_root.tagged,
        parent_generation_root=genesis_rooted.accepted_history()[-2].generation_root.tagged,
        tree=genesis_rooted.tree_at(head.oid),
    )
    # The one manifest root the body carries is the Merkle root the head change
    # set accepted; there is no flat root beside it.
    head_record = genesis_rooted.accepted_history()[-1].record
    assert isinstance(head_record, ChangeSetRecord)
    assert body.merkle_root == head_record.candidate.candidate_manifest_root
    assert "manifest_root" not in body.model_dump(mode="json")
    write_checkpoint(directory, body, written_at="2026-08-19T00:00:00.000000Z")

    checkpointed = _reopen(instance)
    assert checkpointed.accepted_coordinate() == expected
    assert [item.semantic_root for item in checkpointed.accepted_history()] == [
        item.semantic_root for item in genesis_rooted.accepted_history()
    ]
    assert [item.generation_root for item in checkpointed.accepted_history()] == [
        item.generation_root for item in genesis_rooted.accepted_history()
    ]


def test_a_stale_checkpoint_file_is_discarded_and_replay_falls_back(
    crossed_ledger: tuple[PlaybillInstance, _Builder],
) -> None:
    instance, _builder = crossed_ledger
    directory = instance._checkpoint_directory(instance.root)
    checkpoint_path(directory).unlink(missing_ok=True)
    expected = _reopen(instance).accepted_coordinate()

    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stale = []
    for version in (1, 2):
        path = directory / f"replay-checkpoint-v{version}.json"
        path.write_bytes(b'{"tag":"playbill-replay-checkpoint-file-v%d"}\n' % version)
        stale.append(path)

    reopened = _reopen(instance)
    assert reopened.accepted_coordinate() == expected
    assert not any(path.exists() for path in stale)


def test_a_checkpoint_carrying_a_flat_manifest_root_is_refused_and_replaced(
    crossed_ledger: tuple[PlaybillInstance, _Builder],
) -> None:
    """A body in the retired two-root shape is malformed, never half-read."""

    import json

    instance, _builder = crossed_ledger
    directory = instance._checkpoint_directory(instance.root)
    checkpoint_path(directory).unlink(missing_ok=True)
    expected = _reopen(instance).accepted_coordinate()
    current = load_checkpoint_file(directory)
    assert current is not None

    payload = json.loads(checkpoint_path(directory).read_bytes())
    payload["body"]["manifest_root"] = "sha256:" + "12" * 32
    checkpoint_path(directory).write_bytes(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode() + b"\n"
    )

    reopened = _reopen(instance)
    assert reopened.accepted_coordinate() == expected
    rewritten = load_checkpoint_file(directory)
    assert rewritten is not None
    assert rewritten.body == current.body


def test_accepted_projection_reads_the_ledger(
    crossed_ledger: tuple[PlaybillInstance, _Builder],
) -> None:
    instance, _builder = crossed_ledger
    reopened = _reopen(instance)
    head = reopened.accepted_coordinate()
    # Accepted projection parses every receipt in the history at once and reads
    # their member and law evidence in one stream.
    parsed = parse_projection_tree(
        dict(reopened.tree_at(head.git_oid)),
        bodies=reopened.body_store(),
    )
    identities = {row.identity for row in parsed.envelopes}
    assert {f"document:note-{index}" for index in range(1, _GENERATIONS + 1)} <= identities


def test_a_new_instance_reproduces_its_coordinates_through_its_first_two_generations(
    tmp_path: Path,
) -> None:
    """Fresh reopen, then generations one and two: live, cold and checkpointed agree."""

    instance, owner = initialize_local(tmp_path)
    genesis = instance.accepted_coordinate()
    fresh = _reopen(instance)
    assert fresh.accepted_coordinate() == genesis
    assert fresh.descriptor.genesis == instance.descriptor.genesis
    assert fresh.descriptor.genesis.semantic_root == genesis.semantic_root

    builder = _Builder(instance, owner, checkpoint_interval=10_000)
    live = []
    for index in (1, 2):
        builder.accept(_members(builder, index), phase=f"generation-{index}")
        # The coordinate settlement computed as it accepted the generation.
        live.append(builder.base)

    directory = instance._checkpoint_directory(instance.root)
    checkpoint_path(directory).unlink(missing_ok=True)
    cold = _reopen(instance)
    history = cold.accepted_history()
    assert [item.sequence for item in history] == [0, 1, 2]
    assert history[0].record is None
    assert history[0].semantic_root.tagged == genesis.semantic_root
    assert [
        (item.oid, item.semantic_root.tagged, item.generation_root.tagged) for item in history[1:]
    ] == [(item.git_oid, item.semantic_root, item.generation_root) for item in live]
    assert history[1].record is not None
    assert history[1].record.candidate.parent_semantic_root == genesis.semantic_root
    assert history[1].semantic_root == compute_semantic_root(
        manifest_root=history[1].record.candidate.candidate_manifest_root,
        changeset_digest=history[1].record.changeset_digest,
        approval_digests=tuple(sorted(_approval_digests(history[1].record))),
        parent=history[0].semantic_root,
    )

    # A checkpoint at generation one replays the suffix; the reopen leaves one at
    # the head, from which the next reopen replays nothing.
    write_checkpoint(
        directory,
        checkpoint_body(
            instance_id=genesis.instance_id,
            object_format=genesis.git_object_format,
            compiler=genesis.compiler,
            genesis=cold.descriptor.genesis,
            sequence=1,
            git_oid=history[1].oid,
            semantic_root=history[1].semantic_root.tagged,
            generation_root=history[1].generation_root.tagged,
            parent_generation_root=history[0].generation_root.tagged,
            tree=cold.tree_at(history[1].oid),
        ),
        written_at="2026-10-30T00:00:00.000000Z",
    )
    for _attempt in ("suffix", "head"):
        reopened = _reopen(instance)
        assert reopened.accepted_coordinate() == cold.accepted_coordinate()
        assert [
            (item.semantic_root, item.generation_root, item.descriptor)
            for item in reopened.accepted_history()
        ] == [(item.semantic_root, item.generation_root, item.descriptor) for item in history]
    head_checkpoint = load_checkpoint_file(directory)
    assert head_checkpoint is not None
    assert head_checkpoint.body.sequence == 2
