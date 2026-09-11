"""Changeset successor compilation equals cold reconstruction, including presentation rows."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from cruxible_client.contracts.authoring.models import ClaimRetirementMemberV1
from cruxible_client.contracts.errors import ProjectionIntegrityError, SettlementIntegrityError
from cruxible_core.compiler import projection_delta as delta_module
from cruxible_core.compiler.assembler import ProjectionAssembler
from cruxible_core.proposals.proposals import AuthenticatedActor
from tests.core_support._support import initialize_local
from tests.test_authoring.test_authoring_change_set_intents import (
    _accept,
    _change_set,
    _claim,
    _coordinator,
)
from tests.test_authoring.test_authoring_preflight import TIMESTAMP, _seed_claim_surface


def _rows(path):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {name: sorted(db.execute(f"SELECT * FROM {name}").fetchall()) for name in names}


def test_standalone_cold_preserves_historical_consumed_input_resolution(tmp_path):
    from cruxible_core.indexes.sqlite import bind_projection
    from tests.test_claims.test_claim_retirement import _accepted_dependency_world

    instance, *_ = _accepted_dependency_world(tmp_path)
    directory = tmp_path / "standalone-cold"
    directory.mkdir()
    assembler = ProjectionAssembler(
        instance._ledger,
        accepted=instance.accepted_coordinate(),
        publication_directory=directory,
        bodies=instance.body_store(),
        accepted_coordinates_by_sequence=instance._accepted_coordinates_by_sequence(),
    )
    rebuilt = assembler.assemble(
        assembler.request(output_staging_directory=directory / ".stage-cold")
    )
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as published:
        with bind_projection(
            directory / rebuilt.manifest_path, expected=instance.accepted_coordinate()
        ) as cold:
            sql = "SELECT * FROM pins ORDER BY source_identity,edge_kind,ordinal"
            assert [tuple(row) for row in cold._connection.execute(sql)] == [
                tuple(row) for row in published._connection.execute(sql)
            ]
            assert cold.manifest.logical_digest == published.manifest.logical_digest


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
        inventories = []
        listing = assembler._repository.list_tree_with_sizes

        def counted(oid):
            inventories.append(oid)
            return listing(oid)

        with monkeypatch.context() as patch:
            patch.setattr(assembler._repository, "list_tree_with_sizes", counted)
            result = assemble(assembler, request, crash_hook=crash_hook, delta=delta)
        if delta is None:
            return result
        assert inventories.count(request.git_oid) == 1
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
    from cruxible_core.indexes.projection import AssemblerRequest, projection_manifest_name

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


def test_unrelated_document_carries_nonempty_citation_relations_without_rebuild(
    tmp_path, monkeypatch
):
    from cruxible_client.contracts.documents import (
        DocumentAuthority,
        DocumentLifecycle,
        DocumentShell,
        render_document,
    )
    from cruxible_core.indexes.serving import bind_current_projection
    from tests.test_indexes.test_resolution_contracts import _accept_tree

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    intent = coordinator.create(
        actor=actor, payload=_change_set(_claim()), canonical_timestamp=TIMESTAMP
    ).intent
    _accept(instance, owner, coordinator, intent.intent_id, actor)
    publication = Path(instance.inspect().storage_directories["projections"])
    with bind_current_projection(publication, expected=instance.accepted_coordinate()) as handle:
        prior_uses = tuple(
            tuple(row)
            for row in handle._connection.execute(
                "SELECT * FROM citation_uses ORDER BY owner_kind,owner_key,use_key"
            )
        )
        assert prior_uses
    body = instance.store_document_body(b"unrelated document")
    document = DocumentShell(
        identity="document:unrelated",
        document_kind="note",
        title="Unrelated",
        media_type="text/plain",
        body_digest=body.digest,
        authority=DocumentAuthority(required_tier="governed_write"),
        governance_scope=("project:test",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    with monkeypatch.context() as patch:

        def forbidden(*args, **kwargs):
            raise AssertionError("unrelated members cannot require citation reconstruction")

        from cruxible_core.indexes import typed_sqlite

        original_populate = typed_sqlite.populate_citations

        def only_unrelated(connection, sources, **kwargs):
            assert not any(path.startswith("claims/") for path in sources)
            return original_populate(connection, sources, **kwargs)

        patch.setattr(typed_sqlite, "populate_citations", only_unrelated)
        _accept_tree(
            instance,
            owner,
            {
                **instance.tree_at(instance.accepted_coordinate().git_oid),
                "documents/unrelated.json": render_document(document),
            },
            timestamp=TIMESTAMP,
            proposal_name="unrelated-document",
        )
    with bind_current_projection(publication, expected=instance.accepted_coordinate()) as handle:
        assert (
            tuple(
                tuple(row)
                for row in handle._connection.execute(
                    "SELECT * FROM citation_uses ORDER BY owner_kind,owner_key,use_key"
                )
            )
            == prior_uses
        )
        expected = _rows(handle.index_path)
    directory = tmp_path / "cold-oracle"
    directory.mkdir()
    assembler = ProjectionAssembler(
        instance._ledger,
        accepted=instance.accepted_coordinate(),
        publication_directory=directory,
        bodies=instance.body_store(),
        accepted_coordinates_by_sequence=instance._accepted_coordinates_by_sequence(),
    )
    rebuilt = assembler.assemble(
        assembler.request(output_staging_directory=directory / ".stage-cold")
    )
    assert _rows(directory / rebuilt.manifest.pieces[0].name) == expected


def test_warm_citation_successor_reconstructs_only_changed_claims(tmp_path, monkeypatch):
    from cruxible_core.indexes import typed_sqlite

    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    first = coordinator.create(
        actor=actor, payload=_change_set(_claim(qualifier="first")), canonical_timestamp=TIMESTAMP
    ).intent
    _accept(instance, owner, coordinator, first.intent_id, actor)
    before = instance.accepted_coordinate()
    with instance.bind_accepted_projection(before) as handle:
        assert handle._connection.execute("SELECT count(*) FROM citation_uses").fetchone()[0] > 0
    original = typed_sqlite.populate_citations
    selected_sources = []

    def bounded(connection, sources, **kwargs):
        claim_paths = tuple(path for path in sources if path.startswith("claims/"))
        assert len(claim_paths) <= 1
        selected_sources.append(claim_paths)
        return original(connection, sources, **kwargs)

    second = coordinator.create(
        actor=actor, payload=_change_set(_claim(qualifier="second")), canonical_timestamp=TIMESTAMP
    ).intent
    with monkeypatch.context() as patch:
        patch.setattr(typed_sqlite, "populate_citations", bounded)
        _accept(instance, owner, coordinator, second.intent_id, actor)
    assert any(selected_sources)
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as handle:
        assert handle._connection.execute("SELECT count(*) FROM citation_uses").fetchone()[0] > 0
    with instance.bind_accepted_projection(before) as handle:
        assert handle._connection.execute("SELECT count(*) FROM citation_uses").fetchone()[0] > 0
