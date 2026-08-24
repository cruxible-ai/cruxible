"""PB-B additive fact registry and canonical logical export tests."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from cruxible_client.contracts.errors import ProjectionFormatError
from cruxible_client.contracts.projection_extensions import (
    ProjectionExtensionRegistry,
    ProjectionFact,
    ProjectionFactDeclaration,
    fixture_extension_registry,
    normalize_projection_value,
)
from cruxible_core.playbill.assembler import ProjectionAssembler
from cruxible_core.storage.playbill_projection import (
    canonical_logical_export,
    physical_file_digest,
    projection_logical_digest,
)
from tests.test_playbill._projection_support import (
    MemoryLedger,
    accepted_coordinate,
    fixture_bytes,
    presentation_bytes,
)


def _build(
    tmp_path: Path,
    tree: dict[str, bytes],
    *,
    name: str,
    oid_seed: str,
):
    repository = MemoryLedger(tmp_path / f"repository-{name}", tree, oid_seed=oid_seed)
    publication = tmp_path / f"published-{name}"
    publication.mkdir()
    assembler = ProjectionAssembler(
        repository,
        accepted=accepted_coordinate(repository, generation_byte=("2" if name == "a" else "3") * 2),
        publication_directory=publication,
    )
    result = assembler.assemble(
        assembler.request(output_staging_directory=publication / f".stage-{name}")
    )
    return assembler, result


def test_projection_value_normalization_is_closed_and_explicit() -> None:
    assert normalize_projection_value(Decimal("1.2300")) == {"$decimal": "1.23"}
    assert normalize_projection_value({"$decimal": "-0.000"}) == {"$decimal": "0"}
    assert normalize_projection_value(datetime(2026, 8, 11, 12, 30, tzinfo=timezone.utc)) == {
        "$timestamp": "2026-08-11T12:30:00Z"
    }
    assert normalize_projection_value({"$timestamp": "2026-08-11T08:30:00-04:00"}) == {
        "$timestamp": "2026-08-11T12:30:00Z"
    }
    assert normalize_projection_value({"$digest": "sha256:" + "a" * 64}) == {
        "$digest": "sha256:" + "a" * 64
    }
    assert normalize_projection_value({"$path": "artifacts/fixtures/one.yaml"}) == {
        "$path": "artifacts/fixtures/one.yaml"
    }
    assert normalize_projection_value({"$name": "fixture.one"}) == {"$name": "fixture.one"}
    assert list(normalize_projection_value({"z": 1, "a": 2})) == ["a", "z"]

    for refused in (1.5, float("nan"), (1, 2), {"$unknown": "value"}):
        with pytest.raises(ProjectionFormatError):
            normalize_projection_value(refused)


@pytest.mark.parametrize(
    ("schema_id", "schema_version", "match"),
    [
        ("playbill.unknown.fact", 1, "undeclared"),
        ("playbill.fixture.fact", 2, "version mismatch"),
        ("playbill.fixture.label", 1, "declared presentation"),
    ],
)
def test_unknown_mismatched_or_misclassified_semantic_fact_refuses(
    tmp_path: Path,
    schema_id: str,
    schema_version: int,
    match: str,
) -> None:
    tree = {
        "artifacts/fixtures/one.yaml": fixture_bytes(
            "one",
            1,
            schema_id=schema_id,
            schema_version=schema_version,
        )
    }
    repository = MemoryLedger(tmp_path / "repository", tree)
    publication = tmp_path / "published"
    publication.mkdir()
    assembler = ProjectionAssembler(
        repository,
        accepted=accepted_coordinate(repository),
        publication_directory=publication,
    )
    with pytest.raises(ProjectionFormatError, match=match):
        assembler.assemble(
            assembler.request(output_staging_directory=publication / ".stage-refusal")
        )


def test_duplicate_extension_fact_refuses_at_registry_boundary() -> None:
    registry = fixture_extension_registry()
    fact = ProjectionFact(
        schema_id="playbill.fixture.fact",
        schema_version=1,
        subject_identity="one",
        fact_key="value",
        value=1,
    )
    with pytest.raises(ProjectionFormatError, match="duplicate projection fact"):
        registry.validate((fact, fact), classification="semantic")


def test_registry_can_carry_additive_versions_but_refuses_undeclared_version() -> None:
    registry = ProjectionExtensionRegistry(
        (
            ProjectionFactDeclaration(
                schema_id="playbill.fixture.versioned",
                schema_version=1,
                classification="semantic",
            ),
            ProjectionFactDeclaration(
                schema_id="playbill.fixture.versioned",
                schema_version=2,
                classification="semantic",
            ),
        )
    )
    version_two = ProjectionFact(
        schema_id="playbill.fixture.versioned",
        schema_version=2,
        subject_identity="one",
        fact_key="value",
        value=None,
    )
    assert registry.validate((version_two,), classification="semantic") == (version_two,)
    with pytest.raises(ProjectionFormatError, match="expected one of 1, 2"):
        registry.validate(
            (version_two.model_copy(update={"schema_version": 3}),),
            classification="semantic",
        )


def test_semantic_fact_change_changes_logical_digest(tmp_path: Path) -> None:
    _assembler_a, result_a = _build(
        tmp_path,
        {"artifacts/fixtures/one.yaml": fixture_bytes("one", {"enabled": True})},
        name="a",
        oid_seed="a",
    )
    _assembler_b, result_b = _build(
        tmp_path,
        {"artifacts/fixtures/one.yaml": fixture_bytes("one", {"enabled": False})},
        name="b",
        oid_seed="b",
    )
    assert result_a.logical_digest != result_b.logical_digest


def test_tree_and_fact_row_iteration_order_do_not_change_logical_digest(tmp_path: Path) -> None:
    first = fixture_bytes("first", 1)
    second = fixture_bytes("second", 2)
    _assembler_a, result_a = _build(
        tmp_path,
        {
            "artifacts/fixtures/second.yaml": second,
            "artifacts/fixtures/first.yaml": first,
        },
        name="a",
        oid_seed="order-a",
    )
    _assembler_b, result_b = _build(
        tmp_path,
        {
            "artifacts/fixtures/first.yaml": first,
            "artifacts/fixtures/second.yaml": second,
        },
        name="b",
        oid_seed="order-b",
    )
    assert result_a.logical_digest == result_b.logical_digest


def test_presentation_cache_add_remove_or_label_change_is_nonlogical(tmp_path: Path) -> None:
    fixture = fixture_bytes("one", {"enabled": True})
    trees = (
        {"artifacts/fixtures/one.yaml": fixture},
        {
            "artifacts/fixtures/one.yaml": fixture,
            "presentation/fixtures/one.json": presentation_bytes("one", "First label"),
        },
        {
            "artifacts/fixtures/one.yaml": fixture,
            "presentation/fixtures/one.json": presentation_bytes("one", "Changed label"),
        },
    )
    results = [
        _build(tmp_path, tree, name=f"label-{index}", oid_seed=f"label-{index}")[1]
        for index, tree in enumerate(trees)
    ]
    assert len({result.logical_digest for result in results}) == 1
    assert [result.row_counts["presentation_facts"] for result in results] == [0, 1, 1]
    assert len({result.manifest.pieces[0].physical_digest for result in results}) == 3


def test_logical_export_is_independent_of_sqlite_page_layout(tmp_path: Path) -> None:
    _assembler, result = _build(
        tmp_path,
        {"artifacts/fixtures/one.yaml": fixture_bytes("one", [1, 2, 3])},
        name="a",
        oid_seed="layout",
    )
    original = Path(result.manifest_path).parent / result.manifest.pieces[0].name
    repacked = tmp_path / "repacked.sqlite"
    shutil.copyfile(original, repacked)
    connection = sqlite3.connect(repacked)
    try:
        connection.execute("PRAGMA page_size=8192")
        connection.execute("VACUUM")
    finally:
        connection.close()

    assert physical_file_digest(original) != physical_file_digest(repacked)
    assert canonical_logical_export(original) == canonical_logical_export(repacked)
    assert projection_logical_digest(original) == projection_logical_digest(repacked)


def test_assembler_implementation_is_nonlogical_build_metadata(tmp_path: Path) -> None:
    _assembler, result = _build(
        tmp_path,
        {"artifacts/fixtures/one.yaml": fixture_bytes("one", {"count": 1})},
        name="a",
        oid_seed="assembler-metadata",
    )
    original = Path(result.manifest_path).parent / result.manifest.pieces[0].name
    alternate = tmp_path / "alternate-assembler.sqlite"
    shutil.copyfile(original, alternate)
    connection = sqlite3.connect(alternate)
    try:
        connection.execute(
            "UPDATE assembler_metadata SET implementation = 'rust-parity' WHERE singleton = 1"
        )
        connection.commit()
    finally:
        connection.close()

    exported = canonical_logical_export(original)
    compiler_table = next(
        table for table in exported["tables"] if table["name"] == "compiler_coordinates"
    )
    assert [column["name"] for column in compiler_table["columns"]] == [
        "singleton",
        "schema_version",
        "compiler_digest",
    ]
    assert all(table["name"] != "assembler_metadata" for table in exported["tables"])
    assert canonical_logical_export(alternate) == exported
    assert projection_logical_digest(alternate) == projection_logical_digest(original)
    assert physical_file_digest(alternate) != physical_file_digest(original)


def test_fact_declarations_constraints_and_rows_are_in_logical_export(tmp_path: Path) -> None:
    _assembler, result = _build(
        tmp_path,
        {"artifacts/fixtures/one.yaml": fixture_bytes("one", {"count": 2})},
        name="a",
        oid_seed="export",
    )
    index = Path(result.manifest_path).parent / result.manifest.pieces[0].name
    exported = canonical_logical_export(index)
    tables = {table["name"]: table for table in exported["tables"]}
    assert tables["projection_fact_schemas"]["rows"] == [
        [
            "playbill.fixture.fact",
            1,
            '["unique(subject_identity,fact_key)"]',
        ]
    ]
    assert tables["semantic_facts"]["rows"] == [
        ["playbill.fixture.fact", 1, "one", "value", '{"count":2}']
    ]
    assert "presentation_facts" not in tables


def test_language_neutral_projection_golden_is_pinned(tmp_path: Path) -> None:
    golden_path = Path(__file__).parents[1] / "goldens" / "playbill" / "projection-v1.json"
    golden = json.loads(golden_path.read_bytes())
    tree = {path: content.encode("utf-8") for path, content in golden["tree"].items()}
    _assembler, result = _build(
        tmp_path,
        tree,
        name="a",
        oid_seed="golden",
    )
    index = Path(result.manifest_path).parent / result.manifest.pieces[0].name
    assert golden["contract"] == "playbill-projection-logical-v1"
    assert result.logical_digest == golden["expected"]["logical_digest"]
    assert canonical_logical_export(index) == golden["expected"]["logical_export"]
