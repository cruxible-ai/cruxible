"""Typed storage keeps canonical Claim values and full semantic addresses exact."""

import hashlib
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.claims import (
    ExactContentClaimObject,
    LiteralClaimObject,
    SubjectClaimObject,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
    parse_claim,
    render_claim,
)
from cruxible_client.contracts.semantic import ContentSpan, SemanticAddress
from cruxible_core.compiler.assembler import PYTHON_REFERENCE_ASSEMBLER
from cruxible_core.compiler.projection_artifacts import parse_projection_tree
from cruxible_core.indexes.sqlite import ProjectionHandle, initialize_projection_database
from cruxible_core.indexes.typed_state import TypedStateReader
from tests.test_claims.test_claim_type_migrations import _accepted_claim_world


@pytest.fixture(scope="module")
def claim_world(tmp_path_factory):
    instance, claim_id, _ = _accepted_claim_world(tmp_path_factory.mktemp("typed-values"))
    path = claim_path(claim_id)
    claim = parse_claim(instance.blob_at(instance.accepted_coordinate().git_oid, path), path=path)
    yield instance, path, claim
    instance._accepted_history_index.close()


def _stored_claim(claim_world, tmp_path, claim):
    instance, path, _ = claim_world
    assembler = instance.projection_assembler()
    request = assembler.request(output_staging_directory=tmp_path / ".stage-values")
    content = render_claim(claim)
    # These variants exercise storage representation, without proposing changes
    # to the fixture's independently governed ClaimType or proof history.
    parsed = parse_projection_tree(
        {path: content},
        registry=assembler.registry,
        artifact_kinds=assembler.artifact_kinds,
        artifact_codec=assembler.artifact_codec,
        bodies=instance.body_store(),
    )
    database = tmp_path / "values.sqlite"
    initialize_projection_database(
        database,
        request=request,
        parsed=parsed,
        sources={path: content},
        assembler_implementation=PYTHON_REFERENCE_ASSEMBLER,
        bodies=instance.body_store(),
    )
    oid = hashlib.new(
        request.git_object_format, b"blob " + str(len(content)).encode("ascii") + b"\0" + content
    ).hexdigest()
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        reader = TypedStateReader(
            connection,
            instance.accepted_coordinate(),
            SimpleNamespace(read_blob={oid: content}.__getitem__),
        )
        restored = reader.source(claim.identity.qualified)
        assert restored == claim
        assert render_claim(restored) == content
        row = dict(connection.execute("SELECT * FROM claims").fetchone())
        assert row["artifact_digest"] == claim_artifact_digest(claim).tagged
        assert row["statement_digest"] == claim_statement_digest(claim.statement).tagged
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='semantic_facts'"
            ).fetchone()[0]
            == 0
        )
    return row


@pytest.mark.parametrize(
    ("value", "literal_type", "text", "boolean", "integer"),
    [
        (None, "null", None, None, None),
        (False, "boolean", None, 0, None),
        (True, "boolean", None, 1, None),
        (-(2**140), "integer", None, None, str(-(2**140))),
        (2**140, "integer", None, None, str(2**140)),
        ("Révision λ", "string", "Révision λ", None, None),
        ([None, True, 2**140, {"signed": -(2**140)}], "array", None, None, None),
        ({"nested": [False, None, {"signed": -(2**140)}]}, "object", None, None, None),
    ],
)
def test_literal_discriminants_and_exact_source_roundtrip(
    claim_world, tmp_path, value, literal_type, text, boolean, integer
):
    original = claim_world[2]
    claim = original.model_copy(
        update={
            "statement": original.statement.model_copy(
                update={"object": LiteralClaimObject(value=value)}
            )
        }
    )
    row = _stored_claim(claim_world, tmp_path, claim)
    assert (
        row["object_kind"],
        row["literal_type"],
        row["literal_text"],
        row["literal_boolean"],
        row["literal_integer_text"],
    ) == ("literal", literal_type, text, boolean, integer)


def test_exact_content_span_beyond_sqlite_integer_range_roundtrips(claim_world, tmp_path):
    original = claim_world[2]
    digest = "sha256:" + "a" * 64
    span = ContentSpan(content_digest=digest, start_byte=2**96, end_byte=2**96 + 513)
    claim = original.model_copy(
        update={
            "statement": original.statement.model_copy(
                update={"object": ExactContentClaimObject(content_digest=digest, span=span)}
            )
        }
    )
    row = _stored_claim(claim_world, tmp_path, claim)
    assert (
        row["object_kind"],
        row["object_content_digest"],
        row["object_span_start_text"],
        row["object_span_end_text"],
        row["literal_type"],
    ) == ("exact_content", digest, str(span.start_byte), str(span.end_byte), None)


def test_subject_and_object_nonwhole_selectors_roundtrip(claim_world, tmp_path):
    original = claim_world[2]
    path = "artifacts/procedures/selected.json"
    subject = SemanticAddress.procedure_node(path, "inspect")
    target = SemanticAddress.procedure_arm(
        path, from_node_id="inspect", arm_label="on_true", target_node_id="finish"
    )
    claim = original.model_copy(
        update={
            "statement": original.statement.model_copy(
                update={"subject": subject, "object": SubjectClaimObject(address=target)}
            )
        }
    )
    row = _stored_claim(claim_world, tmp_path, claim)
    assert (
        row["subject_path"],
        row["subject_selector_scheme"],
        row["subject_selector_value"],
        row["object_kind"],
        row["object_path"],
        row["object_selector_scheme"],
        row["object_selector_value"],
    ) == (
        path,
        "procedure-node-v1",
        "inspect",
        "subject",
        path,
        "procedure-arm-v1",
        "inspect:on_true:finish",
    )


def test_filtered_claim_selection_distinguishes_selectors_before_materialization(
    claim_world, tmp_path
):
    instance, path, original = claim_world
    address = SemanticAddress.procedure_node("procedures/selected.json", "inspect")
    claim = original.model_copy(
        update={"statement": original.statement.model_copy(update={"subject": address})}
    )
    content = render_claim(claim)
    assembler = instance.projection_assembler()
    request = assembler.request(output_staging_directory=tmp_path / ".stage-filter")
    parsed = parse_projection_tree(
        {path: content},
        registry=assembler.registry,
        artifact_kinds=assembler.artifact_kinds,
        artifact_codec=assembler.artifact_codec,
        bodies=instance.body_store(),
    )
    database = tmp_path / "filter.sqlite"
    initialize_projection_database(
        database,
        request=request,
        parsed=parsed,
        sources={path: content},
        assembler_implementation=PYTHON_REFERENCE_ASSEMBLER,
        bodies=instance.body_store(),
    )
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        materialized = []

        def materialize(identity):
            materialized.append(identity)
            return identity

        handle = SimpleNamespace(
            _connection=connection,
            _closed=False,
            claim=materialize,
        )
        for mismatched in (
            SemanticAddress.whole_artifact(address.artifact_path),
            SemanticAddress.procedure_node(address.artifact_path, "finish"),
        ):
            assert ProjectionHandle.list_claims(handle, subject=mismatched) == ()
        assert ProjectionHandle.list_claims(handle, predicate="different.predicate") == ()
        assert materialized == []
        assert ProjectionHandle.list_claims(
            handle,
            subject=address,
            predicate=claim.statement.predicate,
            include_retired=False,
        ) == (claim.identity.qualified,)
        assert materialized == [claim.identity.qualified]
