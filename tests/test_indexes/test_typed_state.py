"""Versioned typed storage parity and exact selection, independent of page layout."""

import sqlite3

from cruxible_core.compiler.assembler import PYTHON_REFERENCE_ASSEMBLER
from cruxible_core.indexes.projection import PROJECTION_STORAGE_SCHEMA_VERSION, AssemblerRequest
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
    parsed, original = _parse(instance)
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
    assert exported["storage_schema_version"] == PROJECTION_STORAGE_SCHEMA_VERSION
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
    # Every surviving owner family must use its identity seek; removed
    # Blueprint/interface/policy tables contribute no branch.
    expected_tables = {
        "resolution_contracts",
        "attestations",
        "claims",
        "procedures",
        "lines",
        "triggers",
        "subjects",
        "subject_kinds",
        "documents",
        "claim_types",
        "providers",
        "procedure_mandates",
        "query_definitions",
        "contracts",
        "capture_contracts",
        "exhaust_promotions",
    }
    assert {detail.split()[1] for detail in searches} == expected_tables
    assert len(searches) == len(expected_tables)
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
    from cruxible_client.contracts.procedures.artifacts import procedure_runnability
    from tests.test_procedures.test_procedure_measurement_readings import _world

    instance, _, procedure = _world(tmp_path)
    parsed, _ = _parse(instance)
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        reader = projection.typed
        assert reader is not None
        row = reader.connection.execute(
            "SELECT definition_format,runnable FROM procedures WHERE identity=?",
            (procedure.identity.qualified,),
        ).fetchone()
        assert tuple(row) == (
            str(procedure.definition.graph_format),
            procedure_runnability(procedure.definition)[0],
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
    from cruxible_client.contracts.procedures.artifacts import procedure_runnability
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
        from cruxible_client.contracts.procedures.artifacts import procedure_path, render_procedure
        from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
        from cruxible_core.claims.closure import parse_dependency_artifact

        assert (
            selected.pins
            == parse_dependency_artifact(
                line_spec_path(line.identity.name), render_line_spec(line)
            ).pins
        )
        assert selected.identity == line.identity
        selected_procedure = reader.dependency_state(procedure.identity.qualified)
        assert (
            selected_procedure.pins
            == parse_dependency_artifact(
                procedure_path(procedure.identity.name), render_procedure(procedure)
            ).pins
        )
        assert reader.dependency_state("Procedure:missing") is None
        assert [
            (row.identity, row.path, row.lifecycle, row.runnable)
            for row in reader.procedure_inventory()
        ] == [
            (
                procedure.identity.qualified,
                selected_procedure.path,
                procedure.lifecycle.state,
                procedure_runnability(procedure.definition)[0],
            )
        ]


def test_claim_type_name_selection_has_indexed_lookups():
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(schema_sql())
        for column in ("name", "leaf"):
            plan = [
                row[3]
                for row in connection.execute(
                    "EXPLAIN QUERY PLAN SELECT source_identity,kind,name "
                    f"FROM claim_type_names WHERE {column}=?",
                    ("status",),
                )
            ]
            assert len(plan) == 1
            assert "SEARCH claim_type_names USING COVERING INDEX" in plan[0]
            assert f"({column}=?)" in plan[0]


def test_claim_type_name_overlay_and_delta_match_cold_reconstruction(tmp_path):
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.claim_types import (
        claim_type_digest,
        claim_type_path,
        render_claim_type,
    )
    from cruxible_core.indexes.evaluated_state import EvaluationRows
    from cruxible_core.indexes.typed_sqlite import parse_static_owners, replace_rows
    from tests.test_claims.test_claims import _claim_type
    from tests.test_indexes.test_indexed_evaluation import _fixture

    original = _claim_type()
    path = claim_type_path(original.identity.name)
    sources = {path: render_claim_type(original)}
    tree, _, _ = _fixture(tmp_path, sources)
    successor = original.model_copy(
        update={
            "allowed_subject_kinds": ("new.work_item",),
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_type_digest(original).tagged),
        }
    )
    retired = successor.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired",
                predecessor_digest=claim_type_digest(original).tagged,
            )
        }
    )
    for index, changed in enumerate(
        (render_claim_type(successor), render_claim_type(retired), None)
    ):
        base = EvaluationRows(tree._accepted_reader())
        try:
            selected = base.overlay({path: changed})
            assert base.claim_type_names(("project.work_item",))[1] == ("project.work_item",)
            assert selected.claim_type_names(("project.work_item",)) == ((), ())
            if index == 0:
                assert selected.source(original.identity.qualified).allowed_subject_kinds == (
                    "new.work_item",
                )
                assert selected.claim_type_names(("status",))[0] == (original.identity.qualified,)
                assert selected.claim_type_names(("new.work_item",))[1] == ("new.work_item",)
            else:
                assert selected.claim_type_names(("status", "new.work_item")) == ((), ())
            candidate_rows = [
                tuple(row)
                for row in selected.connection.execute(
                    "SELECT * FROM selected_claim_type_names ORDER BY source_identity,kind,name"
                )
            ]
        finally:
            base.close()
        cold_dir = tmp_path / str(index)
        cold_dir.mkdir()
        updated = {} if changed is None else {path: changed}
        cold, _, _ = _fixture(cold_dir, updated)
        projection = cold._accepted_reader()
        try:
            expected = [
                tuple(row)
                for row in projection._connection.execute(
                    "SELECT * FROM claim_type_names ORDER BY source_identity,kind,name"
                )
            ]
            assert candidate_rows == expected
            warm = tree._accepted_reader()
            try:
                # A real delta writer must remove old name rows before inserting a successor.
                replace_rows(
                    warm._connection,
                    request=warm.accepted,
                    parsed=parse_static_owners(updated, accepted=warm.accepted),
                    sources=updated,
                    changed_paths=(path,),
                )
                assert [
                    tuple(row)
                    for row in warm._connection.execute(
                        "SELECT * FROM claim_type_names ORDER BY source_identity,kind,name"
                    )
                ] == expected
            finally:
                warm.close()  # Roll back; each case starts from the same accepted projection.
        finally:
            projection.close()


