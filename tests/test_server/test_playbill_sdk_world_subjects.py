"""Which Subjects a World knows, against a real daemon: live, complete, at one coordinate.

A World resolves individual Subjects through ``get`` and lists a kind only on
explicit enumeration through ``query``. These run the SDK
over an in-process HTTP app on a real instance, because what is under test is
what the service's queries bind: retired Subject shells, and the server's
result ceiling.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from cruxible_client.authoring.sdk import Cruxible
from cruxible_client.authoring.sdk_types import AbsentSubject
from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.compact_query import QueryRequest
from cruxible_client.contracts.subjects import (
    parse_subject,
    render_subject,
    subject_digest,
    subject_path,
)
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from cruxible_core.service.discovery import compact_query as compact_module
from tests.core_support._write_support import KIND, seed_write_vocabulary


def _retire_subject(instance: PlaybillInstance, subject_id: str, *, actor_id: str) -> None:
    """Accept one change set that retires a Subject shell, through the ordinary path."""

    path = subject_path(KIND, subject_id)
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    shell = parse_subject(tree[path], path=path)
    tree[path] = render_subject(
        shell.model_copy(
            update={
                "lifecycle": ArtifactLifecycle(
                    state="retired", predecessor_digest=subject_digest(shell).tagged
                )
            }
        )
    )
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id=actor_id),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/retire-{subject_id}",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp="2026-09-29T11:59:30.000000Z",
    )
    assert proposed.candidate is not None, proposed.evaluation
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposed.admission.proposal_id, activated_by=actor_id
    )
    assert receipt.status == "accepted"


@pytest.fixture
def pb(playbill_http: tuple[TestClient, str, Path], tmp_path: Path) -> Cruxible:
    """wi-1, wi-2 and wi-3 accepted, then wi-3 retired."""

    client, instance_id, _key = playbill_http
    actor = client.get(f"/api/v1/{instance_id}/whoami").json()["actor_id"]
    instance = get_playbill_manager().get(instance_id)
    seed_write_vocabulary(instance, actor_id=actor)
    _retire_subject(instance, "wi-3", actor_id=actor)
    transport = CruxibleClient(base_url="http://testserver")
    transport._client._client = client  # type: ignore[attr-defined]  # noqa: SLF001
    workspace = tmp_path / "sdk-workspace"
    workspace.mkdir()
    return Cruxible._from_client(  # noqa: SLF001
        transport,
        instance_id=instance_id,
        workspace=workspace,
        clock=lambda: datetime(2026, 9, 29, 12, tzinfo=UTC),
    )


def _query(pb: Cruxible, **fields: object) -> object:
    return pb._client.query(  # noqa: SLF001
        pb._instance_id,  # noqa: SLF001
        request=QueryRequest.model_validate(fields),
    )


def test_a_world_does_not_resurrect_a_retired_subject(pb: Cruxible) -> None:
    world = pb.world()
    items = world.project.work_item

    assert items.subject_ids == ("wi-1", "wi-2")
    assert "wi-3" not in items
    with pytest.raises(AbsentSubject) as absent:
        items["wi-3"]
    assert absent.value.subject_id == "wi-3"


def test_direct_lookup_refuses_a_retired_subject_before_any_inventory(pb: Cruxible) -> None:
    items = pb.world().project.work_item
    with pytest.raises(AbsentSubject) as absent:
        items["wi-3"]
    assert absent.value.subject_id == "wi-3"
    assert "wi-3" not in items
    assert "wi-1" in items


def test_alias_of_a_live_subject_wins_over_a_retired_id_even_after_enumeration(
    pb: Cruxible,
) -> None:
    assert (
        pb.changes(because="Merged the retired item.")
        .add(f"{KIND}/wi-2", "semantic.alias", "wi-3")
        .write()
        .status
        == "accepted"
    )
    items = pb.world().project.work_item
    assert items.subject_ids == ("wi-1", "wi-2")
    assert items["wi-3"] is items["wi-2"]
    assert "wi-3" in items
    assert len(items) == 2


def test_query_lists_live_subjects_and_marks_retired_ones_on_request(pb: Cruxible) -> None:
    live = _query(pb, kind=KIND, select=["subject_id"])
    assert [row["subject_id"] for row in live.rows] == ["wi-1", "wi-2"]  # type: ignore[attr-defined]
    assert all("lifecycle" not in row for row in live.rows)  # type: ignore[attr-defined]

    every = _query(pb, kind=KIND, select=["subject_id"], status=["live", "retired"])
    assert [(row["subject_id"], row["lifecycle"]) for row in every.rows] == [  # type: ignore[attr-defined]
        ("wi-1", "live"),
        ("wi-2", "live"),
        ("wi-3", "retired"),
    ]


def test_a_world_reads_every_subject_past_the_server_ceiling(
    pb: Cruxible, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capped answer continues as a new window; it is never taken as complete."""

    monkeypatch.setattr(compact_module, "COMPACT_QUERY_MAX_RESULTS", 1)
    capped = _query(pb, kind=KIND, select=["subject_id"])
    assert capped.capped == ("max_results=1",)  # type: ignore[attr-defined]

    items = pb.world().project.work_item
    assert items.subject_ids == ("wi-1", "wi-2")
    assert items["wi-2"].address == f"{KIND}/wi-2"


