"""PB-B deterministic assembler contract and exact-tree reader tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cruxible_client.contracts.canonical import SemanticRoot, canonical_bytes
from cruxible_client.contracts.errors import (
    ProjectionCoordinateError,
    ProjectionFormatError,
)
from cruxible_client.contracts.types import GitObjectFormat
from cruxible_core.compiler.assembler import ProjectionAssembler
from cruxible_core.compiler.projection_tree import TreeReadLimits
from cruxible_core.indexes.projection import AssemblerRequest, AssemblerResult
from cruxible_core.indexes.sqlite import bind_projection
from tests.core_support._projection_support import (
    MemoryLedger,
    accepted_coordinate,
    subject_bytes,
)
from tests.core_support._support import initialize_local


def _assembler(
    tmp_path: Path,
    repository: MemoryLedger,
    *,
    publication_name: str = "published",
) -> ProjectionAssembler:
    publication = tmp_path / publication_name
    publication.mkdir()
    return ProjectionAssembler(
        repository,
        accepted=accepted_coordinate(repository),
        publication_directory=publication,
    )


def _build(
    tmp_path: Path,
    repository: MemoryLedger,
    *,
    publication_name: str = "published",
) -> tuple[ProjectionAssembler, AssemblerResult]:
    assembler = _assembler(tmp_path, repository, publication_name=publication_name)
    request = assembler.request(
        output_staging_directory=assembler.publication_directory / ".stage-test"
    )
    return assembler, assembler.assemble(request)


def test_request_and_result_are_serializable_and_coordinate_mismatch_precedes_tree_read(
    tmp_path: Path,
) -> None:
    repository = MemoryLedger(
        tmp_path / "repository",
        {"subjects/project.work_item/one.json": subject_bytes("one")},
    )
    assembler = _assembler(tmp_path, repository)
    request = assembler.request(
        output_staging_directory=assembler.publication_directory / ".stage-request"
    )
    assert AssemblerRequest.model_validate_json(request.model_dump_json()) == request

    mismatched = request.model_copy(update={"semantic_root": SemanticRoot("44" * 32).tagged})
    with pytest.raises(ProjectionCoordinateError, match="semantic_root"):
        assembler.assemble(mismatched)
    assert repository.list_calls == 0
    assert repository.read_calls == 0

    result = assembler.assemble(request)
    assert AssemblerResult.model_validate_json(result.model_dump_json()) == result
    assert set(result.instrumentation.phase_nanoseconds) == {
        "git_traversal",
        "parse_normalize",
        "sort",
        "sqlite_load",
        "logical_export_digest",
        "fsync",
        "publication",
    }


def test_projection_ignores_verified_candidate_card_derivatives(tmp_path: Path) -> None:
    artifact = subject_bytes("one")
    repository = MemoryLedger(
        tmp_path / "repository",
        {
            "subjects/project.work_item/one.json": artifact,
            "cards/subjects/project.work_item/one.md": b"# fixture: one\n",
        },
    )

    _assembler_value, result = _build(tmp_path, repository)

    assert result.row_counts["subjects"] == 1


def test_result_refuses_echo_fields_that_differ_from_embedded_manifest(tmp_path: Path) -> None:
    repository = MemoryLedger(
        tmp_path / "repository",
        {"subjects/project.work_item/one.json": subject_bytes("one")},
    )
    _assembler_value, result = _build(tmp_path, repository)
    row_counts = dict(result.row_counts)
    first_table = next(iter(row_counts))
    row_counts[first_table] += 1
    replacements = {
        "git_oid": "0" * len(result.git_oid),
        "semantic_root": "sha256:" + "44" * 32,
        "generation_root": "sha256:" + "55" * 32,
        "logical_digest": "sha256:" + "66" * 32,
        "row_counts": row_counts,
    }

    for field, replacement in replacements.items():
        payload = result.model_dump(mode="json")
        payload[field] = replacement
        with pytest.raises(ValueError, match=field):
            AssemblerResult.model_validate(payload)


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_current_instance_publishes_and_binds_its_exact_genesis_projection(
    tmp_path: Path,
    object_format: GitObjectFormat,
) -> None:
    instance, _owner = initialize_local(tmp_path, object_format=object_format)
    coordinate = instance.accepted_coordinate()
    with instance.bind_accepted_projection(coordinate) as handle:
        assert handle.manifest.git_oid == instance.inspect().head_oid
        assert handle.manifest.semantic_root == instance.inspect().semantic_root
        assert handle.manifest.generation_root == instance.inspect().generation_root
        assert handle.subject("Subject:project.work_item/absent") is None


def test_repeated_scratch_builds_have_identical_logical_export_and_results(
    tmp_path: Path,
) -> None:
    tree = {
        "subjects/project.work_item/two.json": subject_bytes("two"),
        "subjects/project.work_item/one.json": subject_bytes("one"),
    }
    repository = MemoryLedger(tmp_path / "repository", tree)
    first_assembler, first = _build(
        tmp_path,
        repository,
        publication_name="published-first",
    )
    second_assembler, second = _build(
        tmp_path,
        repository,
        publication_name="published-second",
    )

    assert first.logical_digest == second.logical_digest
    assert first.row_counts == second.row_counts
    first_handle = bind_projection(
        Path(first.manifest_path),
        expected=first_assembler.accepted,
    )
    second_handle = bind_projection(
        Path(second.manifest_path),
        expected=second_assembler.accepted,
    )
    assert first_handle.attach_sources(repository, bodies=None, history=None).typed.source(
        "Subject:project.work_item/one"
    ) == second_handle.attach_sources(repository, bodies=None, history=None).typed.source(
        "Subject:project.work_item/one"
    )


def test_sha1_and_sha256_equal_sources_have_distinct_blob_commitments_and_equal_queries(
    tmp_path: Path,
) -> None:
    tree = {"subjects/project.work_item/one.json": subject_bytes("one")}
    sha1 = MemoryLedger(tmp_path / "sha1-repository", tree, object_format="sha1")
    sha256 = MemoryLedger(tmp_path / "sha256-repository", tree, object_format="sha256")
    (tmp_path / "sha1-published").mkdir()
    (tmp_path / "sha256-published").mkdir()
    sha1_assembler = ProjectionAssembler(
        sha1,
        accepted=accepted_coordinate(sha1, generation_byte="55"),
        publication_directory=tmp_path / "sha1-published",
    )
    sha256_assembler = ProjectionAssembler(
        sha256,
        accepted=accepted_coordinate(sha256, generation_byte="66"),
        publication_directory=tmp_path / "sha256-published",
    )
    sha1_result = sha1_assembler.assemble(
        sha1_assembler.request(
            output_staging_directory=sha1_assembler.publication_directory / ".stage-sha1"
        )
    )
    sha256_result = sha256_assembler.assemble(
        sha256_assembler.request(
            output_staging_directory=sha256_assembler.publication_directory / ".stage-sha256"
        )
    )
    assert sha1_result.git_oid != sha256_result.git_oid
    assert sha1_result.generation_root != sha256_result.generation_root
    assert sha1_result.logical_digest != sha256_result.logical_digest  # Exact Git blob IDs differ.
    sha1_handle = bind_projection(Path(sha1_result.manifest_path), expected=sha1_assembler.accepted)
    sha256_handle = bind_projection(
        Path(sha256_result.manifest_path), expected=sha256_assembler.accepted
    )
    assert sha1_handle.attach_sources(sha1, bodies=None, history=None).typed.source(
        "Subject:project.work_item/one"
    ) == sha256_handle.attach_sources(sha256, bodies=None, history=None).typed.source(
        "Subject:project.work_item/one"
    )


@pytest.mark.parametrize(
    ("tree", "modes", "match"),
    [
        ({"hooks/pre-commit": b"malicious"}, {}, "no registered"),
        (
            {"subjects/project.work_item/link.json": b"target"},
            {"subjects/project.work_item/link.json": ("120000", "blob")},
            "symlink",
        ),
        (
            {"subjects/project.work_item/module.json": b"commit"},
            {"subjects/project.work_item/module.json": ("160000", "commit")},
            "submodule",
        ),
        (
            {
                "subjects/project.work_item/lfs.json": (
                    b"version https://git-lfs.github.com/spec/v1\n"
                    b"oid sha256:" + b"0" * 64 + b"\nsize 1\n"
                )
            },
            {},
            "LFS",
        ),
        (
            {
                "subjects/project.work_item/a.json": subject_bytes("duplicate"),
                "subjects/project.work_item/b.json": subject_bytes("duplicate"),
            },
            {},
            "Subject failed strict validation",
        ),
        (
            {
                "subjects/project.work_item/wrong.json": canonical_bytes(
                    {
                        "tag": "unknown-format-v1",
                        "kind": "fixture",
                        "artifact_id": "wrong",
                        "revision": 1,
                        "predecessor_digest": None,
                        "pins": [],
                        "extension_facts": [],
                    }
                )
                + b"\n"
            },
            {},
            "Subject|strict validation",
        ),
    ],
)
def test_tree_reader_refuses_unregistered_nonregular_and_ambiguous_inputs(
    tmp_path: Path,
    tree: dict[str, bytes],
    modes: dict[str, tuple[str, str]],
    match: str,
) -> None:
    repository = MemoryLedger(tmp_path / "repository", tree, modes=modes)
    assembler = _assembler(tmp_path, repository)
    with pytest.raises(ProjectionFormatError, match=match):
        assembler.assemble(
            assembler.request(
                output_staging_directory=assembler.publication_directory / ".stage-refusal"
            )
        )


def test_noncanonical_source_and_resource_limit_are_refused(tmp_path: Path) -> None:
    canonical = subject_bytes("one")
    payload = json.loads(canonical)
    noncanonical = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
    repository = MemoryLedger(
        tmp_path / "repository",
        {"subjects/project.work_item/one.json": noncanonical},
    )
    assembler = _assembler(tmp_path, repository)
    with pytest.raises(ProjectionFormatError, match="Subject failed strict validation"):
        assembler.assemble(
            assembler.request(
                output_staging_directory=assembler.publication_directory / ".stage-yaml"
            )
        )

    limited_repository = MemoryLedger(
        tmp_path / "limited-repository",
        {"subjects/project.work_item/one.json": canonical},
    )
    limited = _assembler(tmp_path, limited_repository, publication_name="limited-published")
    request = limited.request(
        output_staging_directory=limited.publication_directory / ".stage-limited"
    ).model_copy(update={"limits": TreeReadLimits(max_files=1, max_total_bytes=8)})
    with pytest.raises(ProjectionFormatError, match="byte limit"):
        limited.assemble(request)