#: The one current physical schema pin. A schema change must deliberately
#: move its version and this expected digest; no historical reader matrix.
STORAGE_SCHEMA_DIGESTS = {
    26: "6b0ced62904e9163c790289625dcada9ae8311df8aa98488c83fea9d82fa2699",
}


def test_a_physical_schema_change_moves_the_storage_schema_version():
    import hashlib

    from cruxible_core.indexes.typed_sqlite import complete_schema_sql

    version = AssemblerRequest.model_fields["storage_schema_version"].default
    digest = hashlib.sha256(complete_schema_sql().encode()).hexdigest()
    assert STORAGE_SCHEMA_DIGESTS.get(version) == digest, (
        "The typed projection schema changed: bump storage_schema_version and pin "
        f"the new schema here ({version}: {digest})."
    )
    assert len(set(STORAGE_SCHEMA_DIGESTS.values())) == len(STORAGE_SCHEMA_DIGESTS)


def _seed_contracts(connection: sqlite3.Connection, names: list[str]) -> None:
    """Rows enough to exercise the ``contracts`` table's reads; no real Contract bytes."""

    for index, name in enumerate(names):
        path = f"contracts/{name}/v1.json"
        connection.execute(
            "INSERT INTO members(path, git_blob_oid, file_digest, byte_length) VALUES (?,?,?,?)",
            (path, f"oid{index}", f"digest{index}", 1),
        )
        connection.execute(
            "INSERT INTO contracts(identity, path, format_tag, artifact_digest, "
            "predecessor_digest, revision, lifecycle, contract_name, contract_version, "
            "field_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                f"Contract:{name}/v1",
                path,
                "cruxible-contract-v1",
                f"sha256:{index:064d}",
                None,
                1,
                "live",
                name,
                1,
                1,
            ),
        )
    connection.commit()


