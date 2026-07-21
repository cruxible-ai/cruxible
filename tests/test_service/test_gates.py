"""Service receipts for atomic declared-gate evaluation."""

from __future__ import annotations

from pathlib import Path
from threading import Event, Thread

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance
from cruxible_core.service import gates as gates_service
from cruxible_core.service import service_evaluate_gate

GATE_CONFIG_YAML = """\
version: "1.0"
name: gate_receipt_test
entity_types:
  ReviewRequest:
    properties:
      review_request_id: {type: string, primary_key: true}
      status: {type: string, enum: [requested, approved]}
      change_head: {type: string}
relationships: []
gates:
  merge-review:
    kind: git-pre-push
    entity_type: ReviewRequest
    match_property: change_head
    condition: {status: approved}
    adapter: {branch_pattern: refs/heads/main}
  action-review:
    kind: generic
    entity_type: ReviewRequest
    match_property: change_head
    condition: {status: approved}
"""

PIN_A = "a" * 40
PIN_B = "b" * 40
PIN_C = "c" * 40


def _review(review_id: str, status: str, change_head: str) -> EntityInstance:
    return EntityInstance(
        entity_type="ReviewRequest",
        entity_id=review_id,
        properties={
            "review_request_id": review_id,
            "status": status,
            "change_head": change_head,
        },
    )


def _graph(*reviews: EntityInstance) -> EntityGraph:
    graph = EntityGraph()
    for review in reviews:
        graph.add_entity(review)
    return graph


@pytest.fixture
def gate_instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(GATE_CONFIG_YAML)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    instance.save_graph(
        _graph(
            _review("RR-2", "approved", PIN_A),
            _review("RR-1", "approved", PIN_A),
            _review("RR-3", "requested", PIN_B),
        )
    )
    return instance


def _persisted_receipt(instance: CruxibleInstance, receipt_id: str):
    store = instance.get_receipt_store()
    try:
        receipt = store.get_receipt(receipt_id)
    finally:
        store.close()
    assert receipt is not None
    return receipt


def test_satisfied_evaluation_receipt_has_full_payload_and_satisfier_ids(
    gate_instance: CruxibleInstance,
) -> None:
    revision = gate_instance.get_read_revision()

    result = service_evaluate_gate(
        gate_instance,
        instance_id="inst_gate",
        gate_name="merge-review",
        candidates=[PIN_A],
    )

    assert result.verdict == "satisfied"
    assert result.read_revision == revision == gate_instance.get_read_revision()
    assert result.candidate_outcomes[0].satisfying_entity_ids == ["RR-1", "RR-2"]
    receipt = _persisted_receipt(gate_instance, result.receipt_id)
    expected_outcomes = [
        {
            "candidate": PIN_A,
            "satisfied": True,
            "satisfying_entity_ids": ["RR-1", "RR-2"],
        }
    ]
    assert receipt.operation_type == "gate_evaluation"
    assert receipt.query_name == "merge-review"
    assert receipt.committed is True
    assert receipt.parameters == {
        "instance_id": "inst_gate",
        "read_revision": revision,
        "gate_name": "merge-review",
        "kind": "git-pre-push",
        "candidates": [PIN_A],
        "candidate_outcomes": expected_outcomes,
        "verdict": "satisfied",
        "reason": None,
    }
    assert receipt.results == expected_outcomes
    assert receipt.nodes[0].node_type == "gate_evaluation"
    assert receipt.nodes[0].detail == {
        "gate_name": "merge-review",
        "parameters": receipt.parameters,
        "verdict": "satisfied",
        "reason": None,
    }
    assert sorted(
        node.entity_id for node in receipt.nodes if node.node_type == "entity_lookup"
    ) == ["RR-1", "RR-2"]


