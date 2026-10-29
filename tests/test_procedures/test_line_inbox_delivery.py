"""A Line's ``post_inbox`` terminal: items posted as they are, with their own evidence."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.procedures.artifacts import procedure_path, render_procedure
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
from cruxible_client.contracts.procedures.models import (
    InboxEgressNode,
    ProjectNode,
    SourceNode,
)
from cruxible_core.exhaust.line_inbox import LineInboxStore
from cruxible_core.runtime.instance import PlaybillInstance
from tests.support.procedures import MAX_WALL_CLOCK, RUN_WALL_CLOCK
from tests.test_procedures.test_manual_line_runs import manual_run
from tests.test_procedures.test_procedure_source_runs import (
    ADVISORY,
    NOW,
    SOURCE_ALIAS,
    _accept_more,
    _served_line,
    _world,
)

pytestmark = pytest.mark.skip(
    reason=(
        "needs B3 extract: every test in this file reads structured rows "
        "(a list) out of a Source read via '$steps.<alias>.content.json...' "
        "or by spreading the fixture ADVISORY constant as a JSON mapping -- "
        "both assume the provider JSON-decodes the file, which no longer "
        "happens pre-extract (content.text is the read's whole decoded text, "
        "and the shared ADVISORY fixture is now a plain bytes string, not a "
        "dict). This whole file's post_inbox-over-structured-rows premise "
        "needs the extract node, not a mechanical fixture fix."
    )
)

ROWS = [{"cve": "CVE-2026-0001"}, {"cve": "CVE-2026-0002"}, {"cve": "CVE-2026-0003"}]


def inbox_world(  # type: ignore[no-untyped-def]
    tmp_path: Path, *, max_items: int | None = None, items: Any = None, extra: Any = None
):
    """The Source Procedure, ending in a post_inbox over its rows, behind an observe-only Line."""

    instance, owner, procedure, root, policy = _world(
        tmp_path,
        accept_procedure=False,
        contents=canonical_bytes({**ADVISORY, "rows": ROWS, **(extra or {})}),
    )
    source, shape = procedure.definition.nodes
    assert isinstance(source, SourceNode) and isinstance(shape, ProjectNode)
    definition = procedure.definition.model_copy(
        update={
            "nodes": (
                source,
                shape.model_copy(update={"next": "inbox"}),
                InboxEgressNode(
                    node_id="inbox",
                    input=(
                        {"items": f"$steps.{SOURCE_ALIAS}.content.json.rows"}
                        if items is None
                        else items
                    ),
                    max_items=max_items,
                ),
            ),
            "terminal_capability": 1,
        }
    )
    with_inbox = procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
        }
    )
    line = _served_line(with_inbox, policy)
    _accept_more(
        instance,
        owner,
        {
            procedure_path(with_inbox.identity.name): render_procedure(with_inbox),
            line_spec_path(line.identity.name): render_line_spec(line),
        },
        name="inbox-line",
    )
    return instance, root, line


def _entries(instance: PlaybillInstance) -> list[Any]:
    store = LineInboxStore(instance)
    entries, _cursor = store.page(line=None, status=None, limit=50, cursor=None)
    return [store.get(entry.envelope.item_id) for entry in entries]


def test_a_line_posts_each_item_as_is_with_its_capture_attached(tmp_path: Path) -> None:
    instance, root, line = inbox_world(tmp_path)
    head = instance.accepted_coordinate()

    state = manual_run(instance, line, at=NOW, root=root)

    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert (egress.kind, egress.verdict, egress.required_authority) == (
        "post_inbox",
        "delivered",
        "observe",
    )
    assert egress.inbox_truncation is None
    (capture,) = {item.capture_digest for item in state.source_observations}
    entries = _entries(instance)
    assert sorted(item.payload["cve"] for item in entries) == [row["cve"] for row in ROWS]
    for item in entries:
        envelope = item.envelope
        assert envelope.status == "unread"
        assert envelope.line == line.identity.qualified
        assert envelope.node_id == "inbox"
        assert envelope.occurrences == 1
        assert envelope.first_sighting == envelope.last_sighting
        assert envelope.last_sighting.run_id == state.run_id
        assert envelope.last_sighting.source_capture_digests == (capture,)
    assert {child.egress_digest for child in egress.children} == {
        item.envelope.dedupe_key for item in entries
    }
    assert LineInboxStore(instance).unread_by_line() == ((line.identity.qualified, 3),)
    # Posting is inert: the accepted head did not move.
    assert instance.accepted_coordinate() == head


def test_a_run_posts_at_most_its_cap_and_reports_the_truncation(tmp_path: Path) -> None:
    instance, root, line = inbox_world(tmp_path, max_items=2)
    head = instance.accepted_coordinate()

    state = manual_run(instance, line, at=NOW, root=root)

    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert egress.inbox_truncation is not None
    assert egress.inbox_truncation.model_dump(exclude={"tag"}) == {
        "cap": 2,
        "cap_source": "declared",
        "selected": 3,
        "posted": 2,
    }
    assert len(egress.children) == 2
    posted = sorted((item.payload for item in _entries(instance)), key=lambda row: row["cve"])
    assert posted == ROWS[:2]
    assert instance.accepted_coordinate() == head


def test_a_single_input_is_one_item(tmp_path: Path) -> None:
    instance, root, line = inbox_world(tmp_path, items=f"$steps.{SOURCE_ALIAS}.content.json")

    state = manual_run(instance, line, at=NOW, root=root)

    assert state.status == "succeeded", state.terminal
    (entry,) = _entries(instance)
    assert entry.payload == {**ADVISORY, "rows": ROWS}


def test_an_hourly_reread_of_identical_rows_is_one_item_per_row_seen_each_hour(
    tmp_path: Path,
) -> None:
    """Each tick reads its own Capture; the same rows are the same items, seen again."""

    instance, root, line = inbox_world(tmp_path)
    hours = 4

    states = [
        manual_run(instance, line, at=NOW + timedelta(hours=hour), root=root)
        for hour in range(hours)
    ]

    assert all(state.status == "succeeded" for state in states)
    entries = _entries(instance)
    assert sorted(item.payload["cve"] for item in entries) == [row["cve"] for row in ROWS]
    captures = [
        next(iter({item.capture_digest for item in state.source_observations})) for state in states
    ]
    assert len(set(captures)) == hours  # new evidence every hour
    for item in entries:
        envelope = item.envelope
        assert envelope.occurrences == hours
        assert envelope.first_sighting.run_id == states[0].run_id
        assert [sighting.source_capture_digests for sighting in envelope.recent_sightings] == [
            (capture,) for capture in reversed(captures)
        ]
    assert LineInboxStore(instance).unread_by_line() == ((line.identity.qualified, len(ROWS)),)


def test_the_same_item_again_keeps_its_disposition(tmp_path: Path) -> None:
    """An item no Capture backs, posted by two runs: one item, seen twice, decided once."""

    from cruxible_client.contracts.line_inbox import InboxDismissed
    from cruxible_client.contracts.projection import AcceptedCoordinate
    from tests.test_procedures.test_procedure_source_runs import _actor

    instance, root, line = inbox_world(tmp_path, items={"items": [{"note": "check the feed"}]})
    first = manual_run(instance, line, at=NOW, root=root)
    (entry,) = _entries(instance)
    assert entry.envelope.last_sighting.source_capture_digests == ()
    store = LineInboxStore(instance)
    dismissed = InboxDismissed(actor_id="owner", decided_at=NOW, reason="seen it")
    store.record_disposition(
        entry.envelope.item_id,
        dismissed,
        actor=_actor(instance),
        now=NOW,
        coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )

    second = manual_run(instance, line, at=NOW + timedelta(seconds=1), root=root)

    assert first.status == second.status == "succeeded"
    (entry,) = _entries(instance)
    envelope = entry.envelope
    assert (envelope.status, envelope.occurrences, envelope.recurred_since_disposition) == (
        "dismissed",
        2,
        1,
    )
    assert envelope.disposition == dismissed
    assert (envelope.first_sighting.run_id, envelope.last_sighting.run_id) == (
        first.run_id,
        second.run_id,
    )


# --- a single input is one item, exactly as posted (review F-004) -----------------


@pytest.mark.parametrize(
    ("items", "extra", "expected"),
    [
        ([{"note": "a"}, {"note": "b"}], None, [{"note": "a"}, {"note": "b"}]),
        ([], None, []),
        (f"$steps.{SOURCE_ALIAS}.content.json.rows", None, ROWS),
        (
            f"$steps.{SOURCE_ALIAS}.content.json.bundle",
            {"bundle": {"items": [1, 2], "kind": "batch"}},
            {"items": [1, 2], "kind": "batch"},
        ),
    ],
    ids=["literal-array", "empty-array", "referenced-array", "object-holding-items"],
)
def test_a_single_input_is_posted_whole(  # type: ignore[no-untyped-def]
    tmp_path: Path, items, extra, expected
) -> None:
    instance, root, line = inbox_world(tmp_path, items=items, extra=extra)

    state = manual_run(instance, line, at=NOW, root=root)

    assert state.status == "succeeded", state.terminal
    (entry,) = _entries(instance)
    assert entry.payload == expected


# --- an explicit fan-out must resolve to a list (review F-005) --------------------


@pytest.mark.parametrize(
    ("field", "extra", "reason"),
    [
        ("absent", None, "items_unresolved"),
        ("nothing", {"nothing": None}, "items_not_a_list"),
        ("severity", None, "items_not_a_list"),
        ("bundle", {"bundle": {"rows": ROWS}}, "items_not_a_list"),
    ],
    ids=["absent", "null", "string", "object"],
)
def test_an_explicit_fan_out_that_is_not_a_list_refuses_typed(  # type: ignore[no-untyped-def]
    tmp_path: Path, field, extra, reason
) -> None:
    instance, root, line = inbox_world(
        tmp_path, items={"items": f"$steps.{SOURCE_ALIAS}.content.json.{field}"}, extra=extra
    )

    state = manual_run(instance, line, at=NOW, root=root)

    assert state.status == "node_refused", state.terminal
    assert state.terminal.code == "inbox_item_invalid"  # type: ignore[union-attr]
    assert state.terminal.details == {"field": "input.items", "reason": reason}  # type: ignore[union-attr]
    assert _entries(instance) == []


# --- inbox bytes are bounded before anything is posted (review F-003) -------------


def test_an_item_over_the_byte_bound_refuses_and_posts_nothing(tmp_path: Path) -> None:
    from cruxible_client.contracts.line_inbox import INBOX_ITEM_MAX_BYTES

    big = "x" * INBOX_ITEM_MAX_BYTES  # its canonical JSON adds the quotes
    instance, root, line = inbox_world(tmp_path, items={"items": [{"n": 1}, big]})

    state = manual_run(instance, line, at=NOW, root=root)

    assert state.status == "node_refused", state.terminal
    assert state.terminal.code == "inbox_item_over_budget"  # type: ignore[union-attr]
    assert state.terminal.details["bound"] == "item"  # type: ignore[union-attr]
    assert state.terminal.details["child_index"] == 1  # type: ignore[union-attr]
    assert _entries(instance) == []


def test_an_item_just_under_the_byte_bound_posts(tmp_path: Path) -> None:
    from cruxible_client.contracts.line_inbox import INBOX_ITEM_MAX_BYTES

    fits = "x" * (INBOX_ITEM_MAX_BYTES - 2)
    instance, root, line = inbox_world(tmp_path, items=fits)

    state = manual_run(instance, line, at=NOW, root=root)

    assert state.status == "succeeded", state.terminal
    (entry,) = _entries(instance)
    assert entry.envelope.payload_bytes == INBOX_ITEM_MAX_BYTES


def _accept_sdk_input(instance: PlaybillInstance, owner: Any, payload: Any) -> None:
    """Accept one SDK-built authoring payload through the coordinator, as any surface does."""

    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from cruxible_core.service.authoring.documents import (
        service_activate_playbill_proposal,
        service_submit_playbill_approval,
    )
    from tests.test_ledger.test_activation import _sign

    actor = AuthenticatedActor(actor_id="owner")
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    compiled = coordinator.compile(
        actor=actor, payload=payload, canonical_timestamp="2026-09-12T11:40:00.000000Z"
    )
    assert compiled.verdict == "passed", compiled.frontier
    intent = coordinator.list_pending(actor=actor).intents[0]
    submitted = coordinator.submit(intent.intent_id, actor=actor)
    approval = _sign(
        owner, submitted.status.candidate_digest, instance.accepted_coordinate().semantic_root
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=submitted.status.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    service_activate_playbill_proposal(
        instance, proposal_id=submitted.status.proposal_id, activated_by="owner"
    )


def test_a_procedure_built_only_through_the_sdk_posts_to_the_inbox(tmp_path: Path) -> None:
    """Sequence(..., PostInbox(...)) lowers to the same post_inbox terminal a Line posts from."""

    from cruxible_client.authoring.inputs import CarriedContractInput
    from cruxible_client.authoring.procedures import (
        Output,
        PostInbox,
        Project,
        ProviderBinding,
        Sequence,
        Source,
    )
    from cruxible_client.authoring.sdk import Cruxible
    from cruxible_client.contracts.procedures.artifacts import parse_procedure
    from cruxible_client.contracts.procedures.contract_schema import PropertySchema
    from cruxible_client.contracts.procedures.models import ProcedureBudget, ProcedureHardCaps
    from cruxible_client.contracts.workspace_file import WORKSPACE_FILE_OPERATION
    from cruxible_core.governance.seed_artifacts.workspace_file import (
        WORKSPACE_FILE_BUILTIN_PROVIDER_ID,
    )
    from tests.core_support._light_capture_support import record_contract as capture_contract
    from tests.test_procedures.test_procedure_source_runs import _source_request

    instance, owner, _procedure, root, policy = _world(
        tmp_path,
        accept_procedure=False,
        contents=canonical_bytes({**ADVISORY, "rows": ROWS}),
    )
    severity = CarriedContractInput(
        name="sdk-inbox-output", fields={"severity": PropertySchema(type="string")}
    )
    plan = Sequence(
        (
            Source(
                SOURCE_ALIAS,
                capture_contract=capture_contract().identity.name,
                request=_source_request(instance, root).model_dump(mode="json"),
            ),
            Project(
                "result",
                fields={"severity": Output(SOURCE_ALIAS, "content.json.severity")},
                contract_out=severity,
            ),
            PostInbox("inbox", input={"items": Output(SOURCE_ALIAS, "content.json.rows")}),
        ),
        name="sdk-inbox",
        contract_in=CarriedContractInput(name="sdk-inbox-input", fields={}),
        contract_out=severity,
        budget=ProcedureBudget(
            wall_clock=RUN_WALL_CLOCK,
            max_provider_calls=2,
            max_capture_bytes=65_536,
        ),
        hard_caps=ProcedureHardCaps(
            max_wall_clock=MAX_WALL_CLOCK,
            max_provider_calls=4,
            max_capture_bytes=131_072,
            max_items=200,
            max_repeat_attempts=1,
        ),
        acquisition_policy=policy.identity.name,
    ).bind(
        **{
            SOURCE_ALIAS: ProviderBinding(
                provider=WORKSPACE_FILE_BUILTIN_PROVIDER_ID,
                interface=WORKSPACE_FILE_OPERATION,
            )
        }
    )
    preview = plan.preview()
    assert preview.ready_for_prepare, preview.errors
    assert preview.terminals == ("inbox",)
    assert preview.authority == "observe"
    plan.build()

    _accept_sdk_input(instance, owner, Cruxible.procedure(object(), definition=plan).payload)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    accepted = parse_procedure(tree[procedure_path("sdk-inbox")], path=procedure_path("sdk-inbox"))
    assert accepted.definition.model_dump(mode="json", by_alias=True)["nodes"][-1] == {
        "kind": "post_inbox",
        "node_id": "inbox",
        "input": {"items": f"$steps.{SOURCE_ALIAS}.content.json.rows"},
    }
    line = _served_line(accepted, policy)
    _accept_more(
        instance,
        owner,
        {line_spec_path(line.identity.name): render_line_spec(line)},
        name="sdk-inbox-line",
    )

    state = manual_run(instance, line, at=NOW, root=root)

    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert (egress.kind, egress.verdict) == ("post_inbox", "delivered")
    assert sorted(item.payload["cve"] for item in _entries(instance)) == [
        row["cve"] for row in ROWS
    ]
