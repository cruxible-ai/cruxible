"""All-field frozen parity and bounded request-owned SQL candidate selection."""

from __future__ import annotations

import hashlib
import sqlite3
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.subjects import render_subject
from cruxible_core.compiler.assembler import ProjectionAssembler
from cruxible_core.compiler.compiler import P2_B5_COMPILER
from cruxible_core.compiler.projection_artifacts import parse_projection_tree
from cruxible_core.derived.derived_state import SnapshotTree
from cruxible_core.indexes.evaluated_state import derive_indexed_state
from cruxible_core.indexes.sqlite import initialize_projection_database
from cruxible_core.indexes.typed_state import TypedStateReader
from cruxible_core.proposals.proposals import (
    advance_tree_members,
    advance_tree_state,
    build_tree_state,
)
from tests.core_support._projection_support import MemoryLedger, accepted_coordinate
from tests.test_claims.test_incremental_closure import _assert_same_index, _path, _pin_to, _subject


def _tree():
    anchor = _subject("anchor")
    return {
        _path("anchor"): render_subject(anchor),
        _path("dependent"): render_subject(_subject("dependent", pins=(_pin_to(anchor),))),
        _path("unrelated"): render_subject(_subject("unrelated")),
    }


def _fixture(tmp_path, sources):
    repository = MemoryLedger(tmp_path / "repository", sources)
    coordinate = accepted_coordinate(repository).model_copy(update={"compiler": P2_B5_COMPILER})
    directory = tmp_path / "projection"
    directory.mkdir()
    assembler = ProjectionAssembler(
        repository, accepted=coordinate, publication_directory=directory
    )
    request = assembler.request(output_staging_directory=directory / ".stage")
    parsed = parse_projection_tree(
        sources,
        registry=assembler.registry,
        artifact_kinds=assembler.artifact_kinds,
        artifact_codec=assembler.artifact_codec,
    )
    path = directory / "fixture.sqlite"
    initialize_projection_database(
        path,
        request=request,
        parsed=parsed,
        registry=assembler.registry,
        assembler_implementation="test",
        sources=sources,
    )
    blobs = {
        hashlib.new(
            coordinate.git_object_format, b"blob " + str(len(body)).encode() + b"\0" + body
        ).hexdigest(): body
        for body in sources.values()
    }
    counts = {"opened": 0, "closed": 0, "blobs": 0}
    connections = []

    def read_blob(oid):
        counts["blobs"] += 1
        return blobs[oid]

    def factory():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connections.append(connection)
        counts["opened"] += 1

        def close():
            counts["closed"] += 1
            connection.close()

        return SimpleNamespace(
            typed=TypedStateReader(connection, coordinate, SimpleNamespace(read_blob=read_blob)),
            accepted=coordinate,
            _connection=connection,
            index_path=path,
            close=close,
        )

    tree = SnapshotTree(sources)
    tree._accepted = True
    tree._accepted_reader = factory
    return tree, counts, connections


def _same(actual, expected):
    with actual.scope():
        assert dict(actual.members) == dict(expected.members)
        assert actual.merkle == expected.merkle
        _assert_same_index(actual.dependencies, expected.dependencies)
        assert dict(actual.claim_subjects.subject_by_claim) == dict(
            expected.claim_subjects.subject_by_claim
        )
        assert dict(actual.claim_subjects.claims_by_subject) == dict(
            expected.claim_subjects.claims_by_subject
        )


