"""PB-D restart replay and torn-publication recovery tests."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    document_digest,
    document_path,
    parse_document,
)
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
from cruxible_core.playbill.recovery import RecoveredGeneration
from cruxible_core.playbill.serving import SERVING_MANIFEST_FILE, bind_current_projection
from cruxible_core.playbill.settlement import (
    GENERATION_CONSTRUCTION,
    ChangeActorBinding,
    prepare_generation,
)
from cruxible_core.service.playbill_documents import (
    service_activate_playbill_proposal,
    service_playbill_document_history,
    service_propose_playbill_document,
    service_store_playbill_body,
    service_submit_playbill_approval,
)
from cruxible_core.storage.playbill_projection import detect_projection_orphans

from .test_activation import TIMESTAMP, MemoryWitness, _candidate, _instance, _sign


class SimulatedCrash(RuntimeError):
    pass


def _prepared(tmp_path: Path):
    instance, _owner, _reviewer = _instance(tmp_path)
    base, tree, candidate = _candidate(instance)
    bundle = prepare_generation(
        instance._ledger,
        base=base,
        candidate_tree=tree,
        candidate=candidate,
        approval_submissions=(),
        bodies=instance.body_store(),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        proposal_actor_id="owner",
        sequence=1,
    )
    assert bundle.record.approval_requirements == ()
    assert bundle.record.approvals == ()
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
    approvals = (_sign(reviewer, candidate.candidate_digest, base.semantic_root),)

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
            proposal_actor_id="owner",
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
        approvals = (_sign(reviewer, candidate.candidate_digest, base.semantic_root),)
        return prepare_generation(
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


RETENTION_MARKER = "pb-f6-retained-artifact-bytes"


def _accept_document_revision(instance, owner, reviewer, *, revision: int, predecessor: str | None):
    """Accept one further Document revision, advancing main by exactly one generation."""

    body = service_store_playbill_body(
        instance,
        content=f"# {RETENTION_MARKER} r{revision}\n".encode(),
    )
    shell = DocumentShell(
        identity="document:design",
        document_kind="design",
        title=f"{RETENTION_MARKER} r{revision}",
        media_type="text/markdown",
        body_digest=body.digest,
        authority=DocumentAuthority(
            required_tier="graph_write",
            approval_roles=("owner", "reviewer"),
        ),
        governance_scope=("project:playbill",),
        predecessor_digest=predecessor,
        lifecycle=DocumentLifecycle(revision=revision),
    )
    proposal = service_propose_playbill_document(
        instance,
        shell=shell,
        actor_id="owner",
        proposal_name=f"retention-r{revision}",
        timestamp=TIMESTAMP,
    ).proposal
    candidate = proposal.candidate
    assert candidate is not None
    signed = _sign(
        reviewer,
        candidate.candidate_digest,
        candidate.candidate.parent_semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=proposal.admission.proposal_id,
        attestation=signed.attestation,
        authenticated_submitter="approval-relay",
    )
    activated = service_activate_playbill_proposal(
        instance,
        proposal_id=proposal.admission.proposal_id,
        activated_by="owner",
    )
    assert activated.status == "accepted"
    return shell


def test_replay_retains_no_generation_trees_and_serves_history_from_the_ledger(
    tmp_path: Path,
) -> None:
    """Recovery memory must not scale with history: the ledger holds the bytes."""

    instance, owner, reviewer = _instance(tmp_path)
    predecessor: str | None = None
    accepted: list[DocumentShell] = []
    for revision in (1, 2, 3):
        shell = _accept_document_revision(
            instance,
            owner,
            reviewer,
            revision=revision,
            predecessor=predecessor,
        )
        predecessor = document_digest(shell).tagged
        accepted.append(shell)

    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    history = reopened.accepted_history()
    assert [generation.sequence for generation in history] == [0, 1, 2, 3]

    # A recovered generation is a coordinate plus its receipt -- never a tree.
    assert "tree" not in {field.name for field in fields(RecoveredGeneration)}
    assert not any(
        isinstance(getattr(generation, field.name), (bytes, bytearray))
        or (
            isinstance(getattr(generation, field.name), dict)
            and any(
                isinstance(value, (bytes, bytearray))
                for value in getattr(generation, field.name).values()
            )
        )
        for generation in history
        for field in fields(RecoveredGeneration)
    )
    # The marker only ever exists inside accepted artifact bytes, so its absence
    # from everything reachable off the history proves no payload survived replay.
    # Asserting it is present in the tree first keeps the negative meaningful.
    assert RETENTION_MARKER in repr(reopened._ledger.read_tree(history[-1].oid))
    assert RETENTION_MARKER not in repr(history)

    # Each generation's bytes remain readable on demand, straight out of Git.
    path = document_path("design")
    expected = [
        parse_document(reopened._ledger.read_tree(generation.oid)[path], path=path)
        for generation in history[1:]
    ]
    assert expected == accepted

    served = service_playbill_document_history(reopened, identity="document:design")
    assert [entry.sequence for entry in served.entries] == [1, 2, 3]
    assert [entry.revision for entry in served.entries] == [1, 2, 3]
    assert [entry.envelope_digest for entry in served.entries] == [
        document_digest(shell).tagged for shell in accepted
    ]
    assert [entry.body_digest for entry in served.entries] == [
        shell.body_digest for shell in accepted
    ]
    assert [entry.coordinate.git_oid for entry in served.entries] == [
        generation.oid for generation in history[1:]
    ]
