"""Every newly delta-built member kind's successor equals cold reconstruction (perf B10).

Each test accepts one kind through the ordinary activation path while every
delta-built successor projection is rebuilt cold beside it and compared row
for row, by logical digest and by row counts.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_path
from cruxible_core.compiler import projection_delta as delta_module
from cruxible_core.compiler.assembler import ProjectionAssembler
from tests.core_support._support import initialize_local
from tests.test_integration.test_graph_v4_provider_closure import contracted_demo  # noqa: F401

TIMESTAMP = "2026-08-28T15:01:00.000000Z"


def _world(tmp_path: Path) -> Path:
    world = tmp_path / "world"
    world.mkdir()
    return world


def _rows(path: Path) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {name: sorted(db.execute(f"SELECT * FROM {name}").fetchall()) for name in names}


@pytest.fixture
def delta_kinds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """The member kinds of every delta-built successor, each checked against a cold build."""

    kinds: set[str] = set()
    taken: list[frozenset[str]] = []
    populate = delta_module.populate_successor
    assemble = ProjectionAssembler.assemble

    def counted(assembler, *, request, destination, delta, timings):  # type: ignore[no-untyped-def]
        result = populate(
            assembler, request=request, destination=destination, delta=delta, timings=timings
        )
        if result is not None:
            taken.append(frozenset(member.artifact_kind for member in delta.bundle.record.members))
        return result

    def checked(assembler, request, *, crash_hook=None, delta=None):  # type: ignore[no-untyped-def]
        before = len(taken)
        result = assemble(assembler, request, crash_hook=crash_hook, delta=delta)
        if delta is None or len(taken) == before:
            return result
        directory = tmp_path / f"cold-{len(taken)}-{len(kinds)}"
        directory.mkdir()
        for path in (
            *assembler.publication_directory.glob("*.sqlite"),
            *assembler.publication_directory.glob("projection-*.json"),
        ):
            if path.name != Path(result.manifest_path).name and path.name not in {
                piece.name for piece in result.manifest.pieces
            }:
                (directory / path.name).write_bytes(path.read_bytes())
        cold = ProjectionAssembler(
            assembler._repository,
            accepted=assembler.accepted,
            publication_directory=directory,
            bodies=assembler.bodies,
            accepted_coordinates_by_sequence=assembler.accepted_coordinates_by_sequence,
        )
        rebuilt = assemble(cold, cold.request(output_staging_directory=directory / ".stage-cold"))
        assert result.logical_digest == rebuilt.logical_digest
        assert result.row_counts == rebuilt.row_counts
        assert _rows(Path(result.manifest_path).parent / result.manifest.pieces[0].name) == _rows(
            directory / rebuilt.manifest.pieces[0].name
        )
        kinds.update(taken[-1])
        return result

    monkeypatch.setattr(delta_module, "populate_successor", counted)
    monkeypatch.setattr(ProjectionAssembler, "assemble", checked)
    return kinds


def test_a_subject_kind_successor_equals_cold(tmp_path: Path, delta_kinds: set[str]) -> None:
    from tests.test_indexes.test_resolution_contracts import _accept_tree

    instance, owner = initialize_local(_world(tmp_path))
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[subject_path("delta.kind", "one")] = render_subject(
        SubjectShell(
            identity=ArtifactIdentity(kind="Subject", name="delta.kind/one"),
            subject_kind="delta.kind",
            subject_id="one",
        )
    )
    _accept_tree(instance, owner, tree, timestamp=TIMESTAMP, proposal_name="subject-kind")
    assert "subject-kind" in delta_kinds


@pytest.mark.usefixtures("contracted_demo")
def test_a_blueprint_successor_equals_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delta_kinds: set[str]
) -> None:
    from tests.test_procedures.test_blueprints import accept_blueprint_world

    accept_blueprint_world(_world(tmp_path), monkeypatch)
    assert "blueprint" in delta_kinds


def test_a_resolution_contract_successor_equals_cold(tmp_path: Path, delta_kinds: set[str]) -> None:
    from tests.test_procedures.test_independent_resolution_contracts import (
        accept_contract,
        contract_world,
    )

    instance, owner, contract = contract_world(_world(tmp_path))
    accept_contract(instance, owner, contract)
    assert "resolution-contract" in delta_kinds


def test_an_attestation_successor_equals_cold(tmp_path: Path, delta_kinds: set[str]) -> None:
    from cruxible_client.contracts.accepted_attestations import (
        attestation_path,
        render_accepted_attestation,
    )
    from cruxible_client.contracts.claim_attestations import claim_attestation_v2_envelope_digest
    from tests.test_claims.test_claim_attestation_service import RECORDED_AT, _request
    from tests.test_claims.test_claim_type_migrations import _accepted_claim_world
    from tests.test_indexes.test_resolution_contracts import _accept_tree

    world = _world(tmp_path)
    instance, claim_id, owner = _accepted_claim_world(world)
    record = _request(
        instance, owner, claim_id, world, stance="support", attested_at=RECORDED_AT
    ).attestation
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[attestation_path(claim_attestation_v2_envelope_digest(record))] = (
        render_accepted_attestation(record)
    )
    _accept_tree(instance, owner, tree, timestamp=TIMESTAMP, proposal_name="attestation")
    assert "attestation" in delta_kinds


def test_an_exhaust_promotion_successor_equals_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delta_kinds: set[str]
) -> None:
    from tests.test_exhaust import test_exhaust_promotions as promotions

    promotions.test_promotion_passes_proposal_replay_and_projects_canonical_output(
        _world(tmp_path), monkeypatch
    )
    assert "exhaust-promotion" in delta_kinds


def test_a_trigger_successor_equals_cold(tmp_path: Path, delta_kinds: set[str]) -> None:
    from tests.test_procedures.test_procedure_settlement_delivery import settle_world

    settle_world(_world(tmp_path), capture_triggered=True)
    assert "trigger" in delta_kinds


def test_revisions_timestamps_and_coordinates_read_one_path_index() -> None:
    """The per-path member index answers what scanning every record's members did."""

    from types import SimpleNamespace

    from cruxible_core.compiler import projection_artifacts as artifacts

    def member(path: str, digest: str | None, input_digest: str | None = None):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            path=path, candidate_artifact_digest=digest, artifact_digest=input_digest
        )

    records = tuple(
        (
            f"changesets/cs-{sequence:020d}.json",
            SimpleNamespace(
                sequence=sequence,
                members=members,
                candidate=SimpleNamespace(timestamp=f"2026-08-{sequence:02d}T00:00:00.000000Z"),
                law_evidence=(SimpleNamespace(path="a", result={"sequence": sequence}),),
            ),
        )
        for sequence, members in (
            (1, (member("a", "d1"), member("b", None, "i1"))),
            (2, (member("a", "d2"),)),
            (3, (member("b", "d3"), member("a", "d2"))),
            (4, (member("c", "d4"),)),
        )
    )

    def scanned_revision(path, input_digest, artifact_digest):  # type: ignore[no-untyped-def]
        history = tuple(
            item for _path, record in records for item in record.members if item.path == path
        )
        if any(
            (
                item.candidate_artifact_digest == artifact_digest
                if item.candidate_artifact_digest is not None
                else item.artifact_digest == input_digest
            )
            for item in history
        ):
            return len(history)
        return len(history) + 1

    coordinates = {sequence: f"coordinate-{sequence}" for sequence in range(1, 5)}
    for path, input_digest, digest in (
        ("a", "x", "d2"),
        ("a", "x", "d1"),
        ("a", "x", "dz"),
        ("b", "i1", "d0"),
        ("b", "x", "d3"),
        ("c", "x", "d4"),
        ("z", "x", "d9"),
    ):
        assert artifacts.projected_revision(
            records, path=path, artifact_digest=digest
        ) == scanned_revision(path, input_digest, digest)
        newest = next(
            (
                record
                for _path, record in reversed(records)
                if any(
                    item.path == path and item.candidate_artifact_digest == digest
                    for item in record.members
                )
            ),
            None,
        )
        assert artifacts._accepted_artifact_coordinate(
            records, path=path, artifact_digest=digest, coordinates_by_sequence=coordinates
        ) == (None if newest is None else coordinates[newest.sequence])
        stamp = artifacts._accepted_artifact_timestamp(records, path=path, artifact_digest=digest)
        assert (stamp is None) == (newest is None)
        if newest is not None:
            assert stamp is not None and stamp.day == newest.sequence
