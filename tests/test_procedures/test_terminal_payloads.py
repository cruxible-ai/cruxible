"""A claims terminal's journal records cite their per-item parts as run-free bodies."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.errors import CasError
from cruxible_core.exhaust.payloads import UnavailablePayload
from cruxible_core.procedures.terminal_payloads import (
    TerminalPayloadError,
    join_terminal_egress_payload,
    join_terminal_egress_read,
    split_terminal_egress_payload,
)


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


class _Store:
    def __init__(self) -> None:
        self.held: dict[str, bytes] = {}
        self.writes = 0

    def store(self, content: bytes) -> str:
        self.writes += 1
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        self.held[digest] = content
        return digest

    def read(self, digest: str, access: object = None) -> bytes:
        if digest not in self.held:
            raise CasError("missing")
        return self.held[digest]


def _children(count: int) -> list[dict[str, Any]]:
    return [
        {
            "tag": "playbill-terminal-child-receipt-v2",
            "child_index": index,
            "item_key": f"{index:08d}.{'a' * 32}",
            "manifest_digest": _digest(f"manifest-{index}"),
        }
        for index in range(count)
    ]


def _prepared(count: int, *, run_id: str = "run-1") -> dict[str, Any]:
    children = _children(count)
    receipts = {
        child["item_key"]: {"value": {"capture_digest": _digest(f"capture-{index}")}}
        for index, child in enumerate(children)
    }
    # The last item is a Subject item: no receipt, its Capture proven elsewhere.
    subject_key = children[-1]["item_key"]
    receipts.pop(subject_key)
    evidence = {
        child["item_key"]: _digest(f"capture-{index}") for index, child in enumerate(children)
    }
    return {
        "node_id": "claims",
        "kind": "claims",
        "required_rung": 2,
        "items_record_digest": _digest(f"items-{run_id}"),
        "children": children,
        "verdict": "prepared",
        "operation_key": f"op-{run_id}",
        "request": {
            "run_id": run_id,
            "prepared_at": "2026-11-16T00:00:00Z",
            "items": [
                {"child_index": c["child_index"], "item_key": c["item_key"], "value": {}}
                for c in children
            ],
            "claims_changes": {c["item_key"]: {"kind": "claim"} for c in children},
            "claims_evidence": receipts,
        },
        "prepared": {
            "target_paths": ["claims/a"],
            "lowering_digest": _digest("lowering"),
            "item_paths": [[c["item_key"], f"claims/{c['child_index']}"] for c in children],
        },
        "evidence": evidence,
    }


def test_split_then_join_restores_the_exact_record_and_shares_unchanged_bodies() -> None:
    store = _Store()
    original = _prepared(5)
    split = split_terminal_egress_payload(original, store=store.store)
    for key in ("children", "evidence"):
        assert key not in split
    assert "items" not in split["request"] and "item_paths" not in split["prepared"]
    # Only the Subject item's Capture is journaled: the rest are its receipt's.
    assert list(split["evidence_overrides"]) == [original["children"][-1]["item_key"]]
    assert join_terminal_egress_payload(split, store.read) == original
    # A second run over the same items stores the same bodies: nothing new.
    held = dict(store.held)
    again = split_terminal_egress_payload(_prepared(5, run_id="run-2"), store=store.store)
    assert store.held == held
    assert again["children_digest"] == split["children_digest"]
    # The run header stays inline, run-specific.
    assert again["request"]["run_id"] == "run-2"
    for body in store.held.values():
        assert b"run-1" not in body and b"run-2" not in body
    # A delivered record's receipt children are a body too; its child list is
    # the same body as the prepared record's.
    delivered = {
        key: value
        for key, value in original.items()
        if key not in {"request", "prepared", "evidence"}
    }
    delivered |= {
        "verdict": "delivered",
        "receipt": {"tag": "playbill-terminal-egress-receipt-v2", "children": _children(5)},
    }
    split_delivered = split_terminal_egress_payload(delivered, store=store.store)
    assert split_delivered["children_digest"] == split["children_digest"]
    assert "children" not in split_delivered["receipt"]
    assert join_terminal_egress_payload(split_delivered, store.read) == delivered


def test_absence_receipts_are_held_once_without_the_run_id_and_restore_exactly() -> None:
    """Round 9 (K): every reconfirmed absence of a template carries the same
    receipt, whose witness names the run. The request items body holds each
    distinct receipt once and leaves the witness's run id to the record's run
    header, so an unchanged terminal's items are the body stored before."""

    import json

    def absence_record(run_id: str) -> dict[str, Any]:
        record = _prepared(4, run_id=run_id)
        witness = {"run_id": run_id, "capture": _digest("root"), "node_id": "claims"}
        record["request"]["claims_evidence"] = {
            child["item_key"]: {
                "absence": witness,
                "value": {"basis": "capture", "capture_digest": _digest("root")},
            }
            for child in record["children"]
        }
        record["evidence"] = {child["item_key"]: _digest("root") for child in record["children"]}
        return record

    store = _Store()
    first = absence_record("run-1")
    split = split_terminal_egress_payload(first, store=store.store)
    assert join_terminal_egress_payload(split, store.read) == first
    body = json.loads(store.held[split["request"]["items_digest"]])
    assert body["tag"] == "playbill-terminal-request-items-v2"
    assert len(body["receipts"]) == 1 and "run_id" not in body["receipts"][0]["absence"]
    held = dict(store.held)
    second = absence_record("run-2")
    again = split_terminal_egress_payload(second, store=store.store)
    assert store.held == held  # the same items body: nothing new
    assert join_terminal_egress_payload(again, store.read) == second
    # A receipt naming another run keeps its run id in the body.
    other = absence_record("run-3")
    key = next(iter(other["request"]["claims_evidence"]))
    other["request"]["claims_evidence"][key] = {
        **other["request"]["claims_evidence"][key],
        "absence": {"run_id": "run-0", "capture": _digest("root"), "node_id": "claims"},
    }
    assert (
        join_terminal_egress_payload(
            split_terminal_egress_payload(other, store=store.store), store.read
        )
        == other
    )


