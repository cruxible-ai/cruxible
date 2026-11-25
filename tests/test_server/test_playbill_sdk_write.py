"""The SDK write verbs over a daemon: cx.set, cx.retire, cx.changes(because=...), World writes."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client.authoring.sdk import ChangeSetDraft, Cruxible, WriteBatch
from cruxible_client.contracts.errors import WriteRefusalError
from cruxible_client.contracts.write import SlotRef, WriteOutcome
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.core_support._write_support import (
    KIND,
    REPORTS,
    cited_captures,
    report_evidence,
    seed_write_vocabulary,
)

WI1 = f"{KIND}/wi-1"


def _sdk(client: TestClient, instance_id: str, tmp_path: Path, name: str = "sdk") -> Cruxible:
    transport = CruxibleClient(base_url="http://testserver")
    transport._client._client = client  # type: ignore[attr-defined]  # noqa: SLF001
    workspace = tmp_path / f"{name}-workspace"
    workspace.mkdir()
    return Cruxible._from_client(  # noqa: SLF001
        transport,
        instance_id=instance_id,
        workspace=workspace,
        clock=lambda: datetime(2026, 9, 29, 12, tzinfo=UTC),
    )


@pytest.fixture
def pb(playbill_http: tuple[TestClient, str, Path], tmp_path: Path) -> Cruxible:
    client, instance_id, _key = playbill_http
    actor = client.get(f"/api/v1/{instance_id}/whoami").json()["actor_id"]
    seed_write_vocabulary(get_playbill_manager().get(instance_id), actor_id=actor)
    return _sdk(client, instance_id, tmp_path)


def test_set_accepts_revises_and_advances_the_connection(pb: Cruxible) -> None:
    first = pb.set(WI1, "status", "ready", because="Checked.")
    assert isinstance(first, WriteOutcome) and first.status == "accepted"
    assert first.accepted_coordinate is not None
    assert pb.coordinate.git_oid == first.accepted_coordinate.git_oid

    second = pb.set(WI1, "status", "done", because="Shipped.")
    assert second.changes[0].revises == first.changes[0].claim
    assert second.changes[0].verdict == "supported"


def test_a_stale_set_refuses_then_setting_again_replaces_the_value_it_showed(
    pb: Cruxible, playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    first = pb.set(WI1, "status", "ready", because="Checked.")
    client, instance_id, _key = playbill_http
    _sdk(client, instance_id, tmp_path, name="other").set(
        WI1, "status", "blocked", because="Someone else."
    )
    with pytest.raises(WriteRefusalError) as caught:
        pb.set(WI1, "status", "done", because="Stale.")
    assert caught.value.error_code == "cruxible.write.slot_changed"
    assert "'blocked'" in str(caught.value)
    again = pb.set(WI1, "status", "done", because="Seen it; replacing.")
    assert again.status == "accepted"
    assert again.changes[0].before == "blocked"
    assert again.changes[0].revises == first.changes[0].claim


def test_a_refusal_raises_a_typed_error_carrying_the_outcome(pb: Cruxible) -> None:
    with pytest.raises(WriteRefusalError) as caught:
        pb.set(WI1, "status", "dne", because="x")
    error = caught.value
    assert error.error_code == "cruxible.write.value_not_member"
    assert error.candidates == ("done",)
    assert "blocked, done, ready" in str(error)
    assert isinstance(error.outcome, WriteOutcome) and error.outcome.status == "refused"


def test_a_batch_writes_two_adds_and_a_retire_as_one_change_set(pb: Cruxible) -> None:
    status = pb.set(WI1, "status", "ready", because="x").changes[0].claim
    batch = pb.changes(because="Linked and cleaned.")
    assert isinstance(batch, WriteBatch)
    outcome = (
        batch.add(WI1, "governs", f"{KIND}/wi-2")
        .add(WI1, "governs", f"{KIND}/wi-3")
        .retire(SlotRef(subject=WI1, field="status"))
        .write()
    )
    assert outcome.status == "accepted", outcome
    assert [item.op for item in outcome.changes] == ["add", "add", "retire"]
    assert outcome.changes[2].claim == status
    assert "WriteBatch(because='Linked and cleaned.'" in repr(batch)
    # The full authoring changeset is still what changes(rationale=...) opens.
    assert isinstance(pb.changes(rationale="Author by hand."), ChangeSetDraft)
    assert isinstance(pb.changes(), ChangeSetDraft)


def test_expect_compares_by_value_on_set_retire_and_a_batch(
    pb: Cruxible, playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    pb.set(WI1, "status", "ready", because="Checked.", expect=[])
    client, instance_id, _key = playbill_http
    _sdk(client, instance_id, tmp_path, name="other").set(
        WI1, "status", "blocked", because="Someone else."
    )
    with pytest.raises(WriteRefusalError) as caught:
        pb.set(WI1, "status", "done", because="Stale.", expect="ready", at=None)
    assert caught.value.error_code == "cruxible.write.slot_changed"
    assert "holds 'blocked'" in str(caught.value)
    replaced = pb.set(WI1, "status", "done", because="Seen.", expect="blocked", at=None)
    assert replaced.status == "accepted" and replaced.changes[0].before == "blocked"

    linked = (
        pb.changes(because="Linked.")
        .add(WI1, "governs", f"{KIND}/wi-2", expect_absent=True)
        .write()
    )
    assert linked.status == "accepted", linked
    with pytest.raises(WriteRefusalError) as present:
        pb.changes(because="Again.").add(WI1, "governs", f"{KIND}/wi-2", expect_absent=True).write()
    assert present.value.error_code == "cruxible.write.value_already_present"
    ended = pb.retire(SlotRef(subject=WI1, field="status"), because="Withdrawn.", expect="done")
    assert ended.status == "accepted", ended
    batch = pb.changes(because="Unlinked.").retire(
        SlotRef(subject=WI1, field="governs"), expect=[f"{KIND}/wi-2"]
    )
    assert batch.changes[0].expect == (f"{KIND}/wi-2",)  # type: ignore[union-attr]
    assert batch.write().status == "accepted"


def test_a_batch_names_its_subject_once(pb: Cruxible) -> None:
    pb.set(WI1, "title", "Old", because="x")
    batch = pb.changes(because="Triaged.", subject=WI1)
    batch.set("status", "ready").add("governs", f"{KIND}/wi-2")
    batch.set("status", "done", subject=f"{KIND}/wi-3").retire(SlotRef(field="title"))
    assert "subject='project.work_item/wi-1'" in repr(batch)
    outcome = batch.write()
    assert outcome.status == "accepted", outcome
    assert [item.subject for item in outcome.changes] == [WI1, WI1, f"{KIND}/wi-3", WI1]
    with pytest.raises(TypeError, match="names its subject once"):
        batch.set(WI1, "status", "ready", subject=WI1)  # type: ignore[call-overload]
    with pytest.raises(WriteRefusalError) as caught:
        pb.changes(because="No subject.").set("status", "ready").write()
    assert caught.value.error_code == "cruxible.write.subject_required"
    with pytest.raises(ValueError, match="write batch"):
        pb.changes(rationale="x", subject=WI1)  # type: ignore[call-overload]


def test_capture_handles_and_contract_evidence_from_the_sdk(
    pb: Cruxible, playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    from cruxible_client.contracts.write import (
        CaptureEvidence,
        ContractEvidence,
        FileEvidence,
        capture_handle,
    )

    workspace = pb._workspace  # noqa: SLF001
    report_evidence(workspace, "Count: 3")
    first = pb.set(
        WI1, "measured", 3, because="Counted.", evidence=FileEvidence(file="reports.md#Count: 3")
    )
    assert first.changes[0].verdict == "supported", first
    _client, instance_id, _key = playbill_http
    instance = get_playbill_manager().get(instance_id)
    (digest,) = cited_captures(instance, first.changes[0].claim or "")
    handle = capture_handle(digest)
    cited = pb.set(
        f"{KIND}/wi-2",
        "measured",
        3,
        because="Same report.",
        evidence=CaptureEvidence(capture=handle),
    )
    assert cited.changes[0].capture == handle
    batch = pb.changes(because="Counted.", subject=WI1).add(
        "labels", "counted", evidence=ContractEvidence(contract=REPORTS.identity.name)
    )
    assert batch.write().changes[0].capture == handle
    with pytest.raises(WriteRefusalError) as caught:
        pb.set(
            f"{KIND}/wi-3",
            "measured",
            3,
            because="x",
            evidence=CaptureEvidence(capture="CAP-" + "0" * 12),
        )
    assert caught.value.error_code == "cruxible.write.capture_not_found"


def test_retire_dry_run_and_proposal_activate(pb: Cruxible) -> None:
    claim = pb.set(WI1, "title", "Old", because="x").changes[0].claim
    assert claim is not None
    preview = pb.retire(claim, because="Gone.", dry_run=True)
    assert preview.status == "would_accept"
    proposed = pb.retire(claim, because="Gone.", accept="never")
    assert proposed.status == "awaiting_approval" and proposed.proposal is not None
    proposal = pb.proposal(proposed.proposal.proposal_id)
    assert repr(proposal) == f"Proposal({proposed.proposal.proposal_id!r})"
    listed = pb.proposals(status="open")
    assert proposal.proposal_id in [entry.proposal_id for entry in listed.entries]
    receipt = proposal.activate()
    assert receipt.status == "accepted"
    assert proposal.proposal_id not in [
        entry.proposal_id for entry in pb.proposals(status="open").entries
    ]


def test_world_writes_keep_references_valid_after_their_own_write(
    pb: Cruxible, playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    world = pb.world()
    item = world.project.work_item["wi-1"]
    assert item.set(status="ready", because="Checked.").status == "accepted"
    # The same reference, and the same World, after its own write: no refusal.
    again = item.set(status="done", title="Tidy the CLI", because="Shipped.")
    assert again.status == "accepted", again
    assert [change.field for change in again.changes] == ["status", "title"]
    assert item.add(governs=world.project.work_item["wi-2"], because="Linked.").status == "accepted"
    with pytest.raises(WriteRefusalError) as present:
        item.add(governs=world.project.work_item["wi-2"], because="Again.", expect_absent=True)
    assert present.value.error_code == "cruxible.write.value_already_present"
    assert (
        item.add(governs=world.project.work_item["wi-2"], because="Linked.").changes[0].already_live
    )
    # A World pins its own basis, so a preview's next step takes no ``at`` (gapb-26).
    preview = item.retire("title", because="Untitled.", dry_run=True)
    assert preview.status == "would_accept"
    assert preview.next == "Call it again with dry_run=False"
    assert item.retire("title", because="Untitled.").status == "accepted"
    # A name the World does not know refuses before the wire.
    with pytest.raises(AttributeError, match="stauts"):
        item.set(stauts="done", because="x")

    # Someone else moves the slot: the World's next write to it refuses.
    client, instance_id, _key = playbill_http
    other = _sdk(client, instance_id, tmp_path, name="other")
    other.set(WI1, "status", "blocked", because="Someone else.")
    with pytest.raises(WriteRefusalError) as caught:
        item.set(status="ready", because="Stale.")
    assert caught.value.error_code == "cruxible.write.slot_changed"
    # An untouched slot still writes from the same World.
    assert world.project.work_item["wi-3"].set(status="ready", because="x").status == "accepted"


def test_the_world_stub_types_set_with_enum_literals(pb: Cruxible) -> None:
    stub = pb.world().stub()
    ast.parse(stub)
    assert "def set(" in stub and "def add(" in stub and "def retire(" in stub
    assert "evidence: Evidence | AuthoredSection | None" in stub
    add = stub[stub.index("def add(") :]
    assert "expect_absent: bool = ...," in add[: add.index(") -> WriteOutcome")]
    assert "status: Literal['blocked', 'done', 'ready'] = ...," in stub
    # A many-valued field takes one value or a sequence of them.
    assert (
        "governs: str | SubjectRef | WorldNewSubject | "
        "Sequence[str | SubjectRef | WorldNewSubject] = ...,"
    ) in stub
    assert "ruling: str = ...," in stub


def test_incoming_reads_the_edges_pointing_at_a_subject_over_http_and_the_sdk(
    pb: Cruxible, playbill_http: tuple[TestClient, str, Path]
) -> None:
    from cruxible_client.contracts.get_reads import GetIncoming

    target = f"{KIND}/wi-2"
    outcome = (
        pb.changes(because="Both govern wi-2.")
        .add(WI1, "governs", target)
        .add(f"{KIND}/wi-3", "governs", target)
        .write()
    )
    assert outcome.status == "accepted", outcome
    client, instance_id, _key = playbill_http
    url = f"/api/v1/{instance_id}/get"
    first = client.post(url, json={"ref": target, "detail": "incoming", "limit": 1})
    assert first.status_code == 200, first.text
    page = first.json()
    assert page["truncated"] is True and len(page["incoming"]["claims"]) == 1
    rest = client.post(
        url, json={"ref": target, "detail": "incoming", "cursor": page["next_cursor"]}
    )
    assert rest.status_code == 200, rest.text
    assert rest.json().get("next_cursor") is None
    assert len(rest.json()["incoming"]["claims"]) == 1

    # The SDK reads every page, pinned to the first page's coordinate.
    whole = pb.get(target, detail="incoming").value
    assert isinstance(whole, GetIncoming)
    assert sorted(row.subject for row in whole.claims) == [WI1, f"{KIND}/wi-3"]
    assert {row.predicate for row in whole.claims} == {f"{KIND}.governs"}


def test_cx_add_adds_one_value_beside_the_others_like_set(pb: Cruxible) -> None:
    first = pb.add(WI1, "governs", f"{KIND}/wi-2", because="Linked.")
    assert first.status == "accepted", first
    assert [item.op for item in first.changes] == ["add"]
    second = pb.add(WI1, "governs", f"{KIND}/wi-3", because="Linked too.")
    assert second.status == "accepted"
    again = pb.add(WI1, "governs", f"{KIND}/wi-2", because="Linked.")
    assert again.changes[0].already_live, again
    with pytest.raises(WriteRefusalError) as caught:
        pb.add(WI1, "governs", f"{KIND}/wi-2", because="x", expect_absent=True)
    assert caught.value.error_code == "cruxible.write.value_already_present"


# -- based_on a Claim the same World write makes -------------------------------------


def _based_on_pins(instance_id: str, claim: str | None) -> dict[str, str]:
    from cruxible_client.contracts.claims import (
        BASED_ON_CLAIM_PIN_ROLE,
        claim_path,
        parse_claim,
    )

    assert claim is not None
    instance = get_playbill_manager().get(instance_id)
    path = claim_path(claim.removeprefix("Claim:"))
    content = instance.blob_at(instance.accepted_coordinate().git_oid, path)
    assert content is not None
    return {
        pin.target.name: pin.artifact_digest
        for pin in parse_claim(content, path=path).pins
        if pin.role == BASED_ON_CLAIM_PIN_ROLE
    }


def _digest(instance_id: str, claim: str | None) -> str:
    from cruxible_client.contracts.claims import claim_artifact_digest, claim_path, parse_claim

    assert claim is not None
    instance = get_playbill_manager().get(instance_id)
    path = claim_path(claim.removeprefix("Claim:"))
    content = instance.blob_at(instance.accepted_coordinate().git_oid, path)
    assert content is not None
    return claim_artifact_digest(parse_claim(content, path=path)).tagged


def _field_claim(outcome: WriteOutcome, field: str) -> str | None:
    (claim,) = [item.claim for item in outcome.changes if item.field == field]
    return None if claim is None else claim.removeprefix("Claim:")


@pytest.mark.parametrize("reverse", [False, True])
def test_a_draft_pin_names_the_claim_a_sibling_draft_writes(
    pb: Cruxible, playbill_http: tuple[TestClient, str, Path], reverse: bool
) -> None:
    _client, instance_id, _key = playbill_http
    world = pb.world()
    observed = world.project.work_item.define("wi-9").set(status="ready")
    judged = world.project.work_item.define("wi-10").set(
        title="Ready to ship", based_on=observed.pin("status")
    )
    drafts = (judged, observed) if reverse else (observed, judged)
    outcome = world.write(*drafts, because="Judged from the sibling's status.")
    assert outcome.status == "accepted", outcome
    status, title = _field_claim(outcome, "status"), _field_claim(outcome, "title")
    assert _based_on_pins(instance_id, title) == {status: _digest(instance_id, status)}
    assert observed.address == f"{KIND}/wi-9"
    # Once landed, a pin names address#field: the field's one live Claim.
    follow = world.project.work_item["wi-2"].set(
        title="After", based_on=observed.pin("status"), because="Rests on it."
    )
    assert follow.status == "accepted", follow
    assert set(_based_on_pins(instance_id, _field_claim(follow, "title"))) == {status}


def test_a_draft_named_only_by_a_pin_is_created_in_the_same_write(
    pb: Cruxible, playbill_http: tuple[TestClient, str, Path]
) -> None:
    _client, instance_id, _key = playbill_http
    world = pb.world()
    observed = world.project.work_item.define("wi-9").set(status="ready")
    judged = world.project.work_item.define("wi-10").set(
        title="Ready", based_on=[observed.pin("status")]
    )
    outcome = world.write(judged, because="Judged.")
    assert outcome.status == "accepted", outcome
    assert observed.address == f"{KIND}/wi-9" and judged.address == f"{KIND}/wi-10"
    status = _field_claim(outcome, "status")
    assert set(_based_on_pins(instance_id, _field_claim(outcome, "title"))) == {status}


def test_an_edit_pin_names_the_claim_a_sibling_edit_revises(
    pb: Cruxible, playbill_http: tuple[TestClient, str, Path]
) -> None:
    _client, instance_id, _key = playbill_http
    world = pb.world()
    first = world.project.work_item["wi-1"].set(status="ready", because="Checked.")
    status = _field_claim(first, "status")
    source = world.project.work_item["wi-1"].edit(status="done")
    assert source.pin("status") == f"{KIND}/wi-1#{KIND}.status"
    judged = world.project.work_item["wi-2"].edit(title="Follows", based_on=source.pin("status"))
    outcome = world.write(judged, source, because="Judged from the new status.")
    assert outcome.status == "accepted", outcome
    assert _field_claim(outcome, "status") == status
    assert _based_on_pins(instance_id, _field_claim(outcome, "title")) == {
        status: _digest(instance_id, status)
    }


def test_a_draft_pin_previews_proposes_and_activates(pb: Cruxible) -> None:
    world = pb.world()
    observed = world.project.work_item.define("wi-9").set(status="ready")
    judged = world.project.work_item.define("wi-10").set(
        title="Ready", based_on=observed.pin("status")
    )
    preview = world.write(observed, judged, because="Judged.", dry_run=True)
    assert preview.status == "would_accept", preview
    (planned,) = [item for item in preview.changes if item.field == "title"]
    assert planned.based_on == (_field_claim(preview, "status"),)
    assert observed.address is None

    proposed = world.write(observed, judged, because="Judged.", accept="never")
    assert proposed.status == "awaiting_approval" and proposed.proposal is not None, proposed
    status = _field_claim(proposed, "status")
    (shown,) = [item for item in proposed.changes if item.field == "title"]
    assert shown.based_on == (status,)
    receipt = pb.proposal(proposed.proposal.proposal_id).activate()
    assert receipt.status == "accepted"


def test_a_draft_pin_checks_its_field_and_refuses_a_self_pin(pb: Cruxible) -> None:
    world = pb.world()
    draft = world.project.work_item.define("wi-9")
    with pytest.raises(AttributeError, match="stauts"):
        draft.pin("stauts")
    draft.set(status="ready", based_on=draft.pin("status"))
    with pytest.raises(WriteRefusalError) as caught:
        world.write(draft, because="Rests on itself.")
    assert caught.value.error_code == "cruxible.authoring.based_on_self"


def test_the_world_stub_types_pin_by_field_for_drafts_and_edits(
    pb: Cruxible, tmp_path: Path
) -> None:
    from tests.test_client.test_playbill_sdk_world import _mypy

    stub = pb.world().stub()
    ast.parse(stub)
    assert "    WorldDraftPin," in stub
    assert ") -> WorldDraftPin: ..." in stub
    project = tmp_path / "pin-project"
    project.mkdir()
    (project / "world.pyi").write_text(stub, encoding="utf-8")
    (project / "uses_pin.py").write_text(
        "from __future__ import annotations\n"
        "\n"
        "from world import World\n"
        "\n"
        "\n"
        "def pinned(world: World) -> None:\n"
        "    source = world.project.work_item.define('wi-9').set(status='ready')\n"
        "    edit = world.project.work_item['wi-1'].edit(title='x')\n"
        "    world.project.work_item.define('wi-10').set(\n"
        "        title='y', based_on=[source.pin('status'), edit.pin('title'), 'CLM-x']\n"
        "    )\n"
        "    source.pin('stauts')\n",
        encoding="utf-8",
    )
    report = _mypy(project, "uses_pin.py")
    assert "uses_pin.py:12: error:" in report, report
    assert report.count(": error:") == 1, report


def test_a_long_chain_of_draft_pins_is_walked_without_recursion(pb: Cruxible) -> None:
    # Each fresh change pins the draft before it: a chain as long as a write
    # may carry, walked from its last draft alone.
    world = pb.world()
    drafts = [world.project.work_item.define(f"wi-{n}") for n in range(100, 500)]
    landed = world.write(*drafts, because="Each work item exists.")
    assert landed.status == "accepted", landed
    drafts[0].set(title="First")
    for earlier, draft in zip(drafts, drafts[1:], strict=False):
        draft.set(title=f"After {earlier.address}", based_on=earlier.pin("title"))

    preview = world.write(drafts[-1], because="Each rests on the one before.", dry_run=True)

    assert preview.status == "would_accept", preview
    titles = [item for item in preview.changes if item.field == "title"]
    assert len(titles) == len(drafts)
    assert all(item.based_on for item in titles[1:])


def test_a_draft_pin_on_its_key_names_the_key_claim_its_creation_writes(
    pb: Cruxible, playbill_http: tuple[TestClient, str, Path]
) -> None:
    _client, instance_id, _key = playbill_http
    world = pb.world()
    created = world.project.work_item.define("wi-9")
    judged = world.project.work_item["wi-2"].edit(
        title="Follows wi-9", based_on=created.pin("slug")
    )
    outcome = world.write(created, judged, because="Judged from the new key.")
    assert outcome.status == "accepted", outcome
    instance = get_playbill_manager().get(instance_id)
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        (key_claim,) = [
            str(row[0]).removeprefix("Claim:")
            for row in projection.typed.connection.execute(
                "SELECT identity FROM claims WHERE subject_path=? AND predicate=?",
                (f"subjects/{KIND}/wi-9.json", f"{KIND}.slug"),
            )
        ]
    assert _based_on_pins(instance_id, _field_claim(outcome, "title")) == {
        key_claim: _digest(instance_id, key_claim)
    }
