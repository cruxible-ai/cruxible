"""Subject and Document history read only selected retained occurrences."""

from collections import Counter

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.documents import (
    DocumentLifecycle,
    document_digest,
    render_document,
)
from cruxible_client.contracts.errors import (
    DocumentNotFoundError,
    ProjectionIntegrityError,
    SubjectNotFoundError,
)
from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_digest
from cruxible_core.service.authoring.documents import (
    PlaybillAcceptedCoordinate,
    service_get_playbill_document,
    service_list_playbill_documents,
    service_playbill_document_history,
)
from cruxible_core.service.claims.subjects import (
    service_get_playbill_subject,
    service_list_playbill_subjects,
    service_playbill_subject_history,
)
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._support import initialize_local
from tests.test_governance.test_subjects import SUBJECT_IDENTITY, SUBJECT_PATH
from tests.test_governance.test_subjects import _shell as subject_shell
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_service.test_playbill_documents import _shell as document_shell


def test_empty_genesis_reads_need_no_history_inventory(tmp_path, monkeypatch):
    instance, _ = initialize_local(tmp_path)

    def no_history():
        raise AssertionError("genesis read scanned accepted history")

    monkeypatch.setattr(instance, "accepted_history", no_history)
    access = BodyAccessContext(principal_id="reader")
    assert service_list_playbill_documents(instance, access=access).documents == ()
    assert service_list_playbill_subjects(instance).subjects == ()
    with pytest.raises(DocumentNotFoundError):
        service_get_playbill_document(instance, identity="document:missing", access=access)
    with pytest.raises(SubjectNotFoundError):
        service_get_playbill_subject(instance, identity=SUBJECT_IDENTITY)


@pytest.mark.parametrize("unrelated", [2, 9])
def test_selected_history_preserves_receipts_and_ignores_unrelated_generations(
    tmp_path, monkeypatch, unrelated
):
    instance, owner = initialize_local(tmp_path)
    initial_subject = subject_shell()
    initial_document = document_shell(instance.store_document_body(b"historical body").digest)
    retired_subject = subject_shell(
        lifecycle=ArtifactLifecycle(
            state="retired", predecessor_digest=subject_digest(initial_subject).tagged
        )
    )
    revised_document = initial_document.model_copy(
        update={
            "title": "Revised design",
            "predecessor_digest": document_digest(initial_document).tagged,
            "lifecycle": DocumentLifecycle(revision=2),
        }
    )
    coordinates = []
    for index, (subject, document) in enumerate(
        ((initial_subject, initial_document), (retired_subject, revised_document))
    ):
        tree = instance.tree_at(instance.accepted_coordinate().git_oid)
        tree.update(
            {
                SUBJECT_PATH: render_subject(subject),
                "documents/design.json": render_document(document),
            }
        )
        _accept_tree(
            instance,
            owner,
            tree,
            timestamp=f"2026-08-20T12:00:0{index}.000000Z",
            proposal_name=f"selected-{index}",
        )
        coordinates.append(PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate()))
    for index in range(unrelated):
        shell = SubjectShell(
            identity=ArtifactIdentity(kind="Subject", name=f"project.other/item-{index}"),
            subject_kind="project.other",
            subject_id=f"item-{index}",
        )
        tree = instance.tree_at(instance.accepted_coordinate().git_oid)
        tree[f"subjects/project.other/item-{index}.json"] = render_subject(shell)
        _accept_tree(
            instance,
            owner,
            tree,
            timestamp=f"2026-08-20T12:01:{index:02d}.000000Z",
            proposal_name=f"unrelated-{index}",
        )
    expected_records = tuple(generation.record for generation in instance.accepted_history()[1:3])
    with instance.accepted_history_reader():
        pass
    reads = []
    original_blob = instance.blob_at

    def selected_blob(oid, path):
        reads.append((oid, path))
        return original_blob(oid, path)

    def no_inventory(*args, **kwargs):
        raise AssertionError("history rebuilt an inventory or unselected tree")

    monkeypatch.setattr(instance, "blob_at", selected_blob)
    for name in ("accepted_history", "tree_at", "immutable_tree_at"):
        monkeypatch.setattr(instance, name, no_inventory)
    documents = service_playbill_document_history(instance, identity="document:design")
    subjects = service_playbill_subject_history(instance, identity=SUBJECT_IDENTITY)
    for result in (documents, subjects):
        assert [entry.sequence for entry in result.entries] == [1, 2]
        assert [entry.coordinate for entry in result.entries] == coordinates
        assert [entry.candidate_digest for entry in result.entries] == [
            record.candidate_digest for record in expected_records
        ]
        assert [entry.changeset_digest for entry in result.entries] == [
            record.changeset_digest for record in expected_records
        ]
    assert [entry.revision for entry in documents.entries] == [1, 2]
    assert [entry.lifecycle_state for entry in subjects.entries] == ["live", "retired"]
    assert documents.entries[1].predecessor_digest == documents.entries[0].envelope_digest
    assert subjects.entries[1].predecessor_digest == subjects.entries[0].artifact_digest
    assert Counter(path for _oid, path in reads) == {
        "documents/design.json": 2,
        SUBJECT_PATH: 2,
        "changesets/cs-00000000000000000001.json": 2,
        "changesets/cs-00000000000000000002.json": 2,
    }
    reads.clear()
    with pytest.raises(DocumentNotFoundError):
        service_playbill_document_history(instance, identity="document:absent")
    with pytest.raises(SubjectNotFoundError):
        service_playbill_subject_history(instance, identity="Subject:project.other/absent")
    assert reads == []
    monkeypatch.setattr(
        instance,
        "blob_at",
        lambda oid, path: (
            render_document(revised_document)
            if path == "documents/design.json"
            else original_blob(oid, path)
        ),
    )
    with pytest.raises(ProjectionIntegrityError, match="source binding differs"):
        service_playbill_document_history(instance, identity="document:design")
    monkeypatch.setattr(
        instance,
        "blob_at",
        lambda oid, path: None if path == SUBJECT_PATH else original_blob(oid, path),
    )
    with pytest.raises(ProjectionIntegrityError, match="source is unavailable"):
        service_playbill_subject_history(instance, identity=SUBJECT_IDENTITY)