def _query_plan(connection: sqlite3.Connection, statement: str, params: tuple[object, ...]) -> str:
    return " | ".join(
        str(row[-1]) for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}", params)
    )


def test_newest_contract_lookup_seeks_the_name_version_index():
    """``get Contract:<name>`` (service/discovery/get.py's newest lookup) seeks, never scans."""

    connection = sqlite3.connect(":memory:")
    connection.executescript(schema_sql())
    _seed_contracts(connection, [f"security.c{i:04d}" for i in range(500)])
    plan = _query_plan(
        connection,
        "SELECT identity FROM contracts WHERE contract_name=? "
        "ORDER BY contract_version DESC LIMIT 1",
        ("security.c0250",),
    )
    assert "SCAN contracts" not in plan
    assert "contracts_by_name_version" in plan


def test_orient_contracts_page_is_a_bounded_keyset_seek_not_a_full_sort():
    """orient's contracts section (contracts_page) never sorts the whole population."""

    from cruxible_core.service.discovery.contracts import ContractKey, contracts_page

    connection = sqlite3.connect(":memory:")
    connection.executescript(schema_sql())
    names = sorted(f"security.c{i:04d}" for i in range(500))
    _seed_contracts(connection, names)

    page, stop = contracts_page(connection, limit=10, after=None)
    assert len(page) == 10
    assert [name for _identity, name, _version in page] == names[:10]
    assert stop == ContractKey(names[9], 1)

    plan = _query_plan(
        connection,
        "SELECT identity, contract_name, contract_version FROM contracts "
        "WHERE (contract_name, contract_version) > (?, ?) "
        "ORDER BY contract_name, contract_version LIMIT ?",
        (names[9], 1, 10),
    )
    assert "USE TEMP B-TREE" not in plan
    assert "contracts_by_name_version" in plan


#: The predicate-scoped Claim reads a served query makes (as the scaling probe saw them).
_PREDICATE_READS = (
    "SELECT path FROM claims INDEXED BY claims_by_predicate "
    "WHERE 1 AND predicate IN (?) AND lifecycle='live' ORDER BY path",
    "SELECT DISTINCT t.path FROM claims c INDEXED BY claims_by_predicate "
    "JOIN claim_types t ON t.identity=c.claim_type_identity "
    "WHERE 1 AND c.predicate IN (?) AND c.lifecycle='live'",
    "SELECT path,envelope_digest,artifact_digest FROM attestations WHERE "
    "(claim_identity,claim_artifact_digest) IN (SELECT identity,artifact_digest FROM claims "
    "INDEXED BY claims_by_predicate "
    "WHERE predicate IN (?)) ORDER BY attested_at_us,envelope_digest",
)


def _claim_row(number: int, predicate: str) -> tuple[object, ...]:
    return (
        f"Claim:CLM-{number:032d}",
        f"claims/{number:02d}/CLM-{number:032d}.json",
        "playbill-claim-v3",
        f"sha256:{number:064d}",
        1,
        "live",
        f"sha256:{number:064d}",
        f"ClaimType:{predicate}",
        f"sha256:{0:064d}",
        f"subjects/other.kind/s-{number}.json",
        "artifact-v1",
        "",
        predicate,
        "normative",
        "literal",
        "string",
        f"s-{number}",
    )