@pytest.mark.parametrize("operation", ["replace", "delete", "reinstate"])
def test_every_evaluated_field_matches_oracle_and_connections_close(tmp_path, operation):
    sources = _tree()
    tree, counts, connections = _fixture(tmp_path, sources)
    state = derive_indexed_state(tree)
    _same(state, build_tree_state(sources))
    candidate = tree.fork()
    if operation == "replace":
        candidate[_path("dependent")] = render_subject(_subject("dependent"))
    elif operation == "delete":
        del candidate[_path("anchor")]
    else:
        del candidate[_path("dependent")]
        candidate[_path("dependent")] = sources[_path("dependent")]
        candidate[_path("new")] = render_subject(_subject("new"))
    final = candidate.snapshot()
    with state.scope():
        advanced = advance_tree_members(state, previous_tree=tree, tree=final)
        result = advance_tree_state(state, tree=final, advanced=advanced)
    assert counts["opened"] == counts["closed"]
    _same(result, build_tree_state(final))
    assert counts["opened"] == counts["closed"]
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    assert not hasattr(tree, "_evaluation")
    assert not hasattr(tree, "_contenders")


def test_changed_region_reads_stay_fixed_as_unrelated_owners_grow(tmp_path, monkeypatch):
    observed = []
    native_steps = []
    for size in (5, 150):
        sources = {
            **_tree(),
            **{_path(f"extra-{i}"): render_subject(_subject(f"extra-{i}")) for i in range(size)},
        }
        directory = tmp_path / str(size)
        directory.mkdir()
        tree, counts, _ = _fixture(directory, sources)
        state = derive_indexed_state(tree)
        counts["blobs"] = 0
        candidate = tree.fork()
        candidate[_path("dependent")] = render_subject(_subject("dependent"))
        final = candidate.snapshot()
        steps = [0]
        connect = sqlite3.connect

        def counted_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)

            def count():
                steps[0] += 1
                return 0

            connection.set_progress_handler(count, 1)
            return connection

        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", counted_connect)
            patch.setattr(
                SnapshotTree,
                "__iter__",
                lambda self: pytest.fail("enumerated accepted source tree"),
            )
            with state.scope():
                advanced = advance_tree_members(state, previous_tree=tree, tree=final)
                advance_tree_state(state, tree=final, advanced=advanced)
        observed.append(counts["blobs"])
        native_steps.append(steps[0])
        assert counts["opened"] == counts["closed"]
    assert observed[0] == observed[1]
    assert observed[0] < 20
    assert native_steps[1] <= native_steps[0] * 1.2, native_steps


def test_detached_state_models_and_merkle_shells_cannot_poison_reuse(tmp_path):
    tree, counts, _ = _fixture(tmp_path, _tree())
    view = derive_indexed_state(tree)
    view.dependencies.states[_path("anchor")].identity.__dict__["name"] = "forged"
    node = view.merkle.nodes[_path("anchor")]
    node.__dict__["member_digest"] = "forged"
    object.__setattr__(node.digest, "value", "0" * 64)
    object.__setattr__(view.merkle.root, "value", "1" * 64)
    object.__setattr__(view.dependencies.edge_tree.root, "value", "2" * 64)
    view.dependencies.__dict__["states"] = {}
    view.__dict__["members"] = {}
    _same(derive_indexed_state(tree), build_tree_state(tree))
    assert counts["opened"] == counts["closed"]


def test_overlay_renamed_owner_suppresses_old_and_new_source_edges(tmp_path):
    from cruxible_core.claims.closure import reverse_pin_closure

    sources = _tree()
    tree, counts, _ = _fixture(tmp_path, sources)
    state = derive_indexed_state(tree)
    candidate = tree.fork()
    del candidate[_path("dependent")]
    candidate[_path("replacement")] = render_subject(_subject("replacement"))
    sealed = candidate.snapshot()
    with state.scope():
        advanced = advance_tree_members(state, previous_tree=tree, tree=sealed)
        result = advance_tree_state(state, tree=sealed, advanced=advanced)
    _same(result, build_tree_state(sealed))
    identity = _subject("anchor").identity
    assert (
        reverse_pin_closure(sealed, root=identity, include=lambda state: True)
        == reverse_pin_closure(dict(sealed), root=identity, include=lambda state: True)
        == ()
    )
    assert counts["opened"] == counts["closed"]


