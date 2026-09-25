"""Local replay checkpoints: equal answers, typed refusals, genesis fallback."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from cruxible_client.contracts.errors import ReplayCheckpointError
from cruxible_core.indexes.claims.claim_subject_index import build_claim_subject_index
from cruxible_core.ledger.checkpoints import (
    CHECKPOINT_DIRECTORY,
    ReplayCheckpointBodyV2,
    ReplayCheckpointFileV2,
    checkpoint_body,
    checkpoint_digest,
    checkpoint_path,
    load_checkpoint_file,
    render_checkpoint,
    verify_checkpoint,
    write_checkpoint,
)
from cruxible_core.ledger.recovery import RecoveredInstanceState
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._adoption_fixture import MINIATURE, AdoptionFixtureProfile, build_fixture

PROFILE = AdoptionFixtureProfile(
    name="checkpointed",
    subjects=6,
    claim_types=3,
    documents=2,
    query_definitions=2,
    seed_claims=6,
    generations=6,
    seed="checkpointed",
)


def _checkpoints(instance: PlaybillInstance) -> Path:
    return instance.root / CHECKPOINT_DIRECTORY


def _observable(state: RecoveredInstanceState) -> dict[str, object]:
    """Everything a consumer can read back off a recovery, digests included."""

    return {
        "head": state.head.oid,
        "coordinate": state.coordinate.model_dump(mode="json"),
        "history": [
            {
                "sequence": generation.sequence,
                "oid": generation.oid,
                "semantic_root": generation.semantic_root.tagged,
                "generation_root": generation.generation_root.tagged,
                "descriptor": generation.descriptor.model_dump(mode="json"),
                "principals": generation.principals.model_dump(mode="json"),
                "record": (
                    None if generation.record is None else generation.record.model_dump(mode="json")
                ),
            }
            for generation in state.history
        ],
        "projection": (
            None
            if state.projection is None
            else {
                "logical_digest": state.projection.logical_digest,
                "semantic_root": state.projection.semantic_root,
                "generation_root": state.projection.generation_root,
                "row_counts": state.projection.row_counts,
            }
        ),
    }


def _place_checkpoint_at(fixture, sequence: int) -> ReplayCheckpointBodyV2:  # type: ignore[no-untyped-def]
    """Summarize an already-replayed coordinate, exactly as activation would."""

    instance = _reopen_instance(fixture)
    history = instance.accepted_history()
    generation = next(item for item in history if item.sequence == sequence)
    parent = next(item for item in history if item.sequence == sequence - 1)
    tree = instance._ledger.read_tree(generation.oid)
    body = checkpoint_body(
        instance_id=instance.descriptor.instance_id,
        object_format=instance.descriptor.git_object_format,
        compiler=instance.descriptor.compiler,
        genesis=instance.descriptor.genesis,
        sequence=sequence,
        git_oid=generation.oid,
        semantic_root=generation.semantic_root.tagged,
        generation_root=generation.generation_root.tagged,
        parent_generation_root=parent.generation_root.tagged,
        tree=tree,
    )
    write_checkpoint(_checkpoints(instance), body, written_at="2026-01-01T00:00:00.000000Z")
    return body


def _reopen_instance(fixture) -> PlaybillInstance:  # type: ignore[no-untyped-def]
    return PlaybillInstance.open(
        fixture.managed_root,
        trust_root=fixture.instance.trust_root,
    )


def _reopen(fixture) -> RecoveredInstanceState:  # type: ignore[no-untyped-def]
    return _reopen_instance(fixture)._recovered


STRIDE_PROFILE = replace(PROFILE, name="strided", checkpoint_interval=2)


def test_a_checkpoint_written_on_the_acceptance_stride_seeds_a_reopen(tmp_path: Path) -> None:
    """The stride write and the reopen read must agree, not merely both exist.

    A summary built while settling a generation has the *parent's* principal
    registry close at hand and the new generation's nowhere in sight, so this
    asserts the written summary reproduces against its own coordinate and is
    actually loaded, rather than being silently discarded on every reopen.
    """

    fixture = build_fixture(tmp_path, STRIDE_PROFILE)
    written = load_checkpoint_file(_checkpoints(fixture.instance))
    assert written is not None
    assert written.body.sequence % STRIDE_PROFILE.checkpoint_interval == 0
    assert written.body.sequence < fixture.head_sequence

    seed = verify_checkpoint(
        fixture.instance._ledger,
        written,
        genesis=fixture.instance._verified_genesis,
        instance_id=fixture.instance.descriptor.instance_id,
        object_format=fixture.instance.descriptor.git_object_format,
        compiler=fixture.instance.descriptor.compiler,
        genesis_coordinate=fixture.instance.descriptor.genesis,
    )
    assert len(seed.prefix) == written.body.sequence + 1
    assert seed.prefix[-1].principals == written.body.principals
    assert seed.state.claim_subjects == build_claim_subject_index(seed.tree)


def test_a_stride_checkpoint_reopen_equals_a_genesis_rooted_recovery(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path, STRIDE_PROFILE)
    from_stride = _observable(_reopen(fixture))
    checkpoint_path(_checkpoints(fixture.instance)).unlink()
    assert _observable(_reopen(fixture)) == from_stride


def test_the_fixture_leaves_a_checkpoint_on_its_write_stride(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path, PROFILE)
    # This profile's history never reaches the daemon interval, so the checkpoint
    # present at this point is the one recovery itself wrote at the head.
    assert not checkpoint_path(_checkpoints(fixture.instance)).exists()
    reopened = _reopen_instance(fixture)
    record = load_checkpoint_file(_checkpoints(fixture.instance))
    assert record is not None
    assert record.body.sequence == fixture.head_sequence
    assert record.body.git_oid == reopened.accepted_coordinate().git_oid


def test_reopen_from_a_checkpoint_equals_a_genesis_rooted_recovery(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path, PROFILE)
    genesis_state = _observable(_reopen(fixture))

    body = _place_checkpoint_at(fixture, fixture.head_sequence - 3)
    checkpointed = _reopen(fixture)
    assert _observable(checkpointed) == genesis_state
    assert body.sequence < fixture.head_sequence

    # And the reopen refreshed the checkpoint to the head it just served.
    refreshed = load_checkpoint_file(_checkpoints(fixture.instance))
    assert refreshed is not None
    assert refreshed.body.sequence == fixture.head_sequence


def test_a_deleted_checkpoint_costs_time_and_nothing_else(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path, PROFILE)
    expected = _observable(_reopen(fixture))
    checkpoint_path(_checkpoints(fixture.instance)).unlink()
    assert _observable(_reopen(fixture)) == expected


def test_an_absent_checkpoint_directory_is_legal(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path, PROFILE)
    expected = _observable(_reopen(fixture))
    directory = _checkpoints(fixture.instance)
    assert checkpoint_path(directory).is_file()
    shutil.rmtree(directory)  # the checkpoint and its verification record
    assert _observable(_reopen(fixture)) == expected


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sequence", 1),
        ("semantic_root", "sha256:" + "ab" * 32),
        ("generation_root", "sha256:" + "cd" * 32),
        ("parent_generation_root", "sha256:" + "ef" * 32),
        ("manifest_root", "sha256:" + "12" * 32),
        # The trie the suffix updates in place is rebuilt from the coordinate's
        # own members and its root is required to reproduce, so a forged merkle
        # root is refused exactly as a forged flat one is.
        ("merkle_root", "merkle-sha256:" + "12" * 32),
        ("instance_id", "inst_other"),
    ],
)
def test_a_tampered_checkpoint_field_is_refused(tmp_path: Path, field: str, value: object) -> None:
    fixture = build_fixture(tmp_path, PROFILE)
    body = _place_checkpoint_at(fixture, fixture.head_sequence - 2)
    tampered = body.model_copy(update={field: value})
    write_checkpoint(
        _checkpoints(fixture.instance), tampered, written_at="2026-01-01T00:00:00.000000Z"
    )

    with pytest.raises(ReplayCheckpointError):
        verify_checkpoint(
            fixture.instance._ledger,
            ReplayCheckpointFileV2(
                body=tampered,
                checkpoint_digest=checkpoint_digest(tampered).tagged,
                written_at="2026-01-01T00:00:00.000000Z",
            ),
            genesis=fixture.instance._verified_genesis,
            instance_id=fixture.instance.descriptor.instance_id,
            object_format=fixture.instance.descriptor.git_object_format,
            compiler=fixture.instance.descriptor.compiler,
            genesis_coordinate=fixture.instance.descriptor.genesis,
        )


def test_a_tampered_member_manifest_is_refused(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path, PROFILE)
    body = _place_checkpoint_at(fixture, fixture.head_sequence - 2)
    members = dict(body.members)
    victim = sorted(members)[0]
    members[victim] = "0" * 64
    tampered = body.model_copy(update={"members": members})
    with pytest.raises(ReplayCheckpointError):
        verify_checkpoint(
            fixture.instance._ledger,
            ReplayCheckpointFileV2(
                body=tampered,
                checkpoint_digest=checkpoint_digest(tampered).tagged,
                written_at="2026-01-01T00:00:00.000000Z",
            ),
            genesis=fixture.instance._verified_genesis,
            instance_id=fixture.instance.descriptor.instance_id,
            object_format=fixture.instance.descriptor.git_object_format,
            compiler=fixture.instance.descriptor.compiler,
            genesis_coordinate=fixture.instance.descriptor.genesis,
        )


def test_a_tampered_principal_snapshot_is_refused(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path, PROFILE)
    body = _place_checkpoint_at(fixture, fixture.head_sequence - 2)
    principals = body.principals.model_copy(
        update={
            "principals": tuple(
                principal.model_copy(update={"status": "revoked"})
                if principal.principal_id == "owner"
                else principal
                for principal in body.principals.principals
            )
        }
    )
    tampered = body.model_copy(update={"principals": principals})
    with pytest.raises(ReplayCheckpointError):
        verify_checkpoint(
            fixture.instance._ledger,
            ReplayCheckpointFileV2(
                body=tampered,
                checkpoint_digest=checkpoint_digest(tampered).tagged,
                written_at="2026-01-01T00:00:00.000000Z",
            ),
            genesis=fixture.instance._verified_genesis,
            instance_id=fixture.instance.descriptor.instance_id,
            object_format=fixture.instance.descriptor.git_object_format,
            compiler=fixture.instance.descriptor.compiler,
            genesis_coordinate=fixture.instance.descriptor.genesis,
        )


def test_a_checkpoint_whose_digest_does_not_reproduce_is_refused(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path, PROFILE)
    body = _place_checkpoint_at(fixture, fixture.head_sequence - 2)
    target = checkpoint_path(_checkpoints(fixture.instance))
    payload = json.loads(target.read_bytes())
    payload["checkpoint_digest"] = "sha256:" + "99" * 32
    target.write_bytes(render_checkpoint(body, written_at="2026-01-01T00:00:00.000000Z"))
    forged = json.loads(target.read_bytes())
    forged["checkpoint_digest"] = payload["checkpoint_digest"]
    target.write_bytes(json.dumps(forged, separators=(",", ":"), sort_keys=True).encode() + b"\n")
    with pytest.raises(ReplayCheckpointError):
        load_checkpoint_file(_checkpoints(fixture.instance))


def test_a_corrupt_checkpoint_is_discarded_and_recovery_falls_back(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path, PROFILE)
    expected = _observable(_reopen(fixture))
    target = checkpoint_path(_checkpoints(fixture.instance))
    target.write_bytes(b"{ this is not a checkpoint")
    assert _observable(_reopen(fixture)) == expected
    # A refused checkpoint is deleted, then rewritten from the recovery it forced.
    rewritten = load_checkpoint_file(_checkpoints(fixture.instance))
    assert rewritten is not None
    assert rewritten.body.sequence == fixture.head_sequence


def test_a_checkpoint_off_accepted_main_is_refused(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path, PROFILE)
    body = _place_checkpoint_at(fixture, fixture.head_sequence - 2)
    history = fixture.instance._ledger.main_history()
    tampered = body.model_copy(update={"git_oid": history[-1]})
    with pytest.raises(ReplayCheckpointError):
        verify_checkpoint(
            fixture.instance._ledger,
            ReplayCheckpointFileV2(
                body=tampered,
                checkpoint_digest=checkpoint_digest(tampered).tagged,
                written_at="2026-01-01T00:00:00.000000Z",
            ),
            genesis=fixture.instance._verified_genesis,
            instance_id=fixture.instance.descriptor.instance_id,
            object_format=fixture.instance.descriptor.git_object_format,
            compiler=fixture.instance.descriptor.compiler,
            genesis_coordinate=fixture.instance.descriptor.genesis,
        )


def test_a_checkpoint_whose_generation_signature_fails_is_refused(tmp_path: Path) -> None:
    """A checkpoint may not admit a coordinate whose daemon signature does not verify."""

    fixture = build_fixture(tmp_path, PROFILE)
    body = _place_checkpoint_at(fixture, fixture.head_sequence - 2)
    ledger = fixture.instance._ledger
    original = ledger.verify_commit_with_public_key

    def refuse(oid: str, *, principal_id: str, public_key_hex: str) -> bool:
        return False

    ledger.verify_commit_with_public_key = refuse  # type: ignore[method-assign]
    try:
        with pytest.raises(ReplayCheckpointError):
            verify_checkpoint(
                ledger,
                ReplayCheckpointFileV2(
                    body=body,
                    checkpoint_digest=checkpoint_digest(body).tagged,
                    written_at="2026-01-01T00:00:00.000000Z",
                ),
                genesis=fixture.instance._verified_genesis,
                instance_id=fixture.instance.descriptor.instance_id,
                object_format=fixture.instance.descriptor.git_object_format,
                compiler=fixture.instance.descriptor.compiler,
                genesis_coordinate=fixture.instance.descriptor.genesis,
            )
    finally:
        ledger.verify_commit_with_public_key = original  # type: ignore[method-assign]


def test_the_checkpoint_preimage_carries_no_wall_clock(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path, MINIATURE)
    _reopen(fixture)
    record = load_checkpoint_file(_checkpoints(fixture.instance))
    assert record is not None
    assert "written_at" not in record.body.model_dump(mode="json")
    early = render_checkpoint(record.body, written_at="2020-01-01T00:00:00.000000Z")
    late = render_checkpoint(record.body, written_at="2030-01-01T00:00:00.000000Z")
    assert early != late
    assert json.loads(early)["checkpoint_digest"] == json.loads(late)["checkpoint_digest"]


def test_a_tampered_checkpoint_on_disk_falls_back_to_genesis(tmp_path: Path) -> None:
    """A refusal is not a failure: the reopen still serves the genesis answer."""

    fixture = build_fixture(tmp_path, PROFILE)
    expected = _observable(_reopen(fixture))
    body = _place_checkpoint_at(fixture, fixture.head_sequence - 2)
    tampered = body.model_copy(update={"semantic_root": "sha256:" + "ab" * 32})
    write_checkpoint(
        _checkpoints(fixture.instance),
        tampered,
        written_at="2026-01-01T00:00:00.000000Z",
    )
    assert _observable(_reopen(fixture)) == expected
    rewritten = load_checkpoint_file(_checkpoints(fixture.instance))
    assert rewritten is not None
    assert rewritten.body.semantic_root != tampered.semantic_root


def test_activation_writes_a_checkpoint_on_its_configured_stride(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon's own acceptance path leaves the summary a reopen then loads."""

    from cruxible_core.ledger.activation import ActivationPublisher
    from cruxible_core.proposals.settlement import ChangeActorBinding, prepare_generation
    from cruxible_core.storage.cas import ContentAddressedBodyStore
    from tests.test_ledger.test_activation import _candidate, _instance, _sign

    instance, _owner, reviewer = _instance(tmp_path)
    base, tree, candidate = _candidate(instance)
    approvals = (_sign(reviewer, candidate.candidate_digest, base.semantic_root),)
    bundle = prepare_generation(
        instance._ledger,
        base=base,
        candidate_tree=tree,
        candidate=candidate,
        approval_submissions=approvals,
        bodies=instance.body_store(),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        proposal_actor_id="owner",
        sequence=1,
    )
    projections = Path(instance.inspect().storage_directories["projections"])
    publisher = ActivationPublisher(
        instance._ledger,
        publication_directory=projections,
        bodies=ContentAddressedBodyStore(instance.root / "cas"),
        accepted_coordinates_by_sequence=instance._accepted_coordinates_by_sequence(),
        checkpoint_directory=_checkpoints(instance),
        checkpoint_interval=1,
        genesis=instance.descriptor.genesis,
    )
    from cruxible_core.ledger import checkpoints

    expected_members = checkpoints.members_for_tree(bundle.tree)
    assert bundle.members == expected_members

    def no_rehash(_tree):
        pytest.fail("activation checkpoint must reuse verified semantic members")

    monkeypatch.setattr(checkpoints, "members_for_tree", no_rehash)
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"

    record = load_checkpoint_file(_checkpoints(instance))
    assert record is not None
    assert record.body.members == expected_members
    assert record.body.sequence == 1
    assert record.body.git_oid == bundle.oid
    assert record.body.semantic_root == bundle.semantic_root.tagged

    monkeypatch.undo()
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_coordinate().git_oid == bundle.oid