def test_unsatisfied_evaluation_receipt_records_nothing_satisfied_candidate(
    gate_instance: CruxibleInstance,
) -> None:
    result = service_evaluate_gate(
        gate_instance,
        instance_id="inst_gate",
        gate_name="merge-review",
        candidates=[PIN_A, PIN_B, PIN_C],
    )

    assert result.verdict == "unsatisfied"
    assert [outcome.satisfied for outcome in result.candidate_outcomes] == [True, False, False]
    assert result.receipt.parameters["candidate_outcomes"][1:] == [
        {"candidate": PIN_B, "satisfied": False, "satisfying_entity_ids": []},
        {"candidate": PIN_C, "satisfied": False, "satisfying_entity_ids": []},
    ]


@pytest.mark.parametrize(
    ("candidates", "error_reason", "expected_reason"),
    [
        (
            [],
            None,
            "generic gate received no candidate values; pass --candidate VALUE "
            "(repeatable) or pipe one candidate per line",
        ),
        ([PIN_A], "malformed pre-push stdin", "malformed pre-push stdin"),
    ],
)
def test_error_verdict_persists_refusal_receipt(
    gate_instance: CruxibleInstance,
    candidates: list[str],
    error_reason: str | None,
    expected_reason: str,
) -> None:
    gate_name = "action-review" if error_reason is None else "merge-review"
    revision = gate_instance.get_read_revision()

    result = service_evaluate_gate(
        gate_instance,
        instance_id="inst_gate",
        gate_name=gate_name,
        candidates=candidates,
        error_reason=error_reason,
    )

    assert result.verdict == "error"
    assert result.reason == expected_reason
    receipt = _persisted_receipt(gate_instance, result.receipt_id)
    assert receipt.parameters == {
        "instance_id": "inst_gate",
        "read_revision": revision,
        "gate_name": gate_name,
        "kind": "generic" if error_reason is None else "git-pre-push",
        "candidates": candidates,
        "candidate_outcomes": [],
        "verdict": "error",
        "reason": expected_reason,
    }
    assert receipt.results == []
    assert receipt.committed is True


def test_mutation_between_candidates_cannot_split_read_revision(
    gate_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent writer waits until the composite evaluation transaction ends."""
    revision_before = gate_instance.get_read_revision()
    writer_started = Event()
    writer_errors: list[BaseException] = []
    writer: Thread | None = None
    replacement = _graph(
        _review("RR-1", "approved", PIN_A),
        _review("RR-B", "approved", PIN_B),
    )
    real_satisfying_entity_ids = gates_service._satisfying_entity_ids
    calls = 0

    def write_new_pin() -> None:
        writer_started.set()
        try:
            CruxibleInstance.load(gate_instance.root).save_graph(replacement)
        except BaseException as exc:  # pragma: no cover - asserted below
            writer_errors.append(exc)

    def interleaved_satisfying_entity_ids(*args, **kwargs):
        nonlocal calls, writer
        outcome = real_satisfying_entity_ids(*args, **kwargs)
        calls += 1
        if calls == 1:
            writer = Thread(target=write_new_pin)
            writer.start()
            assert writer_started.wait(timeout=1)
        return outcome

    monkeypatch.setattr(
        gates_service,
        "_satisfying_entity_ids",
        interleaved_satisfying_entity_ids,
    )

    result = service_evaluate_gate(
        gate_instance,
        instance_id="inst_gate",
        gate_name="merge-review",
        candidates=[PIN_A, PIN_B],
    )
    assert writer is not None
    writer.join(timeout=5)

    assert not writer.is_alive()
    assert writer_errors == []
    assert result.read_revision == revision_before
    assert [outcome.satisfied for outcome in result.candidate_outcomes] == [True, False]
    assert gate_instance.get_read_revision() == revision_before + 1

    after = service_evaluate_gate(
        gate_instance,
        instance_id="inst_gate",
        gate_name="merge-review",
        candidates=[PIN_B],
    )
    assert after.verdict == "satisfied"
    assert after.read_revision == revision_before + 1
