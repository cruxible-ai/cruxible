"""PB-D restart replay and torn-publication recovery tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.playbill.activation import (
    GENERATION_NOTE,
    MAIN_CAS,
    ORPHAN_CLEANUP,
    SERVING_PUBLICATION,
    WITNESS_PUBLICATION,
)
from cruxible_core.playbill.assembler import PROJECTION_CRASH_POINTS
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.serving import SERVING_MANIFEST_FILE, bind_current_projection
from cruxible_core.playbill.settlement import (
    GENERATION_CONSTRUCTION,
    ChangeActorBinding,
    prepare_generation,
)
from cruxible_core.storage.playbill_projection import detect_projection_orphans

from .test_activation import MemoryWitness, _candidate, _instance, _sign


class SimulatedCrash(RuntimeError):
    pass


def _prepared(tmp_path: Path):
    instance, owner, reviewer = _instance(tmp_path)
    base, tree, candidate = _candidate(instance)
    approvals = tuple(
        sorted(
            (
                _sign(owner, candidate.candidate_digest, base.semantic_root),
                _sign(reviewer, candidate.candidate_digest, base.semantic_root),
            ),
            key=lambda item: item.attestation.signer_id,
        )
    )
    bundle = prepare_generation(
        instance._ledger,
        base=base,
        candidate_tree=tree,
        candidate=candidate,
        approval_submissions=approvals,
        bodies=instance.body_store(),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        sequence=1,
    )
    return instance, base, bundle


@pytest.mark.parametrize(
    ("checkpoint", "phase"),
    [
        (MAIN_CAS, "before"),
        (MAIN_CAS, "after"),
        (GENERATION_NOTE, "before"),
        (GENERATION_NOTE, "after"),
        (SERVING_PUBLICATION, "before"),
        (SERVING_PUBLICATION, "after"),
        (WITNESS_PUBLICATION, "before"),
        (WITNESS_PUBLICATION, "after"),
    ],
)
def test_restart_recovers_every_activation_boundary(
    tmp_path: Path,
    checkpoint: str,
    phase: str,
) -> None:
    instance, base, bundle = _prepared(tmp_path)
    witness = MemoryWitness()
    publisher = instance.activation_publisher(witness=witness)
    projection = publisher.prebuild(bundle, base=base)

    def crash(actual: str) -> None:
        if actual == f"{phase}:{checkpoint}":
            raise SimulatedCrash(actual)

    with pytest.raises(SimulatedCrash):
        publisher.activate(bundle, projection, base=base, crash_hook=crash)

    reopened = PlaybillInstance.open(
        instance.root,
        trust_root=instance.trust_root,
        witness=witness,
    )
    publication = Path(reopened.inspect().storage_directories["projections"])
    if checkpoint == MAIN_CAS and phase == "before":
        assert reopened.accepted_coordinate() == base
        assert not Path(projection.manifest_path).exists()
        assert not instance._ledger.object_exists(bundle.oid)
        assert not (publication / SERVING_MANIFEST_FILE).exists()
        assert witness.records == []
        return

    assert reopened.accepted_coordinate().git_oid == bundle.oid
    assert reopened.accepted_coordinate().semantic_root == bundle.semantic_root.tagged
    assert reopened.accepted_coordinate().generation_root == bundle.generation_root.tagged
    assert reopened._ledger.read_generation_note(bundle.oid) is not None
    assert [record.head_oid for record in witness.records] == [bundle.oid]
    with bind_current_projection(
        publication,
        expected=reopened.accepted_coordinate(),
    ) as handle:
        assert (
            handle.document(
                "document:design",
                access=BodyAccessContext(principal_id="owner", can_read_body=True),
            )
            is not None
        )


def test_clean_reopen_replays_accepted_generation_idempotently(tmp_path: Path) -> None:
    instance, base, bundle = _prepared(tmp_path)
    witness = MemoryWitness()
    publisher = instance.activation_publisher(witness=witness)
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"

    first = PlaybillInstance.open(
        instance.root,
        trust_root=instance.trust_root,
        witness=witness,
    )
    second = PlaybillInstance.open(
        instance.root,
        trust_root=instance.trust_root,
        witness=witness,
    )

    assert first.accepted_coordinate() == second.accepted_coordinate()
    assert first.inspect().head_oid == bundle.oid
    assert [record.head_oid for record in witness.records] == [bundle.oid]


@pytest.mark.parametrize("phase", ["before", "after"])
def test_restart_collects_crash_residue_from_generation_construction(
    tmp_path: Path,
    phase: str,
) -> None:
    instance, owner, reviewer = _instance(tmp_path)
    base, tree, candidate = _candidate(instance)
    approvals = tuple(
        sorted(
            (
                _sign(owner, candidate.candidate_digest, base.semantic_root),
                _sign(reviewer, candidate.candidate_digest, base.semantic_root),
            ),
            key=lambda item: item.attestation.signer_id,
        )
    )

    def crash(actual: str) -> None:
        if actual == f"{phase}:{GENERATION_CONSTRUCTION}":
            raise SimulatedCrash(actual)

    with pytest.raises(SimulatedCrash):
        prepare_generation(
            instance._ledger,
            base=base,
            candidate_tree=tree,
            candidate=candidate,
            approval_submissions=approvals,
            bodies=instance.body_store(),
            actor_binding=ChangeActorBinding(actor_id="owner"),
            sequence=1,
            crash_hook=crash,
        )
    before = instance._ledger.unreachable_commits()
    assert bool(before) is (phase == "after")

    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)

    assert reopened.accepted_coordinate() == base
    assert reopened._ledger.unreachable_commits() == ()


@pytest.mark.parametrize("point", PROJECTION_CRASH_POINTS)
@pytest.mark.parametrize("phase", ["before", "after"])
def test_restart_cleans_every_candidate_prebuild_boundary(
    tmp_path: Path,
    point: str,
    phase: str,
) -> None:
    instance, base, bundle = _prepared(tmp_path)
    publisher = instance.activation_publisher()

    def crash(actual: str) -> None:
        if actual == f"{phase}:{point}":
            raise SimulatedCrash(actual)

    with pytest.raises(SimulatedCrash):
        publisher.prebuild(bundle, base=base, crash_hook=crash)

    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    publication = Path(reopened.inspect().storage_directories["projections"])
    assert reopened.accepted_coordinate() == base
    assert not reopened._ledger.object_exists(bundle.oid)
    assert detect_projection_orphans(publication) == ()
    assert not (publication / SERVING_MANIFEST_FILE).exists()


@pytest.mark.parametrize("phase", ["before", "after"])
def test_restart_finishes_losing_cas_orphan_cleanup(tmp_path: Path, phase: str) -> None:
    instance, owner, reviewer = _instance(tmp_path)
    base, winner_tree, winner_candidate = _candidate(instance, title="Winner")
    _same_base, loser_tree, loser_candidate = _candidate(instance, title="Loser")

    def bundle(tree, candidate):
        approvals = tuple(
            sorted(
                (
                    _sign(owner, candidate.candidate_digest, base.semantic_root),
                    _sign(reviewer, candidate.candidate_digest, base.semantic_root),
                ),
                key=lambda item: item.attestation.signer_id,
            )
        )
        return prepare_generation(
            instance._ledger,
            base=base,
            candidate_tree=tree,
            candidate=candidate,
            approval_submissions=approvals,
            bodies=instance.body_store(),
            actor_binding=ChangeActorBinding(actor_id="owner"),
            sequence=1,
        )

    winner = bundle(winner_tree, winner_candidate)
    loser = bundle(loser_tree, loser_candidate)
    publisher = instance.activation_publisher()
    winner_projection = publisher.prebuild(winner, base=base)
    loser_projection = publisher.prebuild(loser, base=base)
    assert publisher.activate(winner, winner_projection, base=base).status == "accepted"

    def crash(actual: str) -> None:
        if actual == f"{phase}:{ORPHAN_CLEANUP}":
            raise SimulatedCrash(actual)

    with pytest.raises(SimulatedCrash):
        publisher.activate(loser, loser_projection, base=base, crash_hook=crash)

    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_coordinate().git_oid == winner.oid
    assert not reopened._ledger.object_exists(loser.oid)
    assert not Path(loser_projection.manifest_path).exists()
