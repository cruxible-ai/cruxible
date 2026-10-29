"""Every accepted generation settles in the one receipt, chained from genesis.

Genesis keeps the flat manifest root and `playbill-sroot-v1`; every accepted
generation is a `playbill-changeset-v3` receipt whose semantic root is
`playbill-sroot-v2` over a merkle manifest root. The first generation's preimage
names a `playbill-sroot-v1` parent (genesis) and every later one a
`playbill-sroot-v2` parent. The ledger built here must replay from genesis and
from a checkpoint, and accepted projection must read it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    render_document,
)
from cruxible_client.contracts.subjects import SubjectShell, render_subject
from cruxible_core.compiler.projection_artifacts import parse_projection_tree
from cruxible_core.ledger.checkpoints import (
    checkpoint_body,
    checkpoint_path,
    write_checkpoint,
)
from cruxible_core.proposals.settlement import (
    SEMANTIC_ROOT_V2_DOMAIN,
    ChangeSetRecord,
    compute_semantic_root_v2,
    record_semantic_root_derivation,
)
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


def test_the_first_generation_names_genesis_and_the_next_names_a_receipt_parent(
    crossed_ledger: tuple[PlaybillInstance, _Builder],
) -> None:
    instance, _builder = crossed_ledger
    reopened = _reopen(instance)
    history = reopened.accepted_history()
    records = _records(reopened)
    assert [record_semantic_root_derivation(record) for record in records] == [
        SEMANTIC_ROOT_V2_DOMAIN
    ] * _GENERATIONS

    first, second = history[1], history[2]
    record, next_record = records[0], records[1]
    approvals = tuple(sorted(_approval_digests(record)))
    assert first.semantic_root == compute_semantic_root_v2(
        manifest_root_value=record.candidate.candidate_manifest_root,
        changeset_digest_value=record.changeset_digest,
        approval_digests=approvals,
        parent_semantic_root=history[0].semantic_root.tagged,
        parent_derivation="playbill-sroot-v1",
    )
    # The same parent read as a receipt-derived parent yields a different child,
    # which is what stops the chain being re-narrated.
    assert first.semantic_root != compute_semantic_root_v2(
        manifest_root_value=record.candidate.candidate_manifest_root,
        changeset_digest_value=record.changeset_digest,
        approval_digests=approvals,
        parent_semantic_root=history[0].semantic_root.tagged,
        parent_derivation=SEMANTIC_ROOT_V2_DOMAIN,
    )
    assert second.semantic_root == compute_semantic_root_v2(
        manifest_root_value=next_record.candidate.candidate_manifest_root,
        changeset_digest_value=next_record.changeset_digest,
        approval_digests=tuple(sorted(_approval_digests(next_record))),
        parent_semantic_root=first.semantic_root.tagged,
        parent_derivation=SEMANTIC_ROOT_V2_DOMAIN,
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
    # The root the head change set accepted is the merkle root; the flat member
    # manifest root the body also carries is not what it is checked on.
    assert body.merkle_root.startswith("merkle-sha256:")
    assert body.manifest_root.startswith("sha256:")
    assert isinstance(genesis_rooted.accepted_history()[-1].record, ChangeSetRecord)
    write_checkpoint(directory, body, written_at="2026-08-19T00:00:00.000000Z")

    checkpointed = _reopen(instance)
    assert checkpointed.accepted_coordinate() == expected


def test_a_stale_checkpoint_file_is_discarded_and_replay_falls_back(
    crossed_ledger: tuple[PlaybillInstance, _Builder],
) -> None:
    instance, _builder = crossed_ledger
    directory = instance._checkpoint_directory(instance.root)
    checkpoint_path(directory).unlink(missing_ok=True)
    expected = _reopen(instance).accepted_coordinate()

    stale = directory / "replay-checkpoint-v1.json"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stale.write_bytes(b'{"tag":"playbill-replay-checkpoint-file-v1"}\n')

    reopened = _reopen(instance)
    assert reopened.accepted_coordinate() == expected
    assert not stale.exists()


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


def test_the_first_generation_of_a_new_instance_names_a_genesis_parent(
    tmp_path: Path,
) -> None:
    """The first accepted generation of every instance names a `playbill-sroot-v1` parent."""

    instance, owner = initialize_local(tmp_path)
    genesis_root = instance.accepted_coordinate().semantic_root
    builder = _Builder(instance, owner, checkpoint_interval=10_000)
    builder.accept(_members(builder, 1), phase="first")

    reopened = _reopen(instance)
    first = reopened.accepted_history()[1]
    record = first.record
    assert isinstance(record, ChangeSetRecord)
    assert record_semantic_root_derivation(None) == "playbill-sroot-v1"
    assert first.semantic_root == compute_semantic_root_v2(
        manifest_root_value=record.candidate.candidate_manifest_root,
        changeset_digest_value=record.changeset_digest,
        approval_digests=tuple(sorted(_approval_digests(record))),
        parent_semantic_root=genesis_root,
        parent_derivation="playbill-sroot-v1",
    )
    assert reopened.accepted_history()[0].record is None
    assert reopened.accepted_history()[0].semantic_root.tagged == genesis_root