def test_a_retired_subject_takes_no_place_under_the_ceiling(
    pb: Cruxible,
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live-only answer under a ceiling of one is the first live Subject, not empty."""

    client, instance_id, _key = playbill_http
    actor = client.get(f"/api/v1/{instance_id}/whoami").json()["actor_id"]
    _retire_subject(get_playbill_manager().get(instance_id), "wi-1", actor_id=actor)
    monkeypatch.setattr(compact_module, "COMPACT_QUERY_MAX_RESULTS", 1)

    first = _query(pb, kind=KIND, select=["subject_id"])

    assert [row["subject_id"] for row in first.rows] == ["wi-2"]  # type: ignore[attr-defined]
    assert first.capped == ()  # type: ignore[attr-defined]


# -- typed creation: w.<kind>.define(...) and w.write(...) -----------------------------

DECISION, TICKET, NOTE, PAIR = "dev.decision", "dev.ticket", "dev.note", "dev.pair"


def _accept_identity_kinds(instance: PlaybillInstance, *, actor_id: str) -> None:
    """A unique slug kind, a minted kind, a hint kind and a composite kind, through authoring."""

    from cruxible_client.contracts.authoring.inputs import (
        ChangeSetInput,
        ClaimTypeInput,
        SubjectKindInput,
        lower_authoring_input,
    )
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from tests.test_service.test_subject_identity import _field, _key

    payload = lower_authoring_input(
        ChangeSetInput(
            kind="change_set",
            members=(
                ClaimTypeInput(kind="claim_type", claim_type=_key(DECISION, "slug")),
                ClaimTypeInput(kind="claim_type", claim_type=_field(DECISION, "title")),
                SubjectKindInput(
                    kind="subject_kind",
                    name=DECISION,
                    keys=(f"{DECISION}.slug",),
                    uniqueness="unique",
                ),
                ClaimTypeInput(kind="claim_type", claim_type=_field(TICKET, "title")),
                SubjectKindInput(kind="subject_kind", name=TICKET, minted=True),
                ClaimTypeInput(kind="claim_type", claim_type=_key(NOTE, "title")),
                SubjectKindInput(
                    kind="subject_kind", name=NOTE, keys=(f"{NOTE}.title",), uniqueness="hint"
                ),
                ClaimTypeInput(kind="claim_type", claim_type=_key(PAIR, "left")),
                ClaimTypeInput(kind="claim_type", claim_type=_key(PAIR, "right")),
                SubjectKindInput(
                    kind="subject_kind",
                    name=PAIR,
                    keys=(f"{PAIR}.left", f"{PAIR}.right"),
                    uniqueness="unique",
                ),
            ),
        )
    )
    actor = AuthenticatedActor(actor_id=actor_id)
    submitted = AuthoringIntentCoordinator.for_instance(instance).compile_and_submit(
        actor=actor, payload=payload, canonical_timestamp="2026-09-29T11:59:40.000000Z"
    )
    assert submitted.status.proposal_id is not None, submitted.intent.last_preflight
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=submitted.status.proposal_id, activated_by=actor_id
    )
    assert receipt.status == "accepted", receipt


@pytest.fixture
def typed(playbill_http: tuple[TestClient, str, Path], tmp_path: Path) -> Cruxible:
    """The write vocabulary (work items keyed by slug) and four more identity rules."""

    client, instance_id, _key = playbill_http
    actor = client.get(f"/api/v1/{instance_id}/whoami").json()["actor_id"]
    instance = get_playbill_manager().get(instance_id)
    seed_write_vocabulary(instance, actor_id=actor)
    _accept_identity_kinds(instance, actor_id=actor)
    transport = CruxibleClient(base_url="http://testserver")
    transport._client._client = client  # type: ignore[attr-defined]  # noqa: SLF001
    workspace = tmp_path / "sdk-workspace"
    workspace.mkdir()
    return Cruxible._from_client(  # noqa: SLF001
        transport,
        instance_id=instance_id,
        workspace=workspace,
        clock=lambda: datetime(2026, 9, 29, 12, tzinfo=UTC),
    )


def test_one_write_creates_five_typed_subjects_and_edits_an_accepted_one(typed: Cruxible) -> None:
    w = typed.world()
    items = w.project.work_item
    first = items.define("wi-new-a", title="First")
    second = items.define(slug="wi-new-b", status="ready").add(governs=first)
    ticket = w.dev.ticket.define(title="A ticket")
    pair = w.dev.pair.define(left="Alpha  One", right="Beta")
    note = w.dev.note.define("Only a key")
    edit = items["wi-1"].edit().set(title="One").add(governs=second)

    outcome = w.write(second, ticket, edit, pair, note, first, because="Seeded together.")

    assert outcome.status == "accepted", outcome
    assert outcome.base is not None
    assert outcome.coordinate.generation == outcome.base.generation + 1
    assert first.address == f"{KIND}/wi-new-a" and second.address == f"{KIND}/wi-new-b"
    assert ticket.address is not None and ticket.address.startswith(f"{TICKET}/sub-")
    assert pair.address == f"{PAIR}/alpha-one--beta"
    assert note.address == f"{NOTE}/only-a-key"
    assert set(outcome.subjects_added) == {
        first.address,
        second.address,
        ticket.address,
        pair.address,
        note.address,
    }
    read = typed.world()
    assert [view.value for view in read.project.work_item["wi-new-a"].title] == ["First"]
    assert [view.value for view in read.dev.ticket[ticket.address.split("/", 1)[1]].title] == [
        "A ticket"
    ]
    assert [view.value for view in read.project.work_item["wi-1"].title] == ["One"]
    with pytest.raises(AbsentSubject):
        read.project.work_item["wi-typo"].set(title="x", because="A typo is never a creation.")


def test_a_define_run_again_lands_on_its_subject_and_expect_new_refuses(typed: Cruxible) -> None:
    from cruxible_client.contracts.errors import WriteRefusalError
    from cruxible_client.contracts.subject_kinds import key_digest_hex

    w = typed.world()
    first = w.dev.decision.define("ship it", title="Ship")
    assert first.write(because="Decided.").status == "accepted"
    assert first.address == f"{DECISION}/ship-it"

    again = typed.world().dev.decision.define("Ship  It")
    outcome = again.write(because="Decided again.")
    assert (outcome.changes[0].resolved, again.address) == ("existing", f"{DECISION}/ship-it")
    with pytest.raises(WriteRefusalError) as refused:
        typed.world().dev.decision.define("ship it", expect_new=True).write(because="New?")
    assert refused.value.error_code == "cruxible.write.subject_exists"

    # A key whose rendering another Subject's id holds takes the digest suffix.
    taken = typed.world().dev.decision.define("ship-it")
    assert taken.write(because="Another decision.").status == "accepted"
    assert taken.address == f"{DECISION}/ship-it-{key_digest_hex(['ship-it'])[:8]}"


def test_world_lookup_resolves_alias_and_single_key_and_keeps_the_read_coordinate(
    typed: Cruxible,
) -> None:
    draft = typed.world().dev.decision.define("Ship It Now", title="Ship")
    assert draft.write(because="Decided.").status == "accepted"
    assert draft.address is not None
    assert (
        typed.changes(because="Name it for readers.")
        .add(draft.address, "semantic.alias", "Launch Decision")
        .write()
        .status
        == "accepted"
    )
    world = typed.world()
    decisions = world.dev.decision
    by_key = decisions["SHIP IT NOW"]
    assert by_key.address == draft.address
    assert decisions["Launch Decision"] is by_key
    assert decisions["ship-it-now"] is by_key
    assert "Launch Decision" in decisions
    assert decisions.subject_ids == ("ship-it-now",)
    assert tuple(decisions) == (by_key,)
    assert len(decisions) == 1

    assert by_key.set(title="Shipped", because="Completed.").status == "accepted"
    # The lookup remains at the World's coordinate even for an uncached alias.
    assert decisions["launch decision"] is by_key
    assert [claim.value for claim in by_key.title] == ["Ship"]
    assert typed.get(by_key, detail="history", field="title").coordinate == world.coordinate


def _assert_cached_json_lookup(cx: Cruxible, kind: str, spelling: str, address: str) -> None:
    # The generic resolver supports this explicit reference at the same pin.
    world = cx.world()
    assert cx.get(f"Subject:{kind}/{spelling}").value.subject == address
    items = world.kind(kind)
    with patch.object(cx._client, "get", wraps=cx._client.get) as get:
        subject = items[spelling]
        assert subject.address == address
        assert spelling in items
        assert items[spelling] is subject
        assert items[address.split("/", 1)[1]] is subject
        assert get.call_count == 1
        request = get.call_args.kwargs["request"]
        assert request.ref == f"Subject:{kind}/{spelling}"
        assert request.at == world.coordinate


def test_alias_ending_in_json_resolves_and_caches_a_subject(typed: Cruxible) -> None:
    assert (
        typed.changes(because="Name the report item.")
        .add(f"{KIND}/wi-2", "semantic.alias", "item.json")
        .write()
        .status
        == "accepted"
    )
    _assert_cached_json_lookup(typed, KIND, "item.json", f"{KIND}/wi-2")


def test_single_key_ending_in_json_resolves_and_caches_a_subject(typed: Cruxible) -> None:
    draft = typed.world().dev.decision.define("Report.json", title="Report")
    assert draft.write(because="Record the report decision.").status == "accepted"
    assert draft.address is not None
    _assert_cached_json_lookup(typed, DECISION, "Report.json", draft.address)


def test_a_hint_key_two_subjects_hold_is_ambiguous_to_define(typed: Cruxible) -> None:
    from cruxible_client.contracts.errors import WriteRefusalError

    w = typed.world()
    assert w.dev.note.define("Same").write(because="One.").status == "accepted"
    further = typed.world().dev.note.define("same", create_new=True)
    assert further.write(because="Another thing, same name.").status == "accepted"
    assert further.address is not None and further.address != f"{NOTE}/same"
    with pytest.raises(WriteRefusalError) as refused:
        typed.world().dev.note.define("SAME").write(because="Which one?")
    assert refused.value.error_code == "cruxible.subject.key_ambiguous"

    from cruxible_client.contracts.errors import ReadRefusalError

    items = typed.world().dev.note
    for lookup in (lambda: items["SAME"], lambda: "SAME" in items):
        with pytest.raises(ReadRefusalError) as ambiguous:
            lookup()
        assert ambiguous.value.error_code == "cruxible.get.ref_ambiguous"


def test_the_sdk_reads_a_subject_s_whole_field_history_across_pages(
    pb: Cruxible, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_client.contracts.get_reads import GetSubjectHistory
    from cruxible_core.service.discovery import get as get_module

    for value, because in (("ready", "Started."), ("blocked", "Waiting."), ("done", "Shipped.")):
        pb.set(f"{KIND}/wi-1", "status", value, because=because)
    # Pages of two events: the SDK follows the cursor to the last one.
    monkeypatch.setattr(get_module, "GET_HISTORY_DEFAULT_LIMIT", 2)

    history = pb.get(f"{KIND}/wi-1", detail="history", field="status").value

    assert isinstance(history, GetSubjectHistory)
    assert [(event.change, event.value) for event in history.events] == [
        ("changed", "done"),
        ("changed", "blocked"),
        ("stated", "ready"),
    ]
    assert [event.reason.text for event in history.events] == [  # type: ignore[union-attr]
        "Shipped.",
        "Waiting.",
        "Started.",
    ]
    assert history.events[0].before == "blocked"  # type: ignore[union-attr]
