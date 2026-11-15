"""Document edits and section citations through a real HTTP application fixture."""

import base64
from datetime import UTC, datetime

import pytest
import yaml

from cruxible_client.authoring.files import add_catalog_entry
from cruxible_client.authoring.page_writes import PageWriteError
from cruxible_client.authoring.sdk_types import Duration
from cruxible_client.contracts.errors import WriteRefusalError
from cruxible_client.contracts.write import SectionEvidence
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.core_support._write_support import KIND, cited_captures
from tests.test_server.test_playbill_sdk_write import _sdk
from tests.test_server.test_playbill_write_routes import _seed
from tests.test_service.test_write_verbs import _move_to_v7


def _context(playbill_http, tmp_path):
    client, instance_id, _ = playbill_http
    _seed(client, instance_id)
    cx = _sdk(client, instance_id, tmp_path)
    cx._clock = lambda: datetime.now(UTC)
    return cx, get_playbill_manager().get(instance_id), tmp_path / "sdk-workspace"


def _batch(cx, *, field="status", value="ready"):
    return (
        cx.changes(because="Recorded the rationale")
        .append_section("notes.md", "# Decision\nThe checks passed.\n\n")
        .set(
            f"{KIND}/wi-1",
            field,
            value,
            evidence=SectionEvidence(file="notes.md", section=("Decision",)),
        )
    )


def test_sdk_create_catalog_citation_retry_and_hand_edit_drift(playbill_http, tmp_path):
    cx, instance, workspace = _context(playbill_http, tmp_path)
    result = _batch(cx).write()
    assert result.status == "accepted" and result.docs[0].written, result
    assert result.hint is None
    assert result.changes[0].verdict == "supported"
    # Located own words are one Capture: the own words, sourced at their file.
    assert len(cited_captures(instance, result.changes[0].claim)) == 1
    assert cx.file("notes.md").section("Decision").observation().selector.section.path == (
        "Decision",
    )
    retried = _batch(cx).write()
    assert retried.coordinate == result.coordinate
    assert (workspace / "notes.md").read_text().count("# Decision") == 1
    assert len(yaml.safe_load((workspace / ".cruxible/files.yaml").read_text())["entries"]) == 1
    (workspace / "notes.md").write_text("# Decision\nThe basis was edited by hand.\n")
    page = cx.next(expiring_within=Duration.days(count=1))
    assert any(item.reason == "citation_drifted" for item in page.items), page.items


def test_replace_cas_dry_run_docs_only_and_document_followup(playbill_http, tmp_path):
    cx, _, workspace = _context(playbill_http, tmp_path)
    before = cx.coordinate
    preview = (
        cx.changes(because="Prose only")
        .append_section("notes.md", "# Decision\nold")
        .write(dry_run=True)
    )
    assert preview.status == "would_accept" and not (workspace / "notes.md").exists()
    created = cx.changes(because="Prose only").append_section("notes.md", "# Decision\nold").write()
    assert created.proposal is None and not created.changes and cx.coordinate == before
    from cruxible_client.contracts.write import DOCS_ONLY_HINT

    assert created.hint == DOCS_ONLY_HINT
    assert preview.hint == DOCS_ONLY_HINT
    digest = created.docs[0].after_digest
    replaced = (
        cx.changes(because="Revise prose")
        .replace_section("notes.md", ("Decision",), "# Decision\nnew", expect=digest)
        .write()
    )
    assert replaced.docs[0].written and (workspace / "notes.md").read_text() == "# Decision\nnew\n"
    # Walkthrough W12: a replaced section may be cited, so the write claims nothing.
    assert replaced.hint is None
    source = created.docs[0].source_id
    history = cx.get(f"file:{source}", detail="history").value
    assert history.revisions[0].digest == digest
    client, instance_id, _ = playbill_http
    assert (
        history.revisions[0].actor == client.get(f"/api/v1/{instance_id}/whoami").json()["actor_id"]
    )
    retained = cx.get(f"file:{source}@1", detail="body").value
    assert base64.b64decode(retained.content_base64) == b"# Decision\nold\n\n"

    with pytest.raises(WriteRefusalError) as caught:
        cx.changes(because="Stale").replace_section(
            "notes.md", ("Decision",), "# Decision\nthird", expect=digest
        ).write()
    assert caught.value.error_code == "cruxible.doc.section_changed" and caught.value.repair_line
    add_catalog_entry(
        workspace,
        name=created.docs[0].source_id,
        path=workspace / "notes.md",
        replace=True,
        fields={
            "document_id": "notes",
            "document_kind": "report",
            "title": "Notes",
            "media_type": "text/markdown",
            "governance_scope": ["project"],
        },
    )
    documented = (
        cx.changes(because="Document edit")
        .replace_section("notes.md", ("Decision",), "# Decision\nfinal")
        .write()
    )
    assert documented.docs[0].written and "files propose" in documented.docs[0].next
    assert "--file doc-notes.md" in documented.docs[0].next


