"""Versioned typed storage parity and exact selection, independent of page layout."""

import sqlite3

from cruxible_core.compiler.assembler import PYTHON_REFERENCE_ASSEMBLER
from cruxible_core.indexes.projection import AssemblerRequest
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
    request = AssemblerRequest(**original.model_dump(exclude={"tag"}))
    assert request.compiler_digest == original.compiler_digest
    path = tmp_path / "typed.sqlite"
    initialize_projection_database(
        path,
        request=request,
        parsed=parsed,
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
    names = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert (
        not {
            "artifact_envelopes",
            "live_identities",
            "fixtures",
            "semantic_facts",
            "presentation_facts",
            "projection_fact_schemas",
            "presentation_fact_schemas",
        }
        & names
    )
    assert connection.execute(
        "SELECT subject_selector_scheme,subject_selector_value FROM claims"
    ).fetchone()[:] == ("artifact-v1", "")
    assert reader.principal("owner", active=True).principal_id == "owner"
    assert reader.principal_registry().semantic_root == instance.accepted_coordinate().semantic_root
    exported = canonical_logical_export(path)
    assert exported["storage_schema_version"] == 3
    assert projection_logical_digest(path) == projection_logical_digest(path)
    connection.close()


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
    assert len(searches) == 14
    assert all("(identity=?)" in detail for detail in searches)
    connection.close()


def test_claim_lifecycle_selection_uses_covering_index():
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(schema_sql())
        plan = tuple(
            row[3]
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT identity FROM claims "
                "WHERE lifecycle='retired' ORDER BY identity"
            )
        )
    assert plan == ("SEARCH claims USING COVERING INDEX claims_by_lifecycle (lifecycle=?)",)


def test_registered_procedure_and_singletons_read_exact_selected_sources(tmp_path, monkeypatch):
    from cruxible_client.contracts.approval_policy import APPROVAL_POLICY_IDENTITY
    from tests.test_procedures.test_procedure_measurement_readings import _world

    instance, _, procedure = _world(tmp_path)
    parsed, _, _ = _parse(instance)
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        reader = projection.typed
        assert reader is not None
        row = reader.connection.execute(
            "SELECT definition_format,directly_runnable FROM procedures WHERE identity=?",
            (procedure.identity.qualified,),
        ).fetchone()
        assert tuple(row) == (
            str(procedure.definition.graph_format),
            int(procedure.directly_runnable),
        )
        assert reader.source(procedure.identity.qualified) == procedure
        from cruxible_core.indexes.history.history_index import HistoryReader

        monkeypatch.setattr(
            HistoryReader,
            "read_member_record",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("singleton envelope read old record body")
            ),
        )
        assert reader.envelope(APPROVAL_POLICY_IDENTITY) == next(
            row for row in parsed.envelopes if row.identity == APPROVAL_POLICY_IDENTITY
        )


def test_line_identity_lookup_and_role_sensitive_dependency_read(tmp_path):
    from cruxible_client.contracts.procedures.line_specs import line_identity_digest
    from tests.test_procedures.test_procedure_measurement_readings import _line_world

    instance, procedure, line = _line_world(tmp_path)
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        reader = projection.typed
        assert reader is not None
        assert (
            reader.connection.execute(
                "SELECT identity FROM lines WHERE identity_digest=?",
                (line_identity_digest(line.identity),),
            ).fetchone()[0]
            == line.identity.qualified
        )
        selected = reader.dependency_state(line.identity.qualified)
        assert selected.pins == line.pins
        assert selected.identity == line.identity
        selected_procedure = reader.dependency_state(procedure.identity.qualified)
        assert selected_procedure.pins == procedure.pins
        assert reader.dependency_state("Procedure:missing") is None
        assert [
            (row.identity, row.path, row.lifecycle, row.directly_runnable)
            for row in reader.procedure_inventory()
        ] == [
            (
                procedure.identity.qualified,
                selected_procedure.path,
                procedure.lifecycle.state,
                procedure.directly_runnable,
            )
        ]
