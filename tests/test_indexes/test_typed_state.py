"""Versioned typed storage parity and exact selection, independent of page layout."""

import sqlite3

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_core.compiler.assembler import PYTHON_REFERENCE_ASSEMBLER
from cruxible_core.indexes.projection import AssemblerRequestV2, projection_manifest_name
from cruxible_core.indexes.sqlite import (
    canonical_logical_export,
    initialize_projection_database,
    projection_logical_digest,
)
from cruxible_core.indexes.typed_state import TypedStateReader, schema_sql
from tests.test_claims.test_claim_type_migrations import _accepted_claim_world
from tests.test_indexes.test_projection_claim_reuse import _parse


def test_typed_claim_source_parity_and_no_builtin_payload_copy(tmp_path):
    instance, claim_id, _ = _accepted_claim_world(tmp_path)
    parsed, original, registry = _parse(instance)
    request = AssemblerRequestV2(**original.model_dump(exclude={"tag"}))
    assert request.compiler_digest == original.compiler_digest
    assert projection_manifest_name(request) != projection_manifest_name(original)
    path = tmp_path / "typed.sqlite"
    initialize_projection_database(
        path,
        request=request,
        parsed=parsed,
        registry=registry,
        assembler_implementation=PYTHON_REFERENCE_ASSEMBLER,
        bodies=instance.body_store(),
    )
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    reader = TypedStateReader(
        connection, instance.accepted_coordinate(), instance._ledger, bodies=instance.body_store()
    )
    identity = f"Claim:{claim_id}"
    row = reader.envelope(identity)
    assert row == next(row for row in parsed.envelopes if row.identity == identity)
    actual = reader.facts(identity=identity)
    expected = tuple(f for f in parsed.semantic_facts if f.subject_identity == identity)
    assert actual == expected
    assert reader.source(identity).identity.qualified == identity
    assert connection.execute("SELECT count(*) FROM semantic_facts").fetchone()[0] == 0
    names = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert not {"artifact_envelopes", "live_identities"} & names
    assert connection.execute(
        "SELECT subject_selector_scheme,subject_selector_value FROM claims"
    ).fetchone()[:] == ("artifact-v1", "")
    assert reader.principal("owner", active=True).principal_id == "owner"
    assert reader.principal_registry().semantic_root == instance.accepted_coordinate().semantic_root
    exported = canonical_logical_export(path)
    assert exported["storage_schema_version"] == 2
    assert projection_logical_digest(path) == projection_logical_digest(path)
    connection.close()


def test_frozen_v1_request_and_export_stay_distinct(tmp_path):
    instance, _, _ = _accepted_claim_world(tmp_path)
    parsed, request, registry = _parse(instance)
    before = canonical_bytes(request.model_dump(mode="json"))
    path = tmp_path / "frozen.sqlite"
    initialize_projection_database(
        path,
        request=request,
        parsed=parsed,
        registry=registry,
        assembler_implementation=PYTHON_REFERENCE_ASSEMBLER,
    )
    export = canonical_logical_export(path)
    assert export["schema_version"] == 1
    assert "storage_schema_version" not in export
    assert "storage_schema_version" not in request.model_dump()
    assert before == canonical_bytes(request.model_dump(mode="json"))


def test_typed_reverse_and_full_address_indexes_are_present():
    connection = sqlite3.connect(":memory:")
    connection.executescript(schema_sql())
    index_columns = tuple(
        row[2] for row in connection.execute("PRAGMA index_info(claims_by_subject_predicate)")
    )
    assert index_columns == (
        "subject_path",
        "predicate",
        "subject_selector_scheme",
        "subject_selector_value",
        "identity",
    )
    branches = tuple(
        connection.execute(
            "EXPLAIN QUERY PLAN SELECT identity FROM artifact_lookup WHERE identity='Claim:abc'"
        )
    )
    searches = tuple(row[3] for row in branches if row[3].startswith("SEARCH "))
    assert len(searches) == 15
    assert all("(identity=?)" in detail for detail in searches)
    connection.close()


def test_v2_preserves_extensions_and_old_publication_beside_same_coordinate(tmp_path):
    import pytest

    from cruxible_client.contracts.errors import ProjectionIntegrityError
    from cruxible_core.compiler.assembler import ProjectionAssembler
    from cruxible_core.indexes.sqlite import bind_projection, load_projection_manifest
    from tests.core_support._projection_support import (
        MemoryLedger,
        accepted_coordinate,
        fixture_bytes,
        presentation_bytes,
    )

    repository = MemoryLedger(
        tmp_path / "repo",
        {
            "artifacts/fixtures/one.yaml": fixture_bytes("one", {"retained": True}),
            "presentation/fixtures/one.json": presentation_bytes("one", "Visible label"),
        },
    )
    coordinate = accepted_coordinate(repository)
    publication = tmp_path / "published"
    publication.mkdir()
    results = []
    for version in (1, 2):
        assembler = ProjectionAssembler(
            repository,
            accepted=coordinate,
            publication_directory=publication,
            storage_schema_version=version,
        )
        result = assembler.assemble(
            assembler.request(output_staging_directory=publication / f".stage-version-{version}")
        )
        results.append(result)
        assert (
            load_projection_manifest(publication / result.manifest_path).tag == result.manifest.tag
        )
        with bind_projection(publication / result.manifest_path, expected=coordinate) as handle:
            fixture = handle.fixture("one")
            assert fixture["facts"][0]["value"] == {"retained": True}
            assert handle.semantic_facts("playbill.fixture.fact")[0].value == {"retained": True}
            assert (
                handle._connection.execute("SELECT count(*) FROM presentation_facts").fetchone()[0]
                == 1
            )
        for read in (
            lambda: handle.fixture("one"),
            handle.artifact_envelopes,
            lambda: handle.claim("Claim:missing"),
        ):
            with pytest.raises(ProjectionIntegrityError, match="closed"):
                read()
    assert results[0].manifest_path != results[1].manifest_path
    assert results[0].logical_digest != results[1].logical_digest
