"""Daemon admission of section bytes and located own words; no workspace I/O."""

import base64
from pathlib import Path

import pytest

from cruxible_client.authoring.doc_writes import write_workspace
from cruxible_client.contracts.claims import claim_path, parse_claim
from cruxible_client.contracts.doc_sections import section_digest
from cruxible_client.contracts.errors import CasError, WriteRefusalError
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.proposal_models import AuthenticatedActor
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


@pytest.mark.parametrize("race", ["conflict", "unrelated", "already_applied"])
def test_history_confirms_only_replacements_that_land_after_local_cas(tmp_path, race):
    instance, _ = seed_write_surface(tmp_path)
    workspace = tmp_path / "prose"
    workspace.mkdir()
    path = workspace / "notes.md"
    path.write_bytes(b"# Reason\noriginal\n# Other\nunrelated\n")
    access = BodyAccessContext(principal_id="reader", can_read_body=True)

    def history():
        return service_playbill_get(
            instance, request=GetRequest(ref="file:doc-notes.md", detail="history"), access=access
        ).history

    def submit(request):
        return service_playbill_write(instance, request=request, caller=caller())

    def replace(text, send=submit):
        return write_workspace(
            WriteRequest(
                because="Edit",
                docs=(
                    ReplaceSection(
                        file="notes.md",
                        section=("Reason",),
                        text=text,
                    ),
                ),
            ),
            workspace=workspace,
            submit=send,
        )

    replace("# Reason\nfirst")
    before_history = history()
    prior = b"# Reason\nfirst\n"
    confirmed = []

    def raced_submit(request):
        outcome = submit(request)
        if request.docs:
            assert history() == before_history
            assert not instance.body_store().metadata(section_digest(prior), access=access).present
            if race == "conflict":
                path.write_bytes(b"# Reason\nconcurrent\n# Other\nunrelated\n")
            elif race == "already_applied":
                path.write_bytes(b"# Reason\nsecond\n# Other\nunrelated\n")
            else:
                path.write_bytes(b"# Reason\nfirst\n# Other\nconcurrent\n")
        else:
            assert b"# Reason\nsecond\n" in path.read_bytes()
            confirmed.extend(request.doc_confirmations)
        return outcome

    result = replace("# Reason\nsecond", raced_submit)
    if race == "unrelated":
        assert result.docs[0].written
        assert len(confirmed) == 1
        assert len(history().revisions) == 2
        assert instance.body_store().read(section_digest(prior), access=access) == prior
    else:
        assert not confirmed
        assert history() == before_history
        assert not instance.body_store().metadata(section_digest(prior), access=access).present
        if race == "conflict":
            assert not result.docs[0].written
            assert result.docs[0].refusal.code == "cruxible.doc.doc_write_conflict"
        else:
            assert result.docs[0].written


def _replacement_observation():
    previous = b"# Reason\nprevious\n"
    raw = _edit(b"# Reason\nnew\n")
    raw.update(
        op="replace",
        before_section=["Reason"],
        before_digest=section_digest(previous),
        before_coordinate=_observation(previous).coordinate,
        before_content_base64=base64.b64encode(previous).decode(),
        end_byte=len(previous),
    )
    return DocEditObservation.model_validate(raw)


def test_confirmation_is_author_bound_durable_and_idempotent(tmp_path):
    from cruxible_core.service.authoring.doc_writes import confirm_doc_edits

    instance, _ = seed_write_surface(tmp_path)
    edit = _replacement_observation()
    admitted = service_playbill_write(
        instance, request=WriteRequest(because="Replace", docs=(edit,)), caller=caller()
    )
    token = admitted.docs[0].confirmation
    assert token is not None
    access = BodyAccessContext(principal_id="reader", can_read_body=True)

    def history():
        return service_playbill_get(
            instance, request=GetRequest(ref="file:repo.notes", detail="history"), access=access
        ).history

    assert history().revisions == ()
    assert not instance.body_store().metadata(edit.before_digest, access=access).present
    with pytest.raises(WriteRefusalError, match="Only the admitted edit's author"):
        confirm_doc_edits(instance, tokens=(token,), actor=AuthenticatedActor(actor_id="other"))
    assert history().revisions == ()
    instance = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    for _ in range(2):
        result = service_playbill_write(
            instance,
            request=WriteRequest(because="File landed", doc_confirmations=(token,)),
            caller=caller(),
        )
        assert result.status == "accepted"
    assert len(history().revisions) == 1
    assert history().revisions[0].actor == caller().actor.actor_id
    assert instance.body_store().read(edit.before_digest, access=access) == base64.b64decode(
        edit.before_content_base64
    )