def test_captured_only_and_projection_paths_refuse_without_writing(playbill_http, tmp_path):
    cx, instance, workspace = _context(playbill_http, tmp_path)
    _move_to_v7(instance, "measured", actor_id="operator", evidence_requirement="captured")
    with pytest.raises(WriteRefusalError) as caught:
        _batch(cx, field="measured", value=7).write()
    assert caught.value.error_code == "cruxible.write.evidence_required"
    assert not (workspace / "notes.md").exists()
    with pytest.raises(WriteRefusalError, match="protected_path"):
        cx.changes(because="Forbidden").append_section(".cruxible/floor/notes.md", "# A").write()
    (workspace / "notes.md").write_text(
        "# Decision\n<!-- cruxible:block:summary:ref:aaaaaaaaaaaa -->\n"
        "## Protected\nbody\n<!-- /cruxible:block:summary -->\n"
    )
    with pytest.raises(WriteRefusalError, match="projection_edit"):
        cx.changes(because="Forbidden").replace_section(
            "notes.md", ("Decision", "Protected"), "## Protected\nnew"
        ).write()


def test_awaiting_approval_still_writes_file(playbill_http, tmp_path):
    cx, _, workspace = _context(playbill_http, tmp_path)
    result = _batch(cx).write(accept="never")
    assert result.status == "awaiting_approval", result
    assert result.docs[0].written and (workspace / "notes.md").exists()


def test_retry_after_admission_before_file_write_keeps_the_same_claim(
    playbill_http, tmp_path, monkeypatch
):
    cx, _, workspace = _context(playbill_http, tmp_path)

    def conflict(*args, **kwargs):
        raise PageWriteError("simulated post-admission conflict")

    with monkeypatch.context() as patch:
        patch.setattr("cruxible_client.authoring.doc_writes.replace_page_file", conflict)
        first = _batch(cx).write()
    assert first.status == "accepted" and not first.docs[0].written
    assert first.docs[0].refusal.repair and not (workspace / "notes.md").exists()
    second = _batch(cx).write()
    assert second.coordinate == first.coordinate
    assert second.changes[0].claim == first.changes[0].claim
    assert second.docs[0].written and (workspace / "notes.md").exists()


@pytest.mark.parametrize("keyword", ["supported_by", "evidence"])
def test_batch_section_handle_cites_its_operation(playbill_http, tmp_path, keyword):
    cx, _, _ = _context(playbill_http, tmp_path)
    batch = cx.changes(because="Recorded the rationale")
    assert batch.append_section("notes.md", "# Decision\nReady", id="basis") is batch
    batch.set(f"{KIND}/wi-1", "status", "ready", **{keyword: batch.section("basis")})
    result = batch.write()
    assert result.status == "accepted" and result.docs[0].written
    assert result.changes[0].verdict == "supported"
    assert (
        batch.replace_section("notes.md", ("Decision",), "# Decision\nDone", id="revision") is batch
    )