def test_a_verified_checkpoint_at_the_head_reopens_without_reading_its_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.ledger.git import GitLedger

    fixture = build_fixture(tmp_path, PROFILE)
    expected = _observable(_reopen(fixture))  # leaves a verified checkpoint at the head
    directory = _checkpoints(fixture.instance)
    record = load_checkpoint_file(directory)
    assert record is not None
    assert (directory / (checkpoint_path(directory).name + ".verified")).read_text().strip() == (
        record.checkpoint_digest
    )
    reads: list[str] = []
    original = GitLedger.read_tree

    def counted(self, oid, *args, **kwargs):  # type: ignore[no-untyped-def]
        reads.append(oid)
        return original(self, oid, *args, **kwargs)

    monkeypatch.setattr(GitLedger, "read_tree", counted)
    assert _observable(_reopen(fixture)) == expected
    assert record.body.git_oid not in reads

    # A record naming any other body is not this checkpoint's: full re-derivation.
    (directory / (checkpoint_path(directory).name + ".verified")).write_text(
        "sha256:" + "0" * 64 + "\n"
    )
    assert _observable(_reopen(fixture)) == expected
    assert record.body.git_oid in reads


def test_a_verified_record_written_while_the_process_runs_is_not_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.ledger import checkpoints
    from cruxible_core.ledger.git import GitLedger

    fixture = build_fixture(tmp_path, PROFILE)
    expected = _observable(_reopen(fixture))  # leaves a verified checkpoint at the head
    directory = _checkpoints(fixture.instance)
    record = load_checkpoint_file(directory)
    assert record is not None
    stamp = directory / (checkpoint_path(directory).name + ".verified")
    genuine = stamp.read_text()

    # The process first sees a record for another body ...
    stamp.write_text("sha256:" + "0" * 64 + "\n")
    checkpoints.reset_trusted_checkpoints()
    assert record.checkpoint_digest not in checkpoints._trusted_checkpoints(directory)
    # ... then the record naming this body reappears while it runs.
    stamp.write_text(genuine)
    reads: list[str] = []
    original = GitLedger.read_tree

    def counted(self, oid, *args, **kwargs):  # type: ignore[no-untyped-def]
        reads.append(oid)
        return original(self, oid, *args, **kwargs)

    monkeypatch.setattr(GitLedger, "read_tree", counted)
    assert _observable(_reopen(fixture)) == expected
    assert record.body.git_oid in reads  # the full path re-derived it


