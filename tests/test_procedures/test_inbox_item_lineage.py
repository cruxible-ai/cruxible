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
from cruxible_client.contracts.shapes import Contract, contract_digest
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

#: The inbox item every terminal here posts: one rationale.
ITEM = Contract.model_validate(
    {"name": "test.inbox-item", "version": 1, "fields": {"rationale": {"type": "string"}}}
)


def _node(fields: dict[str, object], *, for_each: str | None = None) -> InboxEgressNode:
    return InboxEgressNode.model_validate(
        {
            "node_id": "inbox",
            "contract": {"name": ITEM.name, "version": 1, "digest": contract_digest(ITEM).tagged},
            "fields": fields,
            **({} if for_each is None else {"for_each": for_each}),
        }
    )


def _each(path: str) -> InboxEgressNode:
    """One item per element of ``path``, posting its rationale."""

    return _node({"rationale": "$item.rationale"}, for_each=path)


def _items(node: InboxEgressNode, state: object):  # type: ignore[no-untyped-def]
    return _inbox_items(node, state=state, contract=ITEM, resolve=lambda _ref: None)  # type: ignore[arg-type]


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
        inbox_dedupe_key=(),
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

    values, tokens = _items(_each("$steps.joined.items"), state)

    assert len(values) == 2
    assert _posted_captures(tmp_path, values, tokens) == [(_FEED_A,), (_FEED_B,)]


def test_a_two_capture_list_without_lineage_refuses_instead_of_lending_both() -> None:
    state = _two_capture_state(lambda a, b: None)

    with pytest.raises(_RunRefusal) as refused:
        _items(_each("$steps.joined.items"), state)

    assert refused.value.refusal.code == "inbox_item_evidence_ambiguous"
    assert refused.value.refusal.details == {"field": "for_each", "reason": "lineage_unavailable"}


@pytest.mark.parametrize("path", ["$steps.bundle.other", "$steps.bundle.items.0.nested"])
def test_a_sibling_or_nested_path_does_not_borrow_the_top_level_lineage(path: str) -> None:
    from tests.test_procedures.test_proposal_fan_out import _element

    state = _bundle(_two_lists())
    state.outputs["bundle"]["items"][0]["nested"] = [_element("nested")]

    with pytest.raises(_RunRefusal) as refused:
        _items(_each(path), state)

    assert refused.value.refusal.code == "inbox_item_evidence_ambiguous"


def test_a_field_reading_one_lists_element_cites_only_that_lists_capture(
    tmp_path: Path,
) -> None:
    state = _bundle(_two_lists())

    values, tokens = _items(_node({"rationale": "$steps.a.items.0.rationale"}), state)

    assert values == [{"rationale": "Severity as the feed reports it (from-a)."}]
    assert _posted_captures(tmp_path, values, tokens) == [(_FEED_A,)]


def test_the_exact_lineage_path_of_a_bundle_cites_its_own_source(tmp_path: Path) -> None:
    state = _bundle(_two_lists())

    values, tokens = _items(_each("$steps.bundle.items"), state)

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


# --- an item's own fields select evidence by exact path ---------------------------
# (delta review F-001: the reviewer's three probes)


def test_a_field_reading_an_exact_items_path_keeps_only_its_captures(tmp_path: Path) -> None:
    """`bundle.items` holds A's records; its sibling `bundle.other` holds B's."""

    state = _bundle(_two_lists())

    values, tokens = _items(_node({"rationale": "$steps.bundle.items.0.rationale"}), state)

    assert values == [{"rationale": "Severity as the feed reports it (from-a)."}]
    assert _posted_captures(tmp_path, values, tokens) == [(_FEED_A,)]


def test_a_field_selecting_an_indexed_element_keeps_that_elements_lineage(
    tmp_path: Path,
) -> None:
    state = _two_capture_state(lambda a, b: (frozenset({a}), frozenset({b})))

    values, tokens = _items(_node({"rationale": "$steps.joined.items.1.rationale"}), state)

    assert _posted_captures(tmp_path, values, tokens) == [(_FEED_B,)]


def test_a_field_selecting_an_ambiguous_sibling_refuses() -> None:
    state = _bundle(_two_lists())

    with pytest.raises(_RunRefusal) as refused:
        _items(_node({"rationale": "$steps.bundle.other.0.rationale"}), state)

    assert refused.value.refusal.code == "inbox_item_evidence_ambiguous"
    assert refused.value.refusal.details == {"field": "fields", "reason": "lineage_unavailable"}


def test_a_whole_alias_reference_carries_every_capture_that_built_it(tmp_path: Path) -> None:
    """A legitimate multi-Capture closure is kept when the selected value is the whole alias."""

    state = _two_capture_state(lambda a, b: None)
    state.outputs["joined"] = "Both feeds report high severity."

    values, tokens = _items(_node({"rationale": "$steps.joined"}), state)

    assert _posted_captures(tmp_path, values, tokens) == [(_FEED_A, _FEED_B)]


def test_an_item_outside_its_contract_refuses_and_a_dedupe_key_posts_one(
    tmp_path: Path,
) -> None:
    """F13: items are typed by their Contract, and those sharing a dedupe_key post once."""

    state = _two_capture_state(lambda a, b: (frozenset({a}), frozenset({b})))
    with pytest.raises(_RunRefusal) as refused:
        _items(_node({"rationale": "$steps.joined.items.0.statement"}), state)
    assert refused.value.refusal.code == "inbox_item_invalid"
    assert refused.value.refusal.details == {"field": "fields", "reason": "contract_refused"}

    keyed = InboxEgressNode.model_validate(
        {
            **_each("$steps.joined.items").model_dump(mode="json", by_alias=True),
            "fields": {"rationale": "$item.statement.role"},
            "dedupe_key": ["rationale"],
        }
    )
    values, _tokens = _items(keyed, state)
    assert values == [{"rationale": "observation"}]