def test_batch_refuses_two_evidence_arguments(playbill_http, tmp_path):
    cx, _, _ = _context(playbill_http, tmp_path)
    batch = cx.changes(because="Reason")
    with pytest.raises(ValueError, match="choose evidence"):
        batch.set(
            f"{KIND}/wi-1",
            "status",
            "ready",
            evidence=batch.section("a"),
            supported_by=batch.section("b"),
        )


@pytest.mark.parametrize("lane", ["subject", "edit", "new", "direct"])
def test_typed_set_carries_new_section_and_catalogues_after_admission(
    playbill_http, tmp_path, lane
):
    cx, instance, workspace = _context(playbill_http, tmp_path)
    w = cx.world()
    evidence = cx.section("decisions.md", ("Decision",), text="# Decision\nThe checks passed.")
    assert not (workspace / "decisions.md").exists()
    if lane == "subject":
        result = w.project.work_item["wi-1"].set(status="ready", evidence=evidence, because="Ready")
    elif lane == "edit":
        result = (
            w.project.work_item["wi-1"]
            .edit()
            .set(status="ready", evidence=evidence)
            .write(because="Ready")
        )
    elif lane == "new":
        draft = w.project.work_item.define("new-doc-item").set(status="ready", evidence=evidence)
        result = draft.write(because="Ready")
        assert draft.address is not None
        assert result.subjects_added
    else:
        result = cx.set(f"{KIND}/wi-1", "status", "ready", evidence=evidence, because="Ready")
    assert result.status == "accepted" and result.docs[0].written, result
    claim = next(change for change in result.changes if change.op == "set")
    assert claim.verdict == "supported"
    assert len(cited_captures(instance, claim.claim)) == 1
    assert (workspace / "decisions.md").read_text() == "# Decision\nThe checks passed.\n\n"
    assert len(yaml.safe_load((workspace / ".cruxible/files.yaml").read_text())["entries"]) == 1


def test_typed_section_names_its_heading_once_and_leaves_a_blank_line(playbill_http, tmp_path):
    """F32: heading= builds the heading line, path and depth; appends stay separated."""

    cx, instance, workspace = _context(playbill_http, tmp_path)
    w = cx.world()
    top = cx.section("notes.md", heading="Notes", body="Why items moved.")
    assert w.project.work_item["wi-1"].set(title="One", evidence=top, because="Titled").docs
    first = cx.section("notes.md", heading="wi-1", under=("Notes",), body="The checks passed.")
    assert first.citation.section == ("Notes", "wi-1")
    ready = w.project.work_item["wi-1"].set(status="ready", evidence=first, because="Ready")
    second = cx.section("notes.md", heading="wi-2", under=("Notes",), body="Waiting on review.\n")
    blocked = w.project.work_item["wi-2"].set(status="blocked", evidence=second, because="Blocked")
    for outcome in (ready, blocked):
        assert outcome.status == "accepted" and outcome.docs[0].written, outcome
        assert outcome.changes[0].verdict == "supported"
    assert (workspace / "notes.md").read_text() == (
        "# Notes\n\nWhy items moved.\n\n"
        "## wi-1\n\nThe checks passed.\n\n"
        "## wi-2\n\nWaiting on review.\n\n"
    )
    # The first item's section kept its bytes when the second was appended after it.
    page = cx.next(expiring_within=Duration.days(count=1))
    assert not any(item.reason == "citation_drifted" for item in page.items), page.items
    replaced = w.project.work_item["wi-2"].set(
        status="ready",
        because="Reviewed",
        evidence=cx.section(
            "notes.md", heading="wi-2", under=("Notes",), body="Reviewed.", replace=True
        ),
    )
    assert replaced.docs[0].written
    assert (workspace / "notes.md").read_text().endswith("## wi-2\n\nReviewed.\n")
    with pytest.raises(TypeError):
        cx.section("notes.md", ("Notes", "x"), heading="x", body="both forms")