def test_a_forged_tree_under_a_verified_head_checkpoint_is_never_served(tmp_path: Path) -> None:
    """The verified reopen skips re-deriving the head tree, so the tree must authenticate itself."""

    import zlib

    from cruxible_client.contracts.errors import PlaybillGitError
    from cruxible_core.ledger import git as ledger_git

    fixture = build_fixture(tmp_path, PROFILE)
    _reopen(fixture)  # leaves a verified checkpoint at the head
    instance = _reopen_instance(fixture)
    ledger = instance._ledger
    head = ledger.read_main()
    instance.coordinate_for_oid(head)  # leaves a verified history index
    root = ledger._commit_tree(head)
    assert root is not None
    # A subtree startup never walks through the checked reader: only a whole-tree
    # read of the head reaches it.
    trees = {
        name: entry[1]
        for name, entry in ledger._tree_entries_of(root).items()
        if entry[0] == b"40000"
    }
    name = next(item for item in ("documents", "claims", "subjects") if item in trees)
    subtree = trees[name]
    entries = ledger._tree_entries_of(subtree)
    victim = next(iter(entries))
    forged_blob = (
        ledger._git(["hash-object", "-w", "--stdin"], input_bytes=b'{"forged": true}\n')
        .decode()
        .strip()
    )
    body = b"".join(
        entry_mode
        + b" "
        + entry_name.encode()
        + b"\x00"
        + bytes.fromhex(forged_blob if entry_name == victim else oid)
        for entry_name, (entry_mode, oid) in entries.items()
    )
    loose = ledger.path / "objects" / subtree[:2] / subtree[2:]
    assert loose.is_file()
    loose.chmod(0o644)
    loose.write_bytes(zlib.compress(b"tree %d\x00" % len(body) + body))
    for cache in (
        ledger_git._PARSED_TREES,
        ledger_git._TREE_LISTINGS,
        ledger_git._TREE_CHANGES,
        ledger_git._COMMIT_ROOTS,
    ):
        cache.clear()

    try:
        reopened = _reopen_instance(fixture)
    except PlaybillGitError as refused:
        assert "do not hash to their ID" in str(refused)
        return
    with pytest.raises(PlaybillGitError, match="do not hash to their ID"):
        reopened.immutable_tree_at(head)[f"{name}/{victim}"]
