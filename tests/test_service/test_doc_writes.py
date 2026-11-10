"""Daemon admission of section bytes and located own words; no workspace I/O."""

import base64
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.authoring.doc_writes import write_workspace
from cruxible_client.contracts.claims import claim_path, parse_claim
from cruxible_client.contracts.doc_sections import section_digest
from cruxible_client.contracts.errors import CasError, ReadRefusalError, WriteRefusalError
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


def _load_pending(root: Path, token: str):
    from cruxible_core.service.authoring import doc_writes

    with doc_writes._pending_store(root) as connection:
        return doc_writes._load_pending(connection, token)


def _save_pending(root: Path, token: str, pending) -> None:
    from cruxible_core.service.authoring import doc_writes

    with doc_writes._pending_store(root) as connection:
        doc_writes._store_pending(connection, token, pending)


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


_NOTES_ENTRY = """\
  - name: repo.notes
    locator: notes.md
    document_id: notes
    document_kind: note
    title: Notes
    media_type: text/markdown
    compiler_profile: document-v1
    required_tier: governed_write
    governance_scope: [Document:notes]
"""


def test_a_retired_source_contract_refuses_new_captures_at_every_write_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retiring a source's CaptureContract stops new Captures under it.

    An own-words section (its location) and file evidence on the retired
    source both refuse with one typed code; neither builds a Capture under the
    retired version nor reinstalls the source's first version.
    """

    from cruxible_client.authoring.write_evidence import observe_evidence
    from cruxible_client.contracts.capture_contract_inputs import CaptureContractInputRecord
    from cruxible_client.contracts.captures import (
        capture_contract_digest,
        capture_contract_path,
        foreign_source_capture_contract,
        parse_capture_contract,
    )
    from cruxible_client.contracts.write import FileEvidence
    from cruxible_core.authoring import lowering
    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
    from cruxible_core.service.evidence.capture_contracts import service_propose_capture_contract
    from tests.core_support._write_support import report_evidence

    instance, _ = seed_write_surface(tmp_path)
    workspace = tmp_path / "workspace"
    report_evidence(workspace, "three")  # writes the file catalog
    catalog = workspace / ".cruxible" / "files.yaml"
    catalog.write_text(catalog.read_text(encoding="utf-8") + _NOTES_ENTRY, encoding="utf-8")
    (workspace / "notes.md").write_text(
        "# Notes\n\nfive\n\n## Review\n\nLooked again.\n\n## Second\n\nSecond look.\n",
        encoding="utf-8",
    )

    def own_words(heading: str) -> SectionEvidence:
        observed = observe_evidence(
            SectionEvidence(file="notes.md", section=("Notes", heading), own_words=True),
            workspace=workspace,
        )
        assert isinstance(observed, SectionEvidence)
        return observed

    def write(subject: str, field: str, value: object, evidence: Any) -> Any:
        return service_playbill_write(
            instance,
            request=WriteRequest.model_validate(
                {
                    "because": "Recorded in prose",
                    "changes": [
                        {
                            "op": "set",
                            "subject": f"{KIND}/{subject}",
                            "field": field,
                            "value": value,
                            "evidence": evidence.model_dump(mode="json"),
                        }
                    ],
                }
            ),
            caller=caller(),
        )

    # The first own-words location on the source installs its contract.
    installed = write("wi-2", "title", "Looked again", own_words("Review"))
    assert installed.status == "accepted", installed
    name = foreign_source_capture_contract("repo.notes").identity.name
    path = capture_contract_path(name)
    retiring = service_propose_capture_contract(
        instance,
        input=CaptureContractInputRecord(name=name, lifecycle="retired"),
        actor_id="owner",
        proposal_name="retire-notes",
        timestamp="2026-09-29T12:30:00.000000Z",
    )
    assert retiring.proposal.candidate is not None, retiring.proposal.evaluation.diagnostics
    activated = service_activate_playbill_proposal(
        instance, proposal_id=retiring.proposal.admission.proposal_id, activated_by="owner"
    )
    assert activated.status == "accepted"

    def head() -> Any:
        return parse_capture_contract(
            instance.blob_at(instance.accepted_coordinate().git_oid, path), path=path
        )

    retired = head()
    assert retired.lifecycle.state == "retired"
    built_under: list[str] = []
    real_build = lowering.build_working_selection_capture

    def spy(**kwargs: Any) -> Any:
        result = real_build(**kwargs)
        built_under.append(capture_contract_digest(result.contract).tagged)
        return result

    monkeypatch.setattr(lowering, "build_working_selection_capture", spy)
    before = instance.accepted_coordinate()

    located = write("wi-3", "title", "Second look", own_words("Second"))
    cited = write(
        "wi-3",
        "measured",
        5,
        observe_evidence(FileEvidence(file="notes.md#five"), workspace=workspace),
    )

    for outcome, element in ((located, "located_at"), (cited, "source")):
        assert outcome.status == "refused", outcome
        assert outcome.refusal.code == "cruxible.authoring.source_contract_retired", outcome
        assert outcome.refusal.field_path.endswith(element), outcome
    assert built_under == []
    assert capture_contract_digest(retired).tagged not in built_under
    assert instance.accepted_coordinate() == before
    assert head() == retired


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
    refused = confirm_doc_edits(
        instance, tokens=(token,), actor=AuthenticatedActor(actor_id="other")
    )
    assert refused.refused
    assert refused.doc_confirmations[0].refusal.code == "cruxible.doc.confirmation_author"
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
    save = doc_writes._store_pending

    def crash(connection, token, pending):
        if pending.before_content_base64 is None:
            raise OSError("crash after history append")
        save(connection, token, pending)

    monkeypatch.setattr(doc_writes, "_store_pending", crash)
    with pytest.raises(OSError, match="crash after history append"):
        service_playbill_write(instance, request=request, caller=caller())
    monkeypatch.setattr(doc_writes, "_store_pending", save)
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


@pytest.mark.parametrize("count", [65, 500])
def test_one_request_retains_and_confirms_every_replacement(tmp_path, monkeypatch, count):
    from cruxible_core.service.authoring import doc_writes

    instance, _ = seed_write_surface(tmp_path)
    admitted = service_playbill_write(
        instance,
        request=WriteRequest(
            because="Many replacements", docs=(_replacement_observation(),) * count
        ),
        caller=caller(),
    )
    assert admitted.status == "accepted"
    tokens = tuple(doc.confirmation for doc in admitted.docs)
    assert len(set(tokens)) == count and None not in tokens
    opens = []
    store = doc_writes._pending_store

    def counted(root):
        opens.append(root)
        return store(root)

    monkeypatch.setattr(doc_writes, "_pending_store", counted)
    confirmed = service_playbill_write(
        instance, request=WriteRequest(because="Landed", doc_confirmations=tokens), caller=caller()
    )
    monkeypatch.setattr(doc_writes, "_pending_store", store)
    # The pending store opens once per confirmation batch, never per token.
    assert len(opens) == 1
    assert confirmed.status == "accepted", confirmed
    assert [result.confirmation for result in confirmed.doc_confirmations] == list(tokens)
    assert all(result.refusal is None for result in confirmed.doc_confirmations)
    revisions = []
    cursor = None
    while True:
        page = service_playbill_get(
            instance,
            request=GetRequest(ref="file:repo.notes", detail="history", limit=200, cursor=cursor),
            access=BodyAccessContext(principal_id="reader"),
        )
        revisions.extend(page.history.revisions)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(revisions) == count
    assert {entry.value.object["write_id"] for entry in revisions} == set(tokens)


def test_other_authors_cannot_evict_a_request_inside_its_confirmation_window(tmp_path):
    from datetime import timedelta

    from cruxible_core.service.authoring import doc_writes

    instance, _ = seed_write_surface(tmp_path)
    now = instance.recording_clock()
    instance.recording_clock = lambda: now
    request = WriteRequest(because="First author", docs=(_replacement_observation(),) * 65)
    admitted = service_playbill_write(instance, request=request, caller=caller())
    tokens = tuple(doc.confirmation for doc in admitted.docs)
    # Exercise admission's pending-store layer with several other authenticated
    # author identities, without installing unrelated principals in this fixture.
    for index in range(3):
        doc_writes.prepare_doc_edits(
            instance,
            request=WriteRequest(because="Other author", docs=(_replacement_observation(),) * 500),
            outcome=admitted,
            actor=AuthenticatedActor(actor_id=f"other-{index}"),
        )
    now += timedelta(seconds=doc_writes._CONFIRMATION_WINDOW_SECONDS - 1)
    instance = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    instance.recording_clock = lambda: now
    confirmed = service_playbill_write(
        instance, request=WriteRequest(because="Landed", doc_confirmations=tokens), caller=caller()
    )
    assert confirmed.status == "accepted", confirmed
    assert len(confirmed.doc_confirmations) == 65


@pytest.mark.parametrize("unrelated_sources", [2, 200])
def test_expired_cleanup_is_source_local_and_unknown_tokens_do_not_strand_valid_ones(
    tmp_path, monkeypatch, unrelated_sources
):
    from datetime import timedelta
    from uuid import uuid4

    from cruxible_core.service.authoring import doc_writes

    instance, _ = seed_write_surface(tmp_path)
    now = instance.recording_clock()
    instance.recording_clock = lambda: now
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
    expired = [admit() for _ in range(3)]
    root = doc_writes._pending_root(instance)
    pending = _load_pending(root, expired[0])
    for index in range(unrelated_sources):
        other = pending.model_copy(
            update={"edit": pending.edit.model_copy(update={"source_id": f"other-{index}"})}
        )
        _save_pending(root, f"DCE-{uuid4().hex}", other)
    now += timedelta(seconds=doc_writes._CONFIRMATION_WINDOW_SECONDS + 1)

    def no_scan(*args, **kwargs):
        raise AssertionError("a write must not scan the pending namespace")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "glob", no_scan)
        recent = admit()
        batch = (expired[0], recent, *expired[1:])
        result = service_playbill_write(
            instance,
            request=WriteRequest(because="Landed", doc_confirmations=batch),
            caller=caller(),
        )
        assert result.refused
        assert [item.confirmation for item in result.doc_confirmations] == list(batch)
        assert result.doc_confirmations[1].refusal is None
        assert [item.refusal.code for item in result.doc_confirmations if item.refusal] == [
            "cruxible.doc.confirmation_unknown"
        ] * 3
        assert "do not resend unknown tokens" in result.refusal.repair
        # Confirmed receipts remain idempotent after the admission window closes.
        assert (
            service_playbill_write(
                instance,
                request=WriteRequest(because="Retry", doc_confirmations=(confirmed, recent)),
                caller=caller(),
            ).status
            == "accepted"
        )
    with doc_writes._pending_store(root) as connection:
        assert set(
            connection.execute(
                "SELECT token,unconfirmed FROM pending_edits WHERE source_id=?", (edit.source_id,)
            )
        ) == {(confirmed, 0), (recent, 0)}
        assert (
            connection.execute(
                "SELECT count(*) FROM pending_edits WHERE source_id != ?", (edit.source_id,)
            ).fetchone()[0]
            == unrelated_sources
        )


def test_token_past_its_deadline_confirms_while_its_row_exists(tmp_path):
    from datetime import timedelta

    from cruxible_core.service.authoring import doc_writes

    instance, _ = seed_write_surface(tmp_path)
    admitted = service_playbill_write(
        instance,
        request=WriteRequest(because="Replace", docs=(_replacement_observation(),)),
        caller=caller(),
    )
    token = admitted.docs[0].confirmation
    now = instance.recording_clock() + timedelta(
        seconds=doc_writes._CONFIRMATION_WINDOW_SECONDS + 1
    )
    instance.recording_clock = lambda: now
    request = WriteRequest(because="Late", doc_confirmations=(token,))
    result = service_playbill_write(instance, request=request, caller=caller())
    # Expiry is garbage collection only; the clock never decides a confirmation.
    assert result.status == "accepted", result
    assert result.doc_confirmations[0].refusal is None
    revisions = service_playbill_get(
        instance,
        request=GetRequest(ref="file:repo.notes", detail="history"),
        access=BodyAccessContext(principal_id="reader"),
    ).history.revisions
    assert [entry.value.object["write_id"] for entry in revisions] == [token]
    assert service_playbill_write(instance, request=request, caller=caller()).status == "accepted"


def test_legacy_pending_files_migrate_once_without_evicting_in_flight_tokens(tmp_path):
    from cruxible_core.service.authoring import doc_writes

    instance, _ = seed_write_surface(tmp_path)
    admitted = service_playbill_write(
        instance,
        request=WriteRequest(because="Replace", docs=(_replacement_observation(),)),
        caller=caller(),
    )
    root = doc_writes._pending_root(instance)
    pending = _load_pending(root, admitted.docs[0].confirmation)
    (root.parent / "pending.sqlite3").unlink()
    root.mkdir()
    for index in range(65):
        record = pending if index else pending.model_copy(update={"before_content_base64": None})
        (root / f"DCE-{index:032x}.json").write_text(record.model_dump_json())
    assert _load_pending(root, f"DCE-{1:032x}") == pending
    assert not root.exists()
    with doc_writes._pending_store(root) as connection:
        assert connection.execute("SELECT count(*) FROM pending_edits").fetchone()[0] == 65
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    assert _load_pending(root, f"DCE-{0:032x}").before_content_base64 is None


@pytest.fixture
def non_utc_host_zone(monkeypatch):
    """A naive stamp read as host-local time would shift a deadline by hours."""
    import time

    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize("naive", [False, True])
def test_count_bounded_sqlite_store_migrates_deadlines_from_issuance_once(
    tmp_path, non_utc_host_zone, naive
):
    import json
    import sqlite3

    from cruxible_core.service.authoring import doc_writes

    instance, _ = seed_write_surface(tmp_path)
    admitted = service_playbill_write(
        instance,
        request=WriteRequest(because="Replace", docs=(_replacement_observation(),)),
        caller=caller(),
    )
    token = admitted.docs[0].confirmation
    root = doc_writes._pending_root(instance)
    pending = _load_pending(root, token)
    stored = json.loads(pending.model_dump_json())
    if naive:
        stored["actor"]["timestamp"] = stored["actor"]["timestamp"].removesuffix("+00:00")
        assert "+" not in stored["actor"]["timestamp"]
    database = root.parent / "pending.sqlite3"
    database.unlink()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE pending_edits (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
            "token TEXT NOT NULL UNIQUE, source_id TEXT NOT NULL, "
            "unconfirmed INTEGER NOT NULL, payload TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO pending_edits(token,source_id,unconfirmed,payload) VALUES(?,?,1,?)",
            (token, pending.edit.source_id, json.dumps(stored)),
        )
        connection.execute("PRAGMA user_version=1")
    for _ in range(2):
        assert _load_pending(root, token) == pending
        with doc_writes._pending_store(root) as connection:
            deadline = connection.execute("SELECT expires_at FROM pending_edits").fetchone()[0]
            assert (
                abs(
                    deadline
                    - pending.actor.timestamp.timestamp()
                    - doc_writes._CONFIRMATION_WINDOW_SECONDS
                )
                < 0.001
            )
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_client_exposes_confirmation_failures_without_repeating_unknown_tokens(tmp_path):
    from cruxible_core.service.authoring import doc_writes

    instance, _ = seed_write_surface(tmp_path)
    workspace = tmp_path / "client-prose"
    workspace.mkdir()
    path = workspace / "notes.md"
    path.write_bytes(b"# Reason\nold\n")

    def submit(request):
        if request.doc_confirmations:
            # Garbage collection removed the pending row before the client confirmed.
            with doc_writes._pending_store(doc_writes._pending_root(instance)) as connection:
                connection.execute("DELETE FROM pending_edits")
        return service_playbill_write(instance, request=request, caller=caller())

    with pytest.raises(WriteRefusalError) as caught:
        write_workspace(
            WriteRequest(
                because="Landed after cleanup",
                docs=(ReplaceSection(file="notes.md", section=("Reason",), text="# Reason\nnew"),),
            ),
            workspace=workspace,
            submit=submit,
        )
    assert path.read_bytes() == b"# Reason\nnew\n"
    assert (
        caught.value.outcome.doc_confirmations[0].refusal.code
        == "cruxible.doc.confirmation_unknown"
    )
    assert "do not resend" in caught.value.repair_line
    assert "Retry write with doc_confirmations" not in caught.value.repair_line


@pytest.mark.parametrize(
    ("ref", "detail"), [("file:repo.notes", "history"), ("file:repo.notes@1", "body")]
)
def test_file_history_refuses_an_older_coordinate_instead_of_showing_later_edits(
    tmp_path, ref, detail
):
    instance, _ = seed_write_surface(tmp_path)
    old = instance.accepted_coordinate()
    _move_to_v7(instance, "measured", evidence_requirement="captured")
    admitted = service_playbill_write(
        instance,
        request=WriteRequest(because="Later edit", docs=(_replacement_observation(),)),
        caller=caller(),
    )
    assert (
        service_playbill_write(
            instance,
            request=WriteRequest(
                because="Landed", doc_confirmations=(admitted.docs[0].confirmation,)
            ),
            caller=caller(),
        ).status
        == "accepted"
    )
    access = BodyAccessContext(principal_id="reader", can_read_body=True)
    with pytest.raises(ReadRefusalError) as caught:
        service_playbill_get(
            instance, request=GetRequest(ref=ref, detail=detail, at=old.git_oid), access=access
        )
    assert caught.value.error_code == "cruxible.get.historical_read_unsupported"
    assert caught.value.field_path == "at"
    assert caught.value.repair_line
    current = service_playbill_get(
        instance,
        request=GetRequest(ref=ref, detail=detail, at=instance.accepted_coordinate().git_oid),
        access=access,
    )
    if detail == "history":
        assert len(current.history.revisions) == 1
    else:
        assert base64.b64decode(current.body.content_base64) == b"# Reason\nprevious\n"


@pytest.mark.parametrize("same_token", [False, True])
def test_concurrent_confirmations_return_a_typed_retry_and_never_duplicate_history(
    tmp_path, monkeypatch, same_token
):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from cruxible_core.exhaust.backends import LocalJournalBackend

    instance, _ = seed_write_surface(tmp_path)
    admitted = service_playbill_write(
        instance,
        request=WriteRequest(because="Concurrent edits", docs=(_replacement_observation(),) * 2),
        caller=caller(),
    )
    tokens = tuple(doc.confirmation for doc in admitted.docs)
    attempted = (tokens[0], tokens[0] if same_token else tokens[1])
    gate = threading.Barrier(2, timeout=10)
    activate = LocalJournalBackend.activate_writer

    def race_activation(self, *args, **kwargs):
        # Both callers have read the same head before either may activate/append.
        gate.wait()
        return activate(self, *args, **kwargs)

    def confirm(token):
        return service_playbill_write(
            instance,
            request=WriteRequest(because="Landed", doc_confirmations=(token,)),
            caller=caller(),
        )

    with monkeypatch.context() as patch:
        patch.setattr(LocalJournalBackend, "activate_writer", race_activation)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(confirm, attempted))
    if same_token:
        # The loser's conflict re-probes and finds the winner's record of it.
        assert [result.status for result in results] == ["accepted", "accepted"]
    else:
        assert sorted(result.status for result in results) == ["accepted", "refused"]
        failed = next(result for result in results if result.refused).doc_confirmations[0]
        assert failed.refusal.code == "cruxible.doc.confirmation_conflict"
        assert failed.confirmation in failed.refusal.repair
        assert "Retry write with doc_confirmations" in failed.refusal.repair
    # A retry after either interleaving records only missing events.
    for token in attempted:
        assert confirm(token).status == "accepted"
    history = service_playbill_get(
        instance,
        request=GetRequest(ref="file:repo.notes", detail="history"),
        access=BodyAccessContext(principal_id="reader"),
    ).history
    assert len(history.revisions) == (1 if same_token else 2)
    assert {entry.value.object["write_id"] for entry in history.revisions} == set(attempted)


def test_same_token_confirmed_after_an_empty_probe_is_never_appended_twice(tmp_path, monkeypatch):
    from cruxible_core.exhaust.backends import LocalJournalBackend
    from cruxible_core.service.authoring import doc_writes

    instance, _ = seed_write_surface(tmp_path)
    admitted = service_playbill_write(
        instance,
        request=WriteRequest(because="Replace", docs=(_replacement_observation(),)),
        caller=caller(),
    )
    token = admitted.docs[0].confirmation
    select = LocalJournalBackend.select_records
    raced = False

    def interleave(self, stream, **filters):
        nonlocal raced
        rows = select(self, stream, **filters)
        if not raced and filters.get("event_kind") == "doc_edit" and filters.get("payload_digest"):
            assert rows == ()
            raced = True
            # Another confirmation of the same token lands after this empty
            # probe, while no pending-store lock is held.
            other = doc_writes.confirm_doc_edits(instance, tokens=(token,), actor=caller().actor)
            assert other.status == "accepted", other
        return rows

    monkeypatch.setattr(LocalJournalBackend, "select_records", interleave)
    first = service_playbill_write(
        instance,
        request=WriteRequest(because="Landed", doc_confirmations=(token,)),
        caller=caller(),
    )
    monkeypatch.setattr(LocalJournalBackend, "select_records", select)
    assert raced
    assert first.status == "accepted", first
    revisions = service_playbill_get(
        instance,
        request=GetRequest(ref="file:repo.notes", detail="history"),
        access=BodyAccessContext(principal_id="reader"),
    ).history.revisions
    assert [entry.value.object["write_id"] for entry in revisions] == [token]