def test_sdk_catalogues_an_existing_file_and_cites_it(playbill_http, tmp_path):
    """F23: a file written outside Cruxible is catalogued from the SDK, then cited."""

    cx, _, workspace = _context(playbill_http, tmp_path)
    (workspace / "notes").mkdir()
    (workspace / "notes" / "roadmap.md").write_text("# Roadmap\n\n## wi-1\n\nReady.\n")
    w = cx.world()
    with pytest.raises(WriteRefusalError, match="file_uncatalogued"):
        w.project.work_item["wi-1"].set(
            status="ready",
            because="Ready",
            evidence=SectionEvidence(file="notes/roadmap.md", section=("Roadmap", "wi-1")),
        )
    edit = cx.files.add("roadmap-notes", "notes/roadmap.md")
    assert (edit.status, edit.entry.locator) == ("added", "notes/roadmap.md")
    result = w.project.work_item["wi-1"].set(
        status="ready",
        because="Ready",
        evidence=SectionEvidence(file="notes/roadmap.md", section=("Roadmap", "wi-1")),
    )
    # Cited as file evidence of the catalogued source (its policy decides the verdict).
    assert result.status == "accepted" and result.changes[0].claim, result


def _own_words_size(cx, claim_id):
    """The byte size of the own-words Capture an accepted Claim cites."""

    evidence = cx.get(claim_id, detail="evidence").value
    (own,) = (item for item in evidence.captures if item.origin == "self_source")
    return cx.get(own.capture, detail="summary").value.size


def test_define_and_add_carry_evidence_like_set(playbill_http, tmp_path):
    """F33: the key Claims and added values rest on the section, not on ``because``."""

    cx, _, workspace = _context(playbill_http, tmp_path)
    w = cx.world()
    because = "Loaded."
    note = cx.section("notes.md", heading="wi-f33", body="Why this item exists, at length.")
    draft = w.project.work_item.define("wi-f33", evidence=note, title="F33").add(
        governs=w.project.work_item["wi-1"], evidence=note
    )
    outcome = draft.write(because=because)
    assert outcome.status == "accepted" and len(outcome.docs) == 1, outcome
    section = (workspace / "notes.md").read_bytes()
    assert section == b"# wi-f33\n\nWhy this item exists, at length.\n\n"
    item = cx.world().project.work_item["wi-f33"]
    for field in ("slug", "title", "governs"):
        (view,) = item[field]
        assert _own_words_size(cx, view.claim_id) == len(section), field

    # Located own words: one Capture per Claim, and the section's backlinks list
    # the key Claim beside the added value (a create passes the location too).
    claims = set()
    for field in ("slug", "title", "governs"):
        (view,) = item[field]
        (own,) = cx.get(view.claim_id, detail="evidence").value.captures
        assert (own.origin, own.source) == ("self_source", "doc-notes.md"), (field, own)
        claims.add(view.claim_id.removeprefix("Claim:"))
    linked = {link.source for link in cx.backlinks(file="notes.md", section=("wi-f33",)).links}
    assert linked == claims, linked

    # Without evidence the key Claim rests on the write's because, as before.
    bare = w.project.work_item.define("wi-f33-bare")
    assert bare.write(because=because).status == "accepted"
    (key,) = cx.world().project.work_item["wi-f33-bare"].slug
    assert _own_words_size(cx, key.claim_id) == len(because)

    # An existing section (the write above catalogued its file) is a working selection.
    cited = w.project.work_item.define(
        "wi-f33-cited", evidence=SectionEvidence(file="notes.md", section=("wi-f33",))
    )
    assert cited.write(because=because).status == "accepted"
    (key,) = cx.world().project.work_item["wi-f33-cited"].slug
    captures = cx.get(key.claim_id, detail="evidence").value.captures
    assert [item.source for item in captures] == ["doc-notes.md"]
    # Contract evidence finds a Capture by its Subject, which a new one is not yet.
    from cruxible_client.contracts.write import ContractEvidence

    with pytest.raises(WriteRefusalError) as refused:
        w.project.work_item.define(
            "wi-f33-contract", evidence=ContractEvidence(contract="repo.reports")
        ).write(because=because)
    assert refused.value.error_code == "cruxible.write.create_contract_evidence"