def _predicate_read_work(other_claims: int, index: str) -> tuple[list[int], list[int]]:
    """VM instructions per predicate-scoped read, beside ``other_claims`` other-kind key Claims."""

    with sqlite3.connect(":memory:") as connection:
        connection.executescript(schema_sql())
        rows = [_claim_row(number, "other.kind.slug") for number in range(other_claims)]
        rows += [_claim_row(other_claims + n, "project.work_item.status") for n in range(2)]
        connection.executemany(
            "INSERT INTO claims (identity,path,format_tag,artifact_digest,revision,lifecycle,"
            "statement_digest,claim_type_identity,claim_type_digest,subject_path,"
            "subject_selector_scheme,subject_selector_value,predicate,role,object_kind,"
            "literal_type,literal_text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        # Projections are never ANALYZEd: the planner reads the schema alone.
        work = []
        visited = []
        for statement in _PREDICATE_READS:
            statement = statement.replace("claims_by_predicate", index)
            steps = [0]

            def tick(steps: list[int] = steps) -> int:
                steps[0] += 1
                return 0

            connection.set_progress_handler(tick, 1)
            connection.execute(statement, ("project.work_item.status",)).fetchall()
            connection.set_progress_handler(None, 1)
            work.append(steps[0])
            # Count the Claim rows reached inside each seek, including the
            # attestation subquery even when no attestation is present.
            visits = []
            connection.create_function(
                "visit_claim", 1, lambda identity: visits.append(identity) or 1
            )
            column = "c.predicate" if "c.predicate" in statement else "predicate"
            identity = "c.identity" if column == "c.predicate" else "identity"
            counted = statement.replace(
                column + " IN (?)", column + f" IN (?) AND visit_claim({identity})"
            )
            assert connection.execute(counted, ("project.work_item.status",)).fetchall() == (
                connection.execute(statement, ("project.work_item.status",)).fetchall()
            )
            visited.append(len(visits))
        return work, visited


def test_predicate_scoped_claim_reads_seek_and_do_not_grow_with_other_key_claims(record_property):
    """Every keyed Subject holds a key Claim, so other kinds' Claims must never be scanned.

    ``claims_by_predicate`` answers each predicate-scoped read by seeking its
    predicate (review follow-up: counted work for this index).
    """

    with sqlite3.connect(":memory:") as connection:
        connection.executescript(schema_sql())
        for statement in _PREDICATE_READS:
            plan = " ".join(
                str(row[3]) for row in connection.execute("EXPLAIN QUERY PLAN " + statement, ("x",))
            )
            assert "claims_by_predicate" in plan, plan
            record_property("predicate_plan", plan)
            for label, replacement in (
                ("unhinted", ""),
                ("literal", "INDEXED BY claims_by_literal_boolean"),
            ):
                alternative = statement.replace("INDEXED BY claims_by_predicate", replacement)
                alternative_plan = " ".join(
                    str(row[3])
                    for row in connection.execute("EXPLAIN QUERY PLAN " + alternative, ("x",))
                )
                record_property(label + "_plan", alternative_plan)
                if label == "literal":
                    assert "SEARCH" in alternative_plan and "(predicate=?)" in alternative_plan
    small, small_rows = _predicate_read_work(10, "claims_by_predicate")
    large, large_rows = _predicate_read_work(2000, "claims_by_predicate")
    record_property("predicate_vm_steps", str((small, large)))
    record_property("predicate_claim_rows", str((small_rows, large_rows)))
    assert small_rows == large_rows == [2, 2, 2]
    # A deeper b-tree costs a few instructions per seek, never a row per other Claim.
    assert all(after - before <= 16 for before, after in zip(small, large, strict=True)), (
        small,
        large,
    )

    # The competing scalar index is a predicate seek too, but cannot use the
    # lifecycle suffix without constraining its literal columns. Keep the
    # dedicated index and its strict plan assertion rather than accepting ties.
    literal_small, literal_small_rows = _predicate_read_work(10, "claims_by_literal_boolean")
    literal_large, literal_large_rows = _predicate_read_work(2000, "claims_by_literal_boolean")
    record_property("literal_vm_steps", str((literal_small, literal_large)))
    record_property("literal_claim_rows", str((literal_small_rows, literal_large_rows)))
    assert literal_small_rows == literal_large_rows == [2, 2, 2]
    assert all(
        after - before <= 16 for before, after in zip(literal_small, literal_large, strict=True)
    ), (literal_small, literal_large)


def test_served_predicate_reads_use_the_pinned_statements(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from cruxible_core.service.discovery.query import build_accepted_query_facts

    instance, _, _ = _accepted_claim_world(tmp_path)
    statements = []
    bind = instance.bind_accepted_projection

    @contextmanager
    def traced(*args, **kwargs):
        with bind(*args, **kwargs) as projection:
            projection.typed.connection.set_trace_callback(statements.append)
            try:
                yield projection
            finally:
                projection.typed.connection.set_trace_callback(None)

    monkeypatch.setattr(instance, "bind_accepted_projection", traced)
    facts = build_accepted_query_facts(
        instance,
        coordinate=instance.accepted_coordinate(),
        predicates=("project.work_item.status",),
    )
    assert facts.claims
    for statement in _PREDICATE_READS:
        assert statement.replace("?", "'project.work_item.status'") in statements


def _slot_reads() -> tuple[tuple[str, tuple[str, ...]], ...]:
    from cruxible_core.indexes.subject_identity import LIVE_KEY_CLAIMS_SQL
    from cruxible_core.indexes.typed_state import (
        LIVE_SLOT_CLAIM_DIGESTS_SQL,
        LIVE_SLOT_CLAIMS_SQL,
        LIVE_SUBJECT_PREDICATE_CLAIMS_SQL,
    )

    slot = ("subjects/other.kind/s-1.json", "other.kind.slug")
    return (
        (LIVE_SLOT_CLAIMS_SQL, (*slot, None)),
        (LIVE_SLOT_CLAIM_DIGESTS_SQL, (*slot, None)),
        (LIVE_SUBJECT_PREDICATE_CLAIMS_SQL, slot),
        (LIVE_KEY_CLAIMS_SQL, slot),
    )


def _slot_world(connection: sqlite3.Connection, other_claims: int) -> None:
    connection.executescript(schema_sql())
    rows = [_claim_row(number, "other.kind.slug") for number in range(other_claims)]
    # A second, retired Claim in the probed slot: the lifecycle filter still applies.
    retired = list(_claim_row(other_claims, "other.kind.slug"))
    retired[5], retired[9] = "retired", "subjects/other.kind/s-1.json"
    connection.executemany(
        "INSERT INTO claims (identity,path,format_tag,artifact_digest,revision,lifecycle,"
        "statement_digest,claim_type_identity,claim_type_digest,subject_path,"
        "subject_selector_scheme,subject_selector_value,predicate,role,object_kind,"
        "literal_type,literal_text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [*rows, tuple(retired)],
    )


def test_live_slot_claim_reads_seek_the_subject_predicate_index():
    """A slot's live Claims are one seek, never a walk of the live Claims of a predicate.

    Without the no-op ``+lifecycle`` the unanalyzed planner picks
    ``claims_by_lifecycle`` or ``claims_by_predicate`` and visits every live
    Claim (of the predicate); the answer is unchanged either way.
    """

    with sqlite3.connect(":memory:") as connection:
        connection.executescript(schema_sql())
        for statement, params in _slot_reads():
            plan = [
                str(row[3]) for row in connection.execute("EXPLAIN QUERY PLAN " + statement, params)
            ]
            assert plan[0].startswith("SEARCH claims USING INDEX claims_by_subject_predicate"), plan
            assert "(subject_path=? AND predicate=?" in plan[0], plan

    def work(other_claims: int) -> list[int]:
        with sqlite3.connect(":memory:") as connection:
            _slot_world(connection, other_claims)
            counts = []
            for statement, params in _slot_reads():
                steps = [0]

                def tick(steps: list[int] = steps) -> int:
                    steps[0] += 1
                    return 0

                connection.set_progress_handler(tick, 1)
                found = connection.execute(statement, params).fetchall()
                connection.set_progress_handler(None, 1)
                # The unplussed statement (the old planner choice) answers identically.
                assert (
                    found
                    == connection.execute(
                        statement.replace("+lifecycle", "lifecycle"), params
                    ).fetchall()
                )
                assert [row[0] for row in found] == ["Claim:CLM-" + f"{1:032d}"]
                counts.append(steps[0])
            return counts

    small, large = work(10), work(2000)
    assert all(after - before <= 16 for before, after in zip(small, large, strict=True)), (
        small,
        large,
    )
