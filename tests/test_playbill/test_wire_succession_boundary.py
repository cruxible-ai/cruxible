"""A ledger that crosses the wire succession replays end to end.

An instance that existed before the succession keeps every receipt it settled.
Its accepted history is therefore a `playbill-changeset-v1`/`v2` prefix followed
by a `playbill-changeset-v3` suffix, and the generation where the two meet is an
ordinary generation edge: the v3 record's semantic root names a `playbill-sroot-v1`
parent, and every later one names a `playbill-sroot-v2` parent.

The fixture here builds exactly that ledger -- driving the evaluator at the wire
version a pre-succession build would have produced -- and then requires a
genesis-rooted replay, a checkpointed replay seeded at the boundary itself, and
accepted projection to all accept it. Nothing about a v1 or v2 generation may
change because a v3 generation later joined the same history.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.candidates import (
    CandidateRecord,
    CandidateRecordV2,
    CandidateRecordV3,
    CandidateWireVersion,
)
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    render_document,
)
from cruxible_client.contracts.projection_extensions import playbill_replay_extension_registry
from cruxible_client.contracts.subjects import SubjectShell, render_subject
from cruxible_core.playbill.checkpoints import (
    checkpoint_body,
    checkpoint_path,
    write_checkpoint,
)
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection_artifacts import parse_projection_tree
from cruxible_core.playbill.settlement import (
    SEMANTIC_ROOT_V2_DOMAIN,
    ChangeSetRecord,
    ChangeSetRecordV2,
    ChangeSetRecordV3,
    compute_semantic_root_v2,
    record_semantic_root_derivation,
)
from tests.test_playbill._adoption_fixture import _Builder
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_change_set_closure import claim_type, subject

V1 = "playbill-validated-candidate-v1"
V2 = "playbill-validated-candidate-v2"
V3 = "playbill-validated-candidate-v3"

# One v1 generation, two v2 generations, then three v3 generations: the prefix
# carries both superseded receipt versions and the suffix crosses the boundary
# once and then runs on.
_HISTORY: tuple[CandidateWireVersion, ...] = (V1, V2, V2, V3, V3, V3)


def _members(builder: _Builder, index: int, version: CandidateWireVersion) -> dict[str, bytes]:
    """Author a change set the given wire version could actually have carried.

    A v1 receipt only ever carried one Document, Subject or principal member, so
    a v1 generation in this history is a single Document; every later version
    carries the multi-member closure the fixture exercises elsewhere.
    """

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
    if version == V1:
        return members
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
    for index, version in enumerate(_HISTORY, start=1):
        builder.accept(
            _members(builder, index, version),
            phase="boundary",
            wire_version=version,
        )
    return instance, builder


def _reopen(instance: PlaybillInstance) -> PlaybillInstance:
    """Reopen through a full genesis-rooted replay of the crossed ledger."""

    return PlaybillInstance.open(instance.root, trust_root=instance.trust_root)


def _records(instance: PlaybillInstance) -> list[object]:
    return [generation.record for generation in instance.accepted_history()[1:]]


def test_the_fixture_ledger_really_carries_both_sides_of_the_succession(
    crossed_ledger: tuple[PlaybillInstance, _Builder],
) -> None:
    instance, _builder = crossed_ledger
    reopened = _reopen(instance)
    records = _records(reopened)
    assert [type(record) for record in records] == [
        ChangeSetRecord,
        ChangeSetRecordV2,
        ChangeSetRecordV2,
        ChangeSetRecordV3,
        ChangeSetRecordV3,
        ChangeSetRecordV3,
    ]
    # The candidate version travels with the receipt version, never apart from it.
    assert [record.candidate.tag for record in records] == [
        "playbill-candidate-v1",
        "playbill-candidate-v1",
        "playbill-candidate-v1",
        "playbill-candidate-v2",
        "playbill-candidate-v2",
        "playbill-candidate-v2",
    ]


def test_the_boundary_generation_names_a_v1_parent_and_the_next_names_a_v2_parent(
    crossed_ledger: tuple[PlaybillInstance, _Builder],
) -> None:
    instance, _builder = crossed_ledger
    reopened = _reopen(instance)
    history = reopened.accepted_history()
    records = _records(reopened)

    derivations = [record_semantic_root_derivation(record) for record in records]
    assert derivations == [
        "playbill-sroot-v1",
        "playbill-sroot-v1",
        "playbill-sroot-v1",
        SEMANTIC_ROOT_V2_DOMAIN,
        SEMANTIC_ROOT_V2_DOMAIN,
        SEMANTIC_ROOT_V2_DOMAIN,
    ]

    # The first v3 generation is the boundary: its preimage names a v1 parent.
    boundary = history[4]
    parent = history[3]
    record = records[3]
    assert isinstance(record, ChangeSetRecordV3)
    approvals = tuple(sorted(_approval_digests(record)))
    assert boundary.semantic_root == compute_semantic_root_v2(
        manifest_root_value=record.candidate.candidate_manifest_root,
        changeset_digest_value=record.changeset_digest,
        approval_digests=approvals,
        parent_semantic_root=parent.semantic_root.tagged,
        parent_derivation="playbill-sroot-v1",
    )
    # The same 32-byte parent read as a v2-derived parent yields a different
    # child, which is what stops the chain being re-narrated across the boundary.
    assert boundary.semantic_root != compute_semantic_root_v2(
        manifest_root_value=record.candidate.candidate_manifest_root,
        changeset_digest_value=record.changeset_digest,
        approval_digests=approvals,
        parent_semantic_root=parent.semantic_root.tagged,
        parent_derivation=SEMANTIC_ROOT_V2_DOMAIN,
    )

    # The generation after the boundary names a v2 parent.
    following = history[5]
    next_record = records[4]
    assert isinstance(next_record, ChangeSetRecordV3)
    assert following.semantic_root == compute_semantic_root_v2(
        manifest_root_value=next_record.candidate.candidate_manifest_root,
        changeset_digest_value=next_record.changeset_digest,
        approval_digests=tuple(sorted(_approval_digests(next_record))),
        parent_semantic_root=boundary.semantic_root.tagged,
        parent_derivation=SEMANTIC_ROOT_V2_DOMAIN,
    )


def _approval_digests(record: ChangeSetRecordV3) -> list[str]:
    from cruxible_client.contracts.attestations import approval_digest

    return [approval_digest(item.attestation).tagged for item in record.approvals]


def test_a_crossed_ledger_replays_from_genesis_and_from_a_checkpoint_at_the_boundary(
    crossed_ledger: tuple[PlaybillInstance, _Builder],
) -> None:
    instance, builder = crossed_ledger
    directory = instance._checkpoint_directory(instance.root)
    checkpoint_path(directory).unlink(missing_ok=True)

    genesis_rooted = _reopen(instance)
    expected = genesis_rooted.accepted_coordinate()
    assert len(genesis_rooted.accepted_history()) == len(_HISTORY) + 1

    # Seed a checkpoint at the last pre-succession generation, so the suffix the
    # checkpoint hands to replay begins with the boundary generation itself.
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


def test_a_checkpoint_taken_on_the_v3_suffix_verifies_against_its_own_merkle_root(
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
    # The head receipt is v3, so the root its change set accepted is the merkle
    # root, and the flat root the body also carries is not what it is checked on.
    assert body.merkle_root.startswith("merkle-sha256:")
    assert body.manifest_root.startswith("sha256:")
    assert isinstance(genesis_rooted.accepted_history()[-1].record, ChangeSetRecordV3)
    write_checkpoint(directory, body, written_at="2026-08-19T00:00:00.000000Z")

    checkpointed = _reopen(instance)
    assert checkpointed.accepted_coordinate() == expected


def test_a_stale_pre_succession_checkpoint_is_discarded_and_replay_falls_back(
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


def test_replaying_a_crossed_ledger_reproduces_each_generation_in_its_own_version(
    crossed_ledger: tuple[PlaybillInstance, _Builder],
) -> None:
    """The candidate a replay reproduces is the object its own receipt carries.

    A comparison between a reproduced candidate and a recorded one is only worth
    anything if the two are the same kind of object, so replay asks the evaluator
    for the version the receipt names rather than for the version this build
    would produce today.
    """

    instance, _builder = crossed_ledger
    reopened = _reopen(instance)
    from cruxible_core.playbill.recovery import _candidate_from_record

    reproduced = [
        _candidate_from_record(generation.record)
        for generation in reopened.accepted_history()[1:]
        if generation.record is not None
    ]
    assert [type(item) for item in reproduced] == [
        CandidateRecord,
        CandidateRecordV2,
        CandidateRecordV2,
        CandidateRecordV3,
        CandidateRecordV3,
        CandidateRecordV3,
    ]
    assert [item.tag for item in reproduced] == [V1, V2, V2, V3, V3, V3]


def test_accepted_projection_reads_a_crossed_ledger(
    crossed_ledger: tuple[PlaybillInstance, _Builder],
) -> None:
    instance, _builder = crossed_ledger
    reopened = _reopen(instance)
    head = reopened.accepted_coordinate()
    # Accepted projection parses every receipt in the history at once, on both
    # sides of the boundary, and reads their member and law evidence in one
    # stream: a v1 receipt's `artifact_digest` member and a v3 receipt's
    # `candidate_artifact_digest` member land in the same envelope rows.
    parsed = parse_projection_tree(
        dict(reopened.tree_at(head.git_oid)),
        registry=playbill_replay_extension_registry(),
        bodies=reopened.body_store(),
    )
    identities = {row.identity for row in parsed.envelopes}
    assert {f"document:note-{index}" for index in range(1, len(_HISTORY) + 1)} <= identities


def test_the_document_bytes_are_unaffected_by_which_receipt_accepted_them(
    crossed_ledger: tuple[PlaybillInstance, _Builder],
) -> None:
    instance, _builder = crossed_ledger
    reopened = _reopen(instance)
    tree = reopened.tree_at(reopened.accepted_coordinate().git_oid)
    for index in range(1, len(_HISTORY) + 1):
        assert f"documents/note-{index}.json" in tree


def test_the_first_generation_of_a_new_instance_states_the_boundary_too(
    tmp_path: Path,
) -> None:
    """Genesis is never rewritten, so a brand-new ledger crosses at generation one.

    The genesis semantic root is what a `playbill-sroot-v1` chain starts from and
    the succession does not touch it, so the first accepted generation of every
    instance -- one settled today, on an empty ledger -- is a v3 record whose
    preimage names a v1 parent. Every ledger therefore exercises the succession
    boundary, which is the point of not special-casing it.
    """

    instance, owner = initialize_local(tmp_path)
    genesis_root = instance.accepted_coordinate().semantic_root
    builder = _Builder(instance, owner, checkpoint_interval=10_000)
    builder.accept(_members(builder, 1, V3), phase="first")

    reopened = _reopen(instance)
    first = reopened.accepted_history()[1]
    record = first.record
    assert isinstance(record, ChangeSetRecordV3)
    assert record_semantic_root_derivation(None) == "playbill-sroot-v1"
    assert first.semantic_root == compute_semantic_root_v2(
        manifest_root_value=record.candidate.candidate_manifest_root,
        changeset_digest_value=record.changeset_digest,
        approval_digests=tuple(sorted(_approval_digests(record))),
        parent_semantic_root=genesis_root,
        parent_derivation="playbill-sroot-v1",
    )
    # Genesis itself is untouched by the succession.
    assert reopened.accepted_history()[0].record is None
    assert reopened.accepted_history()[0].semantic_root.tagged == genesis_root
