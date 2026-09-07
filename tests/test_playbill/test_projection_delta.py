"""Changeset successor compilation equals cold reconstruction, including presentation rows."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from cruxible_client.contracts.authoring.models import ClaimRetirementMemberV1
from cruxible_client.contracts.errors import ProjectionIntegrityError, SettlementIntegrityError
from cruxible_core.playbill import projection_delta as delta_module
from cruxible_core.playbill.assembler import ProjectionAssembler
from cruxible_core.playbill.proposals import AuthenticatedActor
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_authoring_change_set_intents import (
    _accept,
    _change_set,
    _claim,
    _coordinator,
)
from tests.test_playbill.test_authoring_preflight import TIMESTAMP, _seed_claim_surface


def _rows(path):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {name: sorted(db.execute(f"SELECT * FROM {name}").fetchall()) for name in names}


def test_successor_matches_every_cold_row_across_create_revise_and_retire(tmp_path, monkeypatch):
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    assemble = ProjectionAssembler.assemble
    seen = []
    compiled_paths = []
    parse = delta_module.parse_projection_tree

    def bounded_parse(blobs, **kwargs):
        compiled_paths.append(set(blobs))
        return parse(blobs, **kwargs)

    monkeypatch.setattr(delta_module, "parse_projection_tree", bounded_parse)

    def checked(assembler, request, *, crash_hook=None, delta=None):
        result = assemble(assembler, request, crash_hook=crash_hook, delta=delta)
        if delta is None:
            return result
        directory = tmp_path / f"cold-{len(seen)}"
        directory.mkdir()
        # Candidate citation maintenance also binds its exact immutable parent.
        parent_files = list(assembler.publication_directory.glob("*.sqlite")) + list(
            assembler.publication_directory.glob("projection-*.json")
        )
        for p in parent_files:
            if p.name != Path(result.manifest_path).name and p.name not in {
                piece.name for piece in result.manifest.pieces
            }:
                (directory / p.name).write_bytes(p.read_bytes())
        cold = ProjectionAssembler(
            assembler._repository,
            accepted=assembler.accepted,
            publication_directory=directory,
            registry=assembler.registry,
            bodies=assembler.bodies,
            accepted_coordinates_by_sequence=assembler.accepted_coordinates_by_sequence,
        )
        rebuilt = assemble(cold, cold.request(output_staging_directory=directory / ".stage-cold"))
        assert result.logical_digest == rebuilt.logical_digest
        assert result.row_counts == rebuilt.row_counts
        assert _rows(Path(result.manifest_path).parent / result.manifest.pieces[0].name) == _rows(
            directory / rebuilt.manifest.pieces[0].name
        )
        assert compiled_paths[-1] <= {m.path for m in delta.bundle.record.members}
        seen.append(result)
        return result

    monkeypatch.setattr(ProjectionAssembler, "assemble", checked)

    def accept(payload):
        intent = coordinator.create(
            actor=actor, payload=payload, canonical_timestamp=TIMESTAMP
        ).intent
        _accept(instance, owner, coordinator, intent.intent_id, actor)
        return intent

    first = accept(_change_set(_claim(qualifier="a"), _claim(qualifier="b")))
    from cruxible_client.contracts.claims import claim_path, parse_claim

    ids_by_qualifier = {
        parse_claim(
            instance.tree_at(instance.accepted_coordinate().git_oid)[claim_path(i.claim_id)],
            path=claim_path(i.claim_id),
        ).statement.qualifier: i.claim_id
        for i in first.change_set_claim_identities
    }
    ids = [ids_by_qualifier["a"], ids_by_qualifier["b"]]
    historical = Path(seen[0].manifest_path).parent / seen[0].manifest.pieces[0].name
    old_bytes = hashlib.sha256(historical.read_bytes()).digest()
    accept(_change_set(_claim(qualifier="a", claim_ref=ids[0], value="done")))
    accept(_change_set(ClaimRetirementMemberV1(claim_ref=ids[1], reason="was-rescinded")))
    assert len(seen) == 3
    assert hashlib.sha256(historical.read_bytes()).digest() == old_bytes


def test_delta_rejects_wrong_successor_before_consuming_artifacts(tmp_path, monkeypatch):
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    assemble = ProjectionAssembler.assemble

    def wrong(assembler, request, *, crash_hook=None, delta=None):
        if delta is not None:
            delta = replace(delta, base=delta.base.model_copy(update={"git_oid": "0" * 40}))
        return assemble(assembler, request, crash_hook=crash_hook, delta=delta)

    monkeypatch.setattr(ProjectionAssembler, "assemble", wrong)
    before = instance.accepted_coordinate()
    intent = coordinator.create(
        actor=actor, payload=_change_set(_claim()), canonical_timestamp=TIMESTAMP
    ).intent
    with pytest.raises(
        (ProjectionIntegrityError, SettlementIntegrityError), match="base|successor"
    ):
        _accept(instance, owner, coordinator, intent.intent_id, actor)
    assert instance.accepted_coordinate() == before


@pytest.mark.parametrize("parent_state", ["missing", "corrupt"])
def test_parent_recovery_or_refusal_preserves_authority(tmp_path, monkeypatch, parent_state):
    from cruxible_core.playbill.projection import AssemblerRequest, projection_manifest_name

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    assemble = ProjectionAssembler.assemble
    existing = coordinator.create(
        actor=actor,
        payload=_change_set(_claim(qualifier="existing")),
        canonical_timestamp=TIMESTAMP,
    ).intent
    _accept(instance, owner, coordinator, existing.intent_id, actor)
    populate = delta_module.populate_successor
    outcomes = []

    def tracked(*args, **kwargs):
        result = populate(*args, **kwargs)
        outcomes.append(result)
        return result

    def damaged(assembler, request, *, crash_hook=None, delta=None):
        if delta is None:
            return assemble(assembler, request, crash_hook=crash_hook)
        base = delta.base
        parent_request = AssemblerRequest(
            instance_id=base.instance_id,
            repository_path=base.repository_path,
            git_object_format=base.git_object_format,
            git_oid=base.git_oid,
            semantic_root=base.semantic_root,
            generation_root=base.generation_root,
            compiler_digest=base.compiler.rule_digest,
            schema_version=base.compiler.schema_version,
            output_staging_directory=request.output_staging_directory,
            limits=request.limits,
        )
        manifest = assembler.publication_directory / projection_manifest_name(parent_request)
        if parent_state == "missing":
            manifest.unlink()
        else:
            manifest.chmod(0o600)
            manifest.write_bytes(b"corrupt")
        return assemble(assembler, request, crash_hook=crash_hook, delta=delta)

    monkeypatch.setattr(delta_module, "populate_successor", tracked)
    monkeypatch.setattr(ProjectionAssembler, "assemble", damaged)
    before = instance.accepted_coordinate()
    intent = coordinator.create(
        actor=actor, payload=_change_set(_claim()), canonical_timestamp=TIMESTAMP
    ).intent
    if parent_state == "corrupt":
        with pytest.raises(ProjectionIntegrityError, match="manifest"):
            _accept(instance, owner, coordinator, intent.intent_id, actor)
        assert instance.accepted_coordinate() == before
    else:
        _accept(instance, owner, coordinator, intent.intent_id, actor)
        assert outcomes == [None]
        assert instance.accepted_coordinate() != before
        recovered = type(instance).open(instance.root, trust_root=instance.trust_root)
        assert recovered.accepted_coordinate() == instance.accepted_coordinate()
