"""The SDK write verbs over a daemon: pb.set, pb.retire, pb.changes(because=...), World writes."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client.authoring.sdk import ChangeSetDraft, Playbill, WriteBatch
from cruxible_client.contracts.errors import WriteRefusalError
from cruxible_client.contracts.write import SlotRef, WriteOutcome
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from tests.core_support._write_support import KIND, seed_write_vocabulary

WI1 = f"{KIND}/wi-1"


def _sdk(client: TestClient, instance_id: str, tmp_path: Path, name: str = "sdk") -> Playbill:
    transport = CruxibleClient(base_url="http://testserver")
    transport._client._client = client  # type: ignore[attr-defined]  # noqa: SLF001
    workspace = tmp_path / f"{name}-workspace"
    workspace.mkdir()
    return Playbill._from_client(  # noqa: SLF001
        transport,
        instance_id=instance_id,
        workspace=workspace,
        clock=lambda: datetime(2026, 9, 29, 12, tzinfo=UTC),
    )


@pytest.fixture
def pb(playbill_http: tuple[TestClient, str, Path], tmp_path: Path) -> Playbill:
    client, instance_id, _key = playbill_http
    actor = client.get(f"/api/v1/{instance_id}/playbill/whoami").json()["actor_id"]
    seed_write_vocabulary(get_playbill_manager().get(instance_id), actor_id=actor)
    return _sdk(client, instance_id, tmp_path)


def test_set_accepts_revises_and_advances_the_connection(pb: Playbill) -> None:
    first = pb.set(WI1, "status", "ready", because="Checked.")
    assert isinstance(first, WriteOutcome) and first.status == "accepted"
    assert first.accepted_coordinate is not None
    assert pb.coordinate.git_oid == first.accepted_coordinate.git_oid

    second = pb.set(WI1, "status", "done", because="Shipped.")
    assert second.changes[0].revises == first.changes[0].claim
    assert second.changes[0].verdict == "supported"


def test_a_stale_set_refuses_then_setting_again_replaces_the_value_it_showed(
    pb: Playbill, playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    first = pb.set(WI1, "status", "ready", because="Checked.")
    client, instance_id, _key = playbill_http
    _sdk(client, instance_id, tmp_path, name="other").set(
        WI1, "status", "blocked", because="Someone else."
    )
    with pytest.raises(WriteRefusalError) as caught:
        pb.set(WI1, "status", "done", because="Stale.")
    assert caught.value.error_code == "playbill.write.slot_changed"
    assert "'blocked'" in str(caught.value)
    again = pb.set(WI1, "status", "done", because="Seen it; replacing.")
    assert again.status == "accepted"
    assert again.changes[0].before == "blocked"
    assert again.changes[0].revises == first.changes[0].claim


def test_a_refusal_raises_a_typed_error_carrying_the_outcome(pb: Playbill) -> None:
    with pytest.raises(WriteRefusalError) as caught:
        pb.set(WI1, "status", "dne", because="x")
    error = caught.value
    assert error.error_code == "playbill.write.value_not_member"
    assert error.candidates == ("done",)
    assert "blocked, done, ready" in str(error)
    assert isinstance(error.outcome, WriteOutcome) and error.outcome.status == "refused"


def test_a_batch_writes_two_adds_and_a_retire_as_one_change_set(pb: Playbill) -> None:
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
    pb: Playbill, playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    pb.set(WI1, "status", "ready", because="Checked.", expect=[])
    client, instance_id, _key = playbill_http
    _sdk(client, instance_id, tmp_path, name="other").set(
        WI1, "status", "blocked", because="Someone else."
    )
    with pytest.raises(WriteRefusalError) as caught:
        pb.set(WI1, "status", "done", because="Stale.", expect="ready", at=None)
    assert caught.value.error_code == "playbill.write.slot_changed"
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
    assert present.value.error_code == "playbill.write.value_already_present"
    ended = pb.retire(SlotRef(subject=WI1, field="status"), because="Withdrawn.", expect="done")
    assert ended.status == "accepted", ended
    batch = pb.changes(because="Unlinked.").retire(
        SlotRef(subject=WI1, field="governs"), expect=[f"{KIND}/wi-2"]
    )
    assert batch.changes[0].expect == (f"{KIND}/wi-2",)  # type: ignore[union-attr]
    assert batch.write().status == "accepted"


def test_retire_dry_run_and_proposal_accept(pb: Playbill) -> None:
    claim = pb.set(WI1, "title", "Old", because="x").changes[0].claim
    assert claim is not None
    preview = pb.retire(claim, because="Gone.", dry_run=True)
    assert preview.status == "would_accept"
    proposed = pb.retire(claim, because="Gone.", accept="never")
    assert proposed.status == "awaiting_approval" and proposed.proposal is not None
    proposal = pb.proposal(proposed.proposal.proposal_id)
    assert repr(proposal) == f"Proposal({proposed.proposal.proposal_id!r})"
    receipt = proposal.accept()
    assert receipt.status == "accepted"


def test_world_writes_keep_references_valid_after_their_own_write(
    pb: Playbill, playbill_http: tuple[TestClient, str, Path], tmp_path: Path
) -> None:
    world = pb.world()
    item = world.project.work_item["wi-1"]
    assert item.set(status="ready", because="Checked.").status == "accepted"
    # The same reference, and the same World, after its own write: no refusal.
    again = item.set(status="done", title="Tidy the CLI", because="Shipped.")
    assert again.status == "accepted", again
    assert [change.field for change in again.changes] == ["status", "title"]
    assert item.add(governs=world.project.work_item["wi-2"], because="Linked.").status == "accepted"
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
    assert caught.value.error_code == "playbill.write.slot_changed"
    # An untouched slot still writes from the same World.
    assert world.project.work_item["wi-3"].set(status="ready", because="x").status == "accepted"


def test_the_world_stub_types_set_with_enum_literals(pb: Playbill) -> None:
    stub = pb.world().stub()
    ast.parse(stub)
    assert "def set(" in stub and "def add(" in stub and "def retire(" in stub
    assert "status: Literal['blocked', 'done', 'ready'] = ...," in stub
    assert "governs: str | SubjectRef = ...," in stub
    assert "ruling: str = ...," in stub
