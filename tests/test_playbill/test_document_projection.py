"""PB-C Document compiler facts, protected reads, and provisional coordinates."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.playbill.assembler import ProjectionAssembler
from cruxible_core.playbill.candidates import SemanticCandidate, candidate_digest
from cruxible_core.playbill.canonical import canonical_bytes, manifest_root, semantic_diff
from cruxible_core.playbill.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.playbill.compiler import PB_B_COMPILER, PB_C_COMPILER
from cruxible_core.playbill.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentLink,
    DocumentPin,
    DocumentShell,
    render_document,
)
from cruxible_core.playbill.errors import ProjectionCoordinateError, ProjectionFormatError
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import ProvisionalProjectionCoordinate
from cruxible_core.playbill.projection_documents import (
    DocumentProjectionView,
    compile_provisional_document_projection,
)
from cruxible_core.storage.playbill_projection import bind_projection
from tests.test_playbill._projection_support import MemoryLedger, accepted_coordinate
from tests.test_playbill._support import initialize_local

DOCUMENT_PATH = "documents/playbill-design.yaml"
TIMESTAMP = "2026-08-11T12:30:00.000000Z"


def _store(tmp_path: Path) -> ContentAddressedBodyStore:
    root = tmp_path / "cas"
    root.mkdir()
    return ContentAddressedBodyStore(root)


def _shell(body_digest: str) -> DocumentShell:
    return DocumentShell(
        identity="document:playbill-design",
        document_kind="design",
        title="Playbill design",
        media_type="text/markdown",
        body_digest=body_digest,
        links=(
            DocumentLink(
                relation="implements",
                target_identity="document:implementation-program",
            ),
        ),
        pins=(
            DocumentPin(
                role="reference",
                target_identity="document:ratified-design",
                target_digest="sha256:" + "88" * 32,
            ),
        ),
        authority=DocumentAuthority(
            required_tier="graph_write",
            approval_roles=("owner", "reviewer"),
        ),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )


def _candidate_coordinate(
    repository: MemoryLedger,
    tree: dict[str, bytes],
) -> ProvisionalProjectionCoordinate:
    canonical = accepted_coordinate(repository).model_copy(update={"compiler": PB_C_COMPILER})
    difference, scope = semantic_diff({}, tree)
    candidate = SemanticCandidate(
        parent_semantic_root=canonical.semantic_root,
        candidate_manifest_root=manifest_root(tree).tagged,
        semantic_diff_digest=difference.tagged,
        scope=scope,
        timestamp=TIMESTAMP,
    )
    return ProvisionalProjectionCoordinate(
        canonical=canonical,
        candidate=candidate,
        candidate_digest=candidate_digest(candidate).tagged,
    )


def _fact(view: DocumentProjectionView, schema_id: str) -> object:
    return next(fact.value for fact in view.facts if fact.schema_id == schema_id)


def test_document_compiler_emits_reproducible_facts_and_protected_exact_span(
    tmp_path: Path,
) -> None:
    bodies = _store(tmp_path)
    body = "# Café\n\nExact bytes.\n".encode()
    metadata = bodies.store(body)
    tree = {DOCUMENT_PATH: render_document(_shell(metadata.digest))}
    repository = MemoryLedger(tmp_path / "repository", tree)
    publication = tmp_path / "published"
    publication.mkdir()
    accepted = accepted_coordinate(repository).model_copy(update={"compiler": PB_C_COMPILER})
    assembler = ProjectionAssembler(
        repository,
        accepted=accepted,
        publication_directory=publication,
        bodies=bodies,
    )
    result = assembler.assemble(
        assembler.request(output_staging_directory=publication / ".stage-document")
    )

    with bind_projection(Path(result.manifest_path), expected=accepted) as handle:
        denied = handle.document(
            "document:playbill-design",
            access=BodyAccessContext(principal_id="reader"),
        )
        allowed = handle.document(
            "document:playbill-design",
            access=BodyAccessContext(principal_id="owner", can_read_body=True),
        )
        assert denied is not None and allowed is not None
        assert denied.coordinate_kind == "canonical"
        assert denied.coordinate == accepted
        assert handle.list_documents(access=BodyAccessContext(principal_id="reader")) == (denied,)
        assert "playbill.document.source_mapping" not in {fact.schema_id for fact in denied.facts}
        mapping = _fact(allowed, "playbill.document.source_mapping")
        assert mapping == {
            "spans": [
                {
                    "content_digest": metadata.digest,
                    "end_byte": len(body),
                    "start_byte": 0,
                    "tag": "playbill-content-span-v1",
                }
            ],
            "subject": {
                "artifact_path": DOCUMENT_PATH,
                "selector": {"scheme": "artifact-v1", "value": ""},
                "tag": "playbill-semantic-address-v1",
            },
            "tag": "playbill-source-mapping-v1",
        }
        subject = _fact(allowed, "playbill.document.subject")
        assert subject["body_digest"] == {"$digest": metadata.digest}  # type: ignore[index]
        assert subject["input_digest"]["$digest"].startswith("sha256:")  # type: ignore[index]
        references = _fact(allowed, "playbill.document.references")
        assert references["links"][0]["target_identity"] == (  # type: ignore[index]
            "document:implementation-program"
        )

        sqlite_bytes = handle.index_path.read_bytes()
        assert body not in sqlite_bytes
        denied_payload = denied.model_dump(mode="json")
        assert "start_byte" not in str(denied_payload)
        assert "end_byte" not in str(denied_payload)


def test_canonical_query_ignores_candidate_projection_and_provisional_read_names_both_coordinates(
    tmp_path: Path,
) -> None:
    bodies = _store(tmp_path)
    body = bodies.store(b"candidate only")
    canonical_repository = MemoryLedger(tmp_path / "repository", {})
    canonical = accepted_coordinate(canonical_repository).model_copy(
        update={"compiler": PB_C_COMPILER}
    )
    publication = tmp_path / "published"
    publication.mkdir()
    assembler = ProjectionAssembler(
        canonical_repository,
        accepted=canonical,
        publication_directory=publication,
        bodies=bodies,
    )
    result = assembler.assemble(
        assembler.request(output_staging_directory=publication / ".stage-canonical")
    )
    tree = {DOCUMENT_PATH: render_document(_shell(body.digest))}
    provisional_coordinate = _candidate_coordinate(canonical_repository, tree)
    provisional = compile_provisional_document_projection(
        tree,
        coordinate=provisional_coordinate,
        bodies=bodies,
    )

    access = BodyAccessContext(principal_id="owner", can_read_body=True)
    with bind_projection(Path(result.manifest_path), expected=canonical) as handle:
        assert handle.document("document:playbill-design", access=access) is None
        assert handle.list_documents(access=access) == ()

    view = provisional.document("document:playbill-design", access=access)
    assert view is not None
    assert view.coordinate_kind == "provisional"
    assert view.coordinate == provisional_coordinate
    assert provisional_coordinate.canonical == canonical
    assert provisional_coordinate.candidate.candidate_manifest_root == manifest_root(tree).tagged

    altered = {DOCUMENT_PATH: tree[DOCUMENT_PATH] + b" "}
    with pytest.raises(ProjectionCoordinateError, match="candidate manifest"):
        compile_provisional_document_projection(
            altered,
            coordinate=provisional_coordinate,
            bodies=bodies,
        )


def test_whole_document_subject_survives_line_movement_while_byte_span_changes(
    tmp_path: Path,
) -> None:
    bodies = _store(tmp_path)
    repository = MemoryLedger(tmp_path / "repository", {})
    views: list[DocumentProjectionView] = []
    for index, content in enumerate(("# Café\n", "\n\n# Café\n")):
        metadata = bodies.store(content.encode())
        tree = {DOCUMENT_PATH: render_document(_shell(metadata.digest))}
        coordinate = _candidate_coordinate(repository, tree)
        projection = compile_provisional_document_projection(
            tree,
            coordinate=coordinate,
            bodies=bodies,
        )
        view = projection.document(
            "document:playbill-design",
            access=BodyAccessContext(
                principal_id=f"reader-{index}",
                can_read_body=True,
            ),
        )
        assert view is not None
        views.append(view)

    first_subject = _fact(views[0], "playbill.document.subject")["address"]  # type: ignore[index]
    second_subject = _fact(views[1], "playbill.document.subject")["address"]  # type: ignore[index]
    assert first_subject == second_subject
    first_mapping = _fact(views[0], "playbill.document.source_mapping")
    second_mapping = _fact(views[1], "playbill.document.source_mapping")
    assert first_mapping != second_mapping
    assert first_mapping["spans"][0]["end_byte"] == len("# Café\n".encode())  # type: ignore[index]
    assert second_mapping["spans"][0]["end_byte"] == len("\n\n# Café\n".encode())  # type: ignore[index]


def test_compiler_coordinate_checkpoint_preserves_pb_b_registry(tmp_path: Path) -> None:
    assert PB_B_COMPILER != PB_C_COMPILER
    bodies = _store(tmp_path)
    body = bodies.store(b"body")
    tree = {DOCUMENT_PATH: render_document(_shell(body.digest))}
    repository = MemoryLedger(tmp_path / "repository", tree)
    publication = tmp_path / "published"
    publication.mkdir()
    assembler = ProjectionAssembler(
        repository,
        accepted=accepted_coordinate(repository).model_copy(update={"compiler": PB_B_COMPILER}),
        publication_directory=publication,
        bodies=bodies,
    )
    with pytest.raises(ProjectionFormatError, match="undeclared projection fact"):
        assembler.assemble(
            assembler.request(output_staging_directory=publication / ".stage-old-compiler")
        )


def test_existing_pb_b_descriptor_coordinate_still_reopens(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    descriptor = instance.descriptor.model_copy(update={"compiler": PB_B_COMPILER})
    descriptor_path = instance.root / "instance.json"
    descriptor_path.write_bytes(canonical_bytes(descriptor.model_dump(mode="json")) + b"\n")

    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.descriptor.compiler == PB_B_COMPILER
