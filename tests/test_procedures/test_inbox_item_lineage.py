"""An inbox item carries only the Captures in its own dependency closure (review F-001).

Uses the two-Capture run states of the proposal fan-out tests: list ``joined``
built from Captures A and B, and a bundle whose ``items`` field keeps
per-element lineage while its sibling ``other`` does not.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.procedures.models import InboxEgressNode
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.exhaust.line_inbox import LineInboxStore
from cruxible_core.procedures.execution import _inbox_items, _RunRefusal
from cruxible_core.procedures.inbox_delivery import InboxTerminalEgressSink
from cruxible_core.procedures.terminal_dependencies import build_terminal_item_manifest
from tests.core_support._support import initialize_local
from tests.test_exhaust.test_line_inbox_store import NOW, _actor
from tests.test_procedures.test_proposal_fan_out import (
    _FEED_A,
    _FEED_B,
    _bundle,
    _two_capture_state,
    _two_lists,
)


def _node(items: object) -> InboxEgressNode:
    return InboxEgressNode(node_id="inbox", input={"items": items})


def _posted_captures(tmp_path: Path, values, tokens) -> list[tuple[str, ...]]:  # type: ignore[no-untyped-def]
    """Deliver the items through the real sink and read back each one's Captures."""

    instance, _owner = initialize_local(tmp_path)
    store = LineInboxStore(instance)
    items = tuple(
        SimpleNamespace(item_key=f"item-{index}", child_index=index, value=value)
        for index, value in enumerate(values)
    )
    manifests = {
        item.item_key: build_terminal_item_manifest(
            closure, run_id="RUN-test", terminal_node_id="inbox", item_key=item.item_key
        )
        for item, closure in zip(items, tokens, strict=True)
    }
    request = SimpleNamespace(
        kind="post_inbox",
        procedure_identity=ArtifactIdentity(kind="Procedure", name="watcher"),
        procedure_artifact_digest="sha256:" + "d" * 64,
        run_id="RUN-test",
        node_id="inbox",
        items=items,
        prepared_at=NOW,
        actor_context=_actor(instance),
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )
    receipt = InboxTerminalEgressSink(
        store=store, line=ArtifactIdentity(kind="Line", name="watch")
    ).deliver_inbox_egress(request=request, manifests=manifests)  # type: ignore[arg-type]
    return [
        store.get(store.resolve(child.egress_digest)).envelope.last_sighting.source_capture_digests
        for child in receipt.children
    ]


def test_each_element_with_lineage_carries_only_its_own_capture(tmp_path: Path) -> None:
    state = _two_capture_state(lambda a, b: (frozenset({a}), frozenset({b})))

    values, tokens = _inbox_items(_node("$steps.joined.items"), state=state)  # type: ignore[arg-type]

    assert len(values) == 2
    assert _posted_captures(tmp_path, values, tokens) == [(_FEED_A,), (_FEED_B,)]


def test_a_two_capture_list_without_lineage_refuses_instead_of_lending_both() -> None:
    state = _two_capture_state(lambda a, b: None)

    with pytest.raises(_RunRefusal) as refused:
        _inbox_items(_node("$steps.joined.items"), state=state)  # type: ignore[arg-type]

    assert refused.value.refusal.code == "inbox_item_evidence_ambiguous"
    assert refused.value.refusal.details == {
        "field": "input.items",
        "reason": "lineage_unavailable",
    }


@pytest.mark.parametrize("path", ["$steps.bundle.other", "$steps.bundle.items.0.nested"])
def test_a_sibling_or_nested_path_does_not_borrow_the_top_level_lineage(path: str) -> None:
    from tests.test_procedures.test_proposal_fan_out import _element

    state = _bundle(_two_lists())
    state.outputs["bundle"]["items"][0]["nested"] = [_element("nested")]

    with pytest.raises(_RunRefusal) as refused:
        _inbox_items(_node(path), state=state)  # type: ignore[arg-type]

    assert refused.value.refusal.code == "inbox_item_evidence_ambiguous"


def test_a_literal_list_gives_each_element_its_own_references(tmp_path: Path) -> None:
    state = _bundle(_two_lists())

    values, tokens = _inbox_items(  # type: ignore[arg-type]
        _node(["$steps.a.items", "$steps.b.items"]), state=state
    )

    assert len(values) == 2
    assert _posted_captures(tmp_path, values, tokens) == [(_FEED_A,), (_FEED_B,)]


def test_the_exact_lineage_path_of_a_bundle_cites_its_own_source(tmp_path: Path) -> None:
    state = _bundle(_two_lists())

    values, tokens = _inbox_items(_node("$steps.bundle.items"), state=state)  # type: ignore[arg-type]

    assert len(values) == 1
    assert _posted_captures(tmp_path, values, tokens) == [(_FEED_A,)]


def test_the_run_byte_bound_counts_every_inbox_terminal_of_the_run() -> None:
    from cruxible_client.contracts.line_inbox import INBOX_ITEM_MAX_BYTES, INBOX_RUN_MAX_BYTES
    from cruxible_core.procedures.execution import _check_inbox_bytes

    state = SimpleNamespace(inbox_bytes=INBOX_RUN_MAX_BYTES - 10)
    fits = ["x" * 6]  # 8 canonical bytes

    assert _check_inbox_bytes(fits, state=state, node_id="inbox") == 8  # type: ignore[arg-type]
    with pytest.raises(_RunRefusal) as refused:
        _check_inbox_bytes(["x" * 9], state=state, node_id="inbox")  # type: ignore[arg-type]
    assert refused.value.refusal.code == "inbox_item_over_budget"
    assert refused.value.refusal.details["bound"] == "run"
    assert INBOX_ITEM_MAX_BYTES < INBOX_RUN_MAX_BYTES