def test_confirmation_retry_recovers_journal_append_before_pending_receipt(tmp_path, monkeypatch):
    from cruxible_core.service.authoring import doc_writes

    instance, _ = seed_write_surface(tmp_path)
    admitted = service_playbill_write(
        instance,
        request=WriteRequest(because="Replace", docs=(_replacement_observation(),)),
        caller=caller(),
    )
    token = admitted.docs[0].confirmation
    request = WriteRequest(because="File landed", doc_confirmations=(token,))
    save = doc_writes._save_pending

    def crash(path, pending):
        if pending.before_content_base64 is None:
            raise OSError("crash after history append")
        save(path, pending)

    monkeypatch.setattr(doc_writes, "_save_pending", crash)
    with pytest.raises(OSError, match="crash after history append"):
        service_playbill_write(instance, request=request, caller=caller())
    monkeypatch.setattr(doc_writes, "_save_pending", save)
    instance = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert service_playbill_write(instance, request=request, caller=caller()).status == "accepted"
    history = service_playbill_get(
        instance,
        request=GetRequest(ref="file:repo.notes", detail="history"),
        access=BodyAccessContext(principal_id="reader"),
    ).history
    assert len(history.revisions) == 1


def test_daemon_refuses_mismatched_previous_bytes_even_for_an_internal_observation(tmp_path):
    instance, _ = seed_write_surface(tmp_path)
    edit = _replacement_observation().model_copy(
        update={
            "before_content_base64": base64.b64encode(b"forged previous text").decode(),
        }
    )
    request = WriteRequest(because="Invalid", docs=(_replacement_observation(),)).model_copy(
        update={"docs": (edit,)}
    )
    result = service_playbill_write(instance, request=request, caller=caller())
    assert result.refused
    assert result.refusal.code == "cruxible.doc.previous_bytes_mismatch"
    assert result.refusal.field_path == "docs[0]"


def test_mixed_file_batch_confirms_only_the_file_whose_cas_succeeded(tmp_path):
    instance, _ = seed_write_surface(tmp_path)
    workspace = tmp_path / "prose"
    workspace.mkdir()
    good, raced = workspace / "good.md", workspace / "raced.md"
    good.write_bytes(b"# Reason\ngood previous\n")
    raced_previous = b"# Reason\nraced previous\n"
    raced.write_bytes(raced_previous)
    tokens = []

    def submit(request):
        result = service_playbill_write(instance, request=request, caller=caller())
        if request.docs:
            raced.write_bytes(b"# Reason\nconcurrent\n")
        else:
            tokens.extend(request.doc_confirmations)
        return result

    result = write_workspace(
        WriteRequest(
            because="Two files",
            docs=(
                ReplaceSection(file="good.md", section=("Reason",), text="# Reason\ngood new"),
                ReplaceSection(file="raced.md", section=("Reason",), text="# Reason\nraced new"),
            ),
        ),
        workspace=workspace,
        submit=submit,
    )
    assert result.docs[0].written and not result.docs[1].written
    assert tokens == [result.docs[0].confirmation]
    for name, count in (("good", 1), ("raced", 0)):
        history = service_playbill_get(
            instance,
            request=GetRequest(ref=f"file:doc-{name}.md", detail="history"),
            access=BodyAccessContext(principal_id="reader"),
        ).history
        assert len(history.revisions) == count
    assert (
        not instance.body_store()
        .metadata(section_digest(raced_previous), access=BodyAccessContext(principal_id="reader"))
        .present
    )