def test_typed_carried_replace_and_stale_expect(playbill_http, tmp_path):
    cx, _, workspace = _context(playbill_http, tmp_path)
    w = cx.world()
    subject = w.project.work_item["wi-1"]
    first = subject.set(
        status="ready",
        because="Ready",
        evidence=cx.section("notes.md", ("Decision",), text="# Decision\nReady"),
    )
    digest = first.docs[0].after_digest
    second = subject.set(
        status="done",
        because="Done",
        evidence=cx.section(
            "notes.md", ("Decision",), text="# Decision\nDone", replace=True, expect=digest
        ),
    )
    assert second.docs[0].written
    assert second.changes[0].revises == first.changes[0].claim
    with pytest.raises(WriteRefusalError, match="section_changed"):
        subject.set(
            status="blocked",
            because="Stale",
            evidence=cx.section(
                "notes.md", ("Decision",), text="# Decision\nBlocked", replace=True, expect=digest
            ),
        )
    assert (workspace / "notes.md").read_text() == "# Decision\nDone\n"


def test_typed_carried_section_reports_post_admission_cas_conflict(
    playbill_http, tmp_path, monkeypatch
):
    cx, _, workspace = _context(playbill_http, tmp_path)
    w = cx.world()
    subject = w.project.work_item["wi-1"]
    subject.set(
        status="ready",
        because="Ready",
        evidence=cx.section("notes.md", ("Decision",), text="# Decision\nReady"),
    )
    original = cx._client.write

    def submit(instance_id, *, request):
        result = original(instance_id, request=request)
        (workspace / "notes.md").write_text("# Decision\nConcurrent edit\n")
        return result

    monkeypatch.setattr(cx._client, "write", submit)
    result = subject.set(
        status="done",
        because="Done",
        evidence=cx.section("notes.md", ("Decision",), text="# Decision\nDone", replace=True),
    )
    assert result.status == "accepted" and not result.docs[0].written
    assert result.docs[0].refusal.code == "cruxible.doc.doc_write_conflict"
    assert (workspace / "notes.md").read_text() == "# Decision\nConcurrent edit\n"


def test_typed_shared_section_evidence_authors_one_operation(playbill_http, tmp_path):
    cx, _, _ = _context(playbill_http, tmp_path)
    w = cx.world()
    evidence = cx.section("notes.md", ("Decision",), text="# Decision\nBoth are ready")
    result = w.write(
        w.project.work_item["wi-1"].edit().set(status="ready", evidence=evidence),
        w.project.work_item["wi-2"].edit().set(status="ready", evidence=evidence),
        because="Ready together",
    )
    assert result.status == "accepted" and len(result.docs) == 1


def test_http_refuses_previous_bytes_that_do_not_match_before_digest(playbill_http, tmp_path):
    from tests.test_service.test_doc_writes import _replacement_observation

    client, instance_id, _ = playbill_http
    _seed(client, instance_id)
    edit = _replacement_observation().model_dump(mode="json")
    edit["before_content_base64"] = base64.b64encode(b"forged previous text").decode()
    result = client.post(
        f"/api/v1/{instance_id}/write",
        json={
            "because": "Invalid previous bytes",
            "docs": [edit],
        },
    )
    assert result.status_code == 422
    assert "previous subtree digest does not reproduce" in result.text
    history = client.post(
        f"/api/v1/{instance_id}/get",
        json={
            "ref": "file:repo.notes",
            "detail": "history",
        },
    )
    assert history.status_code == 200
    assert history.json()["history"]["revisions"] == []


def test_http_file_history_refuses_a_historical_coordinate(playbill_http, tmp_path):
    _, instance, _ = _context(playbill_http, tmp_path)
    old = instance.accepted_coordinate().git_oid
    _move_to_v7(instance, "measured", actor_id="operator", evidence_requirement="captured")
    client, instance_id, _ = playbill_http
    result = client.post(
        f"/api/v1/{instance_id}/get",
        json={"ref": "file:repo.notes", "detail": "history", "at": old},
    )
    assert result.status_code == 400, result.text
    assert result.json()["error_code"] == "cruxible.get.historical_read_unsupported"