def test_contenders_select_complete_address_and_changed_live_members(tmp_path):
    from cruxible_client.contracts.claims import claim_path, render_claim
    from cruxible_client.contracts.semantic import SemanticAddress
    from cruxible_core.authoring.lowering import _same_predicate_claims
    from tests.test_authoring.test_authoring_disposition_slots import _claim_in_slot

    claims = []
    path = "artifacts/procedures/selected.json"
    for i, node in enumerate(("inspect", "inspect", "different"), 1):
        claim = _claim_in_slot(claim_id="CLM-" + str(i) * 32, qualifier=None)
        claims.append(
            claim.model_copy(
                update={
                    "statement": claim.statement.model_copy(
                        update={"subject": SemanticAddress.procedure_node(path, node)}
                    )
                }
            )
        )
    sources = {claim_path(c.identity.name): render_claim(c) for c in claims}
    tree, counts, _ = _fixture(tmp_path, sources)
    statement = claims[0].statement
    assert tuple(c.identity.name for c in tree.claims_for(statement)) == tuple(
        c.identity.name for c in claims[:2]
    )
    candidate = tree.fork()
    retired = claims[0].model_copy(
        update={"lifecycle": claims[0].lifecycle.model_copy(update={"state": "retired"})}
    )
    candidate[claim_path(retired.identity.name)] = render_claim(retired)
    candidate[claim_path(claims[2].identity.name)] = render_claim(
        claims[2].model_copy(update={"statement": statement})
    )
    sealed = candidate.snapshot()
    assert sealed.claims_for(statement) == _same_predicate_claims(dict(sealed), statement)
    assert len(sealed.claims_for(statement)) == 2
    assert counts["opened"] == counts["closed"]


def test_reverse_closure_keeps_cycles_boundaries_and_first_trigger_order(tmp_path):
    from cruxible_core.claims.closure import reverse_pin_closure

    root = _subject("root")
    left = _subject("left", pins=(_pin_to(root),))
    right = _subject("right", pins=(_pin_to(root),))
    shared = _subject("shared", pins=(_pin_to(left), _pin_to(right)))
    left = _subject("left", pins=(_pin_to(root), _pin_to(shared)))
    sources = {
        _path(s.identity.name.split("/")[-1]): render_subject(s)
        for s in (root, left, right, shared)
    }
    tree, counts, _ = _fixture(tmp_path, sources)
    for excluded in (None, left.identity.qualified):

        def include(state):
            return state.identity.qualified != excluded

        expected = reverse_pin_closure(sources, root=root.identity, include=include)
        actual = reverse_pin_closure(tree, root=root.identity, include=include)
        assert actual == expected
        assert len(actual) == (3 if excluded is None else 2)
    assert counts["opened"] == counts["closed"]


def test_frozen_storage_uses_full_source_oracle_and_closes_handle():
    from cruxible_core.claims.closure import reverse_pin_closure

    tree = SnapshotTree(_tree())
    closed = []
    tree._accepted_reader = lambda: SimpleNamespace(typed=None, close=lambda: closed.append(True))
    assert derive_indexed_state(tree) == build_tree_state(tree)
    assert reverse_pin_closure(
        tree, root=_subject("anchor").identity, include=lambda state: True
    ) == (
        reverse_pin_closure(
            dict(tree), root=_subject("anchor").identity, include=lambda state: True
        )
    )
    assert len(closed) == 2


def test_migration_claim_type_inventory_uses_required_pin_selection(tmp_path, monkeypatch):
    from cruxible_client.contracts.claims import claim_path, render_claim
    from cruxible_core.claims.claim_type_migrations import _current_dependents
    from tests.test_authoring.test_authoring_disposition_slots import _claim_in_slot

    claim = _claim_in_slot(claim_id="CLM-" + "1" * 32, qualifier=None)
    sources = {**_tree(), claim_path(claim.identity.name): render_claim(claim)}
    tree, counts, _ = _fixture(tmp_path, sources)
    expected = _current_dependents(sources, identity=claim.statement.claim_type.qualified)
    with monkeypatch.context() as patch:
        patch.setattr(SnapshotTree, "__iter__", lambda self: pytest.fail("enumerated source tree"))
        result = _current_dependents(tree, identity=claim.statement.claim_type.qualified)
    assert result == expected
    assert counts["opened"] == counts["closed"]
    assert counts["blobs"] == 1