@pytest.mark.parametrize("unrelated_sources", [2, 200])
def test_unconfirmed_payloads_are_bounded_per_source_without_scanning(
    tmp_path, monkeypatch, unrelated_sources
):
    from uuid import uuid4

    from cruxible_core.service.authoring import doc_writes

    instance, _ = seed_write_surface(tmp_path)
    monkeypatch.setattr(doc_writes, "_PENDING_LIMIT", 3)
    edit = _replacement_observation()

    def admit():
        outcome = service_playbill_write(
            instance, request=WriteRequest(because="Replace", docs=(edit,)), caller=caller()
        )
        assert outcome.status == "accepted"
        return outcome.docs[0].confirmation

    confirmed = admit()
    assert (
        service_playbill_write(
            instance,
            request=WriteRequest(because="Landed", doc_confirmations=(confirmed,)),
            caller=caller(),
        ).status
        == "accepted"
    )
    first = admit()
    root = doc_writes._pending_root(instance)
    pending = doc_writes._load_pending(root / f"{first}.json")
    for index in range(unrelated_sources):
        other = pending.model_copy(
            update={"edit": pending.edit.model_copy(update={"source_id": f"other-{index}"})}
        )
        doc_writes._save_pending(root / f"DCE-{uuid4().hex}.json", other)
    instance = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)

    def no_scan(*args, **kwargs):
        raise AssertionError("a write must not scan the pending namespace")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "glob", no_scan)
        recent = [admit() for _ in range(5)]
        expired = service_playbill_write(
            instance,
            request=WriteRequest(because="Too late", doc_confirmations=(first,)),
            caller=caller(),
        )
        assert expired.refusal.code == "cruxible.doc.confirmation_unknown"
        # An old confirmed receipt remains idempotent even after newer admissions.
        assert (
            service_playbill_write(
                instance,
                request=WriteRequest(because="Retry", doc_confirmations=(confirmed,)),
                caller=caller(),
            ).status
            == "accepted"
        )
        latest = service_playbill_write(
            instance,
            request=WriteRequest(because="Landed", doc_confirmations=(recent[-1],)),
            caller=caller(),
        )
        assert latest.status == "accepted"
    with doc_writes._pending_store(root) as connection:
        rows = connection.execute(
            "SELECT token,unconfirmed FROM pending_edits WHERE source_id=?",
            (edit.source_id,),
        ).fetchall()
        assert set(rows) == {(confirmed, 0), (recent[-3], 1), (recent[-2], 1), (recent[-1], 0)}
        assert (
            connection.execute(
                "SELECT count(*) FROM pending_edits WHERE source_id != ?", (edit.source_id,)
            ).fetchone()[0]
            == unrelated_sources
        )


def test_legacy_pending_files_migrate_once_with_the_same_payload_bound(tmp_path, monkeypatch):
    import os

    from cruxible_core.service.authoring import doc_writes

    instance, _ = seed_write_surface(tmp_path)
    admitted = service_playbill_write(
        instance,
        request=WriteRequest(because="Replace", docs=(_replacement_observation(),)),
        caller=caller(),
    )
    root = doc_writes._pending_root(instance)
    pending = doc_writes._load_pending(root / f"{admitted.docs[0].confirmation}.json")
    # Recreate the old on-disk format, including a compact confirmation receipt.
    (root.parent / "pending.sqlite3").unlink()
    root.mkdir()
    for index in range(6):
        path = root / f"DCE-{index:032x}.json"
        record = pending if index else pending.model_copy(update={"before_content_base64": None})
        path.write_text(record.model_dump_json())
        os.utime(path, (index, index))
    monkeypatch.setattr(doc_writes, "_PENDING_LIMIT", 3)
    assert doc_writes._load_pending(root / f"DCE-{5:032x}.json") == pending
    assert not root.exists()
    with doc_writes._pending_store(root) as connection:
        assert connection.execute("SELECT count(*) FROM pending_edits").fetchone()[0] == 4
    with pytest.raises(FileNotFoundError):
        doc_writes._load_pending(root / f"DCE-{1:032x}.json")
    assert doc_writes._load_pending(root / f"DCE-{0:032x}.json").before_content_base64 is None
