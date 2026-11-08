"""Daemon admission of section bytes and located own words; no workspace I/O."""

import base64
from pathlib import Path

import pytest

from cruxible_client.authoring.doc_writes import write_workspace
from cruxible_client.contracts.claims import claim_path, parse_claim
from cruxible_client.contracts.doc_sections import section_digest
from cruxible_client.contracts.errors import CasError
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.write import (
    AppendSection,
    DocEditObservation,
    ReplaceSection,
    SectionEvidence,
    SetChange,
    WriteRequest,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._write_support import KIND, caller, seed_write_surface
from tests.test_client.test_doc_write_contracts import _edit, _observation
from tests.test_service.test_write_verbs import _move_to_v7


def test_located_own_words_adds_copy_and_retries_without_generation(tmp_path: Path) -> None:
    instance, _ = seed_write_surface(tmp_path)
    request = WriteRequest(
        because="Recorded in prose",
        changes=(
            SetChange(
                subject=f"{KIND}/wi-1",
                field="status",
                value="ready",
                evidence=SectionEvidence(
                    file="notes.md", section=("Reason",), own_words=True, observation=_observation()
                ),
            ),
        ),
        docs=(DocEditObservation.model_validate(_edit()),),
    )
    first = service_playbill_write(instance, request=request, caller=caller())
    assert first.status == "accepted", first
    change = first.changes[0]
    assert change.verdict == "supported"
    path = claim_path(change.claim)
    tree = instance.immutable_tree_at(instance.accepted_coordinate().git_oid)
    claim = parse_claim(tree[path], path=path)
    assert {(c.role, c.origin) for c in claim.backing.citations} == {
        ("copy", "self_source"),
        ("copy", "independent"),
    }
    retried = service_playbill_write(instance, request=request, caller=caller())
    assert retried.status == "accepted", retried
    assert retried.coordinate == first.coordinate


def test_captured_only_refuses_authored_section_and_docs_only_has_no_generation(
    tmp_path: Path,
) -> None:
    instance, _ = seed_write_surface(tmp_path)
    _move_to_v7(instance, "measured", evidence_requirement="captured")
    before = instance.accepted_coordinate()
    request = WriteRequest(
        because="Reason",
        changes=(
            SetChange(
                subject=f"{KIND}/wi-1",
                field="measured",
                value=7,
                evidence=SectionEvidence(
                    file="notes.md", section=("Reason",), own_words=True, observation=_observation()
                ),
            ),
        ),
    )
    result = service_playbill_write(instance, request=request, caller=caller())
    assert result.refusal.code == "cruxible.write.evidence_required", result
    docs = service_playbill_write(
        instance,
        request=WriteRequest(
            because="Prose only",
            docs=(DocEditObservation.model_validate(_edit()),),
        ),
        caller=caller(),
    )
    assert docs.status == "accepted" and docs.proposal is None and not docs.changes
    assert instance.accepted_coordinate() == before


def test_daemon_refuses_unobserved_doc_operations(tmp_path: Path) -> None:
    instance, _ = seed_write_surface(tmp_path)
    result = service_playbill_write(
        instance,
        request=WriteRequest.model_validate(
            {
                "because": "Prose",
                "docs": [{"op": "append", "file": "arbitrary.md", "text": "# A"}],
            }
        ),
        caller=caller(),
    )
    assert result.refusal.code == "cruxible.write.doc_unobserved"
    assert result.refusal.repair


def test_docs_only_hint_is_advisory_and_absent_with_any_changes(tmp_path):
    from cruxible_client.contracts.write import DOCS_ONLY_HINT

    instance, _ = seed_write_surface(tmp_path)
    edit = DocEditObservation.model_validate(_edit())
    for dry_run in (False, True):
        result = service_playbill_write(
            instance,
            request=WriteRequest(
                because="Note",
                docs=(edit,),
                dry_run=dry_run,
            ),
            caller=caller(),
        )
        assert result.hint == DOCS_ONLY_HINT
        assert not result.refused and not result.warnings
    result = service_playbill_write(
        instance,
        request=WriteRequest(
            because="Decision",
            docs=(edit,),
            changes=(SetChange(subject=f"{KIND}/wi-1", field="status", value="ready"),),
        ),
        caller=caller(),
    )
    assert result.hint is None and "hint" not in result.model_dump(mode="json")


@pytest.mark.parametrize("unrelated_bytes", [10, 100_000])
def test_overwrite_retains_only_previous_subtree_and_exposes_author_in_history(
    tmp_path, monkeypatch, unrelated_bytes
):
    (tmp_path / "instance").mkdir()
    instance, _ = seed_write_surface(tmp_path / "instance")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    previous = b"## Before\nold\r\n### Nested\nkept too\n"
    path = workspace / "notes.md"
    path.write_bytes(b"# Parent\n" + previous + b"# Other\n" + b"x" * unrelated_bytes + b"\n")
    stores = []
    bodies = instance.body_store()
    original_store = bodies.store

    def store(content, **kwargs):
        stores.append(content)
        return original_store(content, **kwargs)

    monkeypatch.setattr(bodies, "store", store)
    result = write_workspace(
        WriteRequest(
            because="Tidy",
            docs=(
                ReplaceSection(
                    file="notes.md",
                    section=("Parent", "Before"),
                    text="## After\nnew",
                ),
            ),
        ),
        workspace=workspace,
        submit=lambda request: service_playbill_write(instance, request=request, caller=caller()),
    )
    assert result.docs[0].written
    assert previous in stores
    assert sum(map(len, stores)) < 3000  # independent of unrelated file bytes
    assert (
        bodies.read(
            section_digest(previous),
            access=BodyAccessContext(
                principal_id="owner",
                can_read_body=True,
            ),
        )
        == previous
    )
    source = result.docs[0].source_id

    def get(ref, detail="history", **kwargs):
        return service_playbill_get(
            instance,
            request=GetRequest(
                ref=ref,
                detail=detail,
                **kwargs,
            ),
            access=BodyAccessContext(principal_id="owner", can_read_body=True),
        )

    instance = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    history = get(f"file:{source}").history
    assert len(history.revisions) == 1
    entry = history.revisions[0]
    assert entry.actor == caller().actor.actor_id
    assert entry.digest == section_digest(previous)
    assert entry.value.object["before_section"] == ["Parent", "Before"]
    assert entry.value.object["section"] == ["Parent", "After"]
    assert entry.value.object["after_digest"] == result.docs[0].after_digest
    recovered = get(f"file:{source}@1", "body").body
    assert base64.b64decode(recovered.content_base64) == previous
    for op in (
        AppendSection(file="notes.md", text="# Appended\nwords"),
        ReplaceSection(file="notes.md", section=("Parent", "After"), text="## After\nnew"),
    ):
        write_workspace(
            WriteRequest(because="Again", docs=(op,)),
            workspace=workspace,
            submit=lambda request: service_playbill_write(
                instance, request=request, caller=caller()
            ),
        )
    assert len(get(f"file:{source}").history.revisions) == 1


def test_overwrite_history_pages_sources_and_skips_preview_and_refusal(tmp_path):
    instance, _ = seed_write_surface(tmp_path)
    workspace = tmp_path / "notes"
    workspace.mkdir()
    path = workspace / "one.md"
    path.write_bytes(b"# A\noriginal\n")

    def write(text, **kwargs):
        return write_workspace(
            WriteRequest(
                because="Edit",
                docs=(
                    ReplaceSection(
                        file="one.md",
                        section=("A",),
                        text=text,
                    ),
                ),
                **kwargs,
            ),
            workspace=workspace,
            submit=lambda request: service_playbill_write(
                instance, request=request, caller=caller()
            ),
        )

    def history(ref="file:doc-one.md", **kwargs):
        return service_playbill_get(
            instance,
            request=GetRequest(
                ref=ref,
                detail="history",
                **kwargs,
            ),
            access=BodyAccessContext(principal_id="reader"),
        )

    write("# A\npreview", dry_run=True)
    assert history().history.revisions == ()
    result = write_workspace(
        WriteRequest(
            because="Refused",
            docs=(
                ReplaceSection(
                    file="one.md",
                    section=("A",),
                    text="# A\nrefused",
                ),
            ),
            changes=(SetChange(subject=f"{KIND}/wi-1", field="no_such_field", value="ready"),),
        ),
        workspace=workspace,
        submit=lambda request: service_playbill_write(instance, request=request, caller=caller()),
    )
    assert result.refused and not result.docs[0].written
    assert history().history.revisions == ()
    write("# A\nfirst")
    write("# A\nsecond")
    page = history(limit=1)
    assert page.truncated and page.history.revisions[0].revision == 2
    write("# A\nthird")
    continued = history(limit=1, cursor=page.next_cursor)
    assert not continued.truncated
    assert continued.history.revisions[0].revision == 1
    assert history("file:another").history.revisions == ()
    with pytest.raises(CasError, match="denied"):
        service_playbill_get(
            instance,
            request=GetRequest(ref="file:doc-one.md@1", detail="body"),
            access=BodyAccessContext(principal_id="reader"),
        )