@pytest.mark.parametrize("nested", [False, True])
def test_direct_candidate_derivation_uses_complete_edits_and_parent_proofs(
    tmp_path, monkeypatch, nested
):
    tree, counts, _ = _fixture(tmp_path, _tree())
    derive_indexed_state(tree)
    candidate = tree.fork()
    candidate[_path("dependent")] = render_subject(_subject("dependent"))
    sealed = candidate.snapshot()
    if nested:
        candidate = sealed.fork()
        del candidate[_path("anchor")]
        candidate[_path("new")] = render_subject(_subject("new"))
        sealed = candidate.snapshot()
    counts["blobs"] = 0
    with monkeypatch.context() as patch:
        patch.setattr(SnapshotTree, "__iter__", lambda self: pytest.fail("enumerated source tree"))
        result = derive_indexed_state(sealed)
    assert counts["blobs"] < 20
    assert counts["opened"] == counts["closed"]
    _same(result, build_tree_state(sealed))
    _same(derive_indexed_state(tree), build_tree_state(tree))


def test_request_scope_owns_exact_selection_objects_until_close(tmp_path):
    import gc
    import weakref

    from cruxible_core.indexes.evaluated_state import SelectionSpec

    tree, counts, _ = _fixture(tmp_path, _tree())
    refs = []
    with SelectionSpec(tree._accepted_reader).scope():
        for revision in range(1, 30):
            body = render_subject(_subject("dependent", revision=revision))
            selected = SelectionSpec(tree._accepted_reader, {_path("dependent"): body})
            refs.append(weakref.ref(selected))
            assert selected.call(lambda rows: rows.source_bytes(_path("dependent"))) == body
            del selected
        gc.collect()
        assert all(ref() is not None for ref in refs)
    gc.collect()
    assert all(ref() is None for ref in refs)
    assert counts["opened"] == counts["closed"]


def test_selection_recipe_detaches_changed_bytes_from_input_mapping(tmp_path):
    from cruxible_core.indexes.evaluated_state import SelectionSpec

    tree, counts, _ = _fixture(tmp_path, _tree())
    body = render_subject(_subject("dependent", revision=1))
    edits = {_path("dependent"): body}
    selected = SelectionSpec(tree._accepted_reader, edits)
    edits[_path("dependent")] = render_subject(_subject("dependent", revision=2))
    assert selected.call(lambda rows: rows.source_bytes(_path("dependent"))) == body
    assert counts["opened"] == counts["closed"]


def test_overlay_keeps_authenticated_connection_when_piece_path_is_replaced(tmp_path, monkeypatch):
    import os
    import shutil

    from cruxible_core.indexes.evaluated_state import EvaluationRows

    tree, counts, _ = _fixture(tmp_path, _tree())
    projection = tree._accepted_reader()
    base = EvaluationRows(projection)
    path = _path("anchor")
    expected = base.incoming(path)
    replacement = tmp_path / "forged.sqlite"
    shutil.copyfile(projection.index_path, replacement)
    with sqlite3.connect(replacement) as connection:
        connection.execute("DELETE FROM pins")
    # Simulate replacement immediately after authentication returns; the bound
    # SQLite connection must remain the source of the candidate's accepted rows.
    projection.require_source_authentication = lambda: os.replace(
        replacement, projection.index_path
    )
    try:
        with monkeypatch.context() as patch:
            patch.setattr(
                sqlite3, "connect", lambda *a, **k: pytest.fail("overlay reopened source")
            )
            candidate = base.overlay({})
            assert candidate.incoming(path) == expected
            assert base.incoming(path) == expected
    finally:
        base.close()
    assert counts["opened"] == counts["closed"]