def test_other_terminals_and_inline_history_pass_through() -> None:
    store = _Store()
    capture = {"node_id": "emit", "kind": "emit_capture", "children": _children(2)}
    assert split_terminal_egress_payload(capture, store=store.store) == capture
    assert store.writes == 0
    inline = _prepared(2)
    assert join_terminal_egress_payload(inline, store.read) == inline


def test_a_split_record_whose_body_is_lost_reads_unavailable() -> None:
    store = _Store()
    split = split_terminal_egress_payload(_prepared(3), store=store.store)
    lost = split["children_digest"]
    store.held.pop(lost)
    access = BodyAccessContext(principal_id="test", can_read_body=True)
    assert join_terminal_egress_read(split, store, access=access) == UnavailablePayload(lost)
    # A body whose bytes are not the canonical body it names is refused.
    store.held[lost] = b"{}"
    with pytest.raises(TerminalPayloadError):
        join_terminal_egress_payload(split, store.read)


def test_a_derived_source_request_cites_its_value_as_a_run_free_body() -> None:
    from cruxible_core.procedures.terminal_payloads import (
        join_derived_source_request,
        split_derived_source_request,
    )

    store = _Store()
    payload = {
        "tag": "playbill-procedure-derived-source-request-v1",
        "run_id": "run-1",
        "occurrence_path": "source/osv",
        "request": {"purls": [f"pkg:pypi/p{index}@1" for index in range(50)]},
        "request_digest": _digest("request-run-1"),
    }
    split = split_derived_source_request(payload, store=store.store)
    assert "request" not in split and split["occurrence_path"] == "source/osv"
    assert join_derived_source_request(split, store.read) == payload
    # Another run asking the same thing stores nothing new.
    held = dict(store.held)
    again = split_derived_source_request(
        {**payload, "run_id": "run-2", "request_digest": _digest("request-run-2")},
        store=store.store,
    )
    assert store.held == held and again["request_body_digest"] == split["request_body_digest"]
    # A record written inline before the split passes through.
    assert join_derived_source_request(payload, store.read) == payload
