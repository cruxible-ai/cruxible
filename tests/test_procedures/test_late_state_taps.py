"""State taps run in dataflow order (D-R38).

A read whose selection names only invocation input, params, literals and other
admission reads binds at admission, exactly as before: its run identity is
byte-identical. A read fed by runtime output (a late read) runs when its input
is ready, at the run's pinned accepted coordinate; it enters the run's RESULT
digest, never its admission; it draws on what is left of the run's one read
ceiling; and its refusal is the run's refusal, at its node.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.procedures.graph import late_state_taps
from cruxible_client.contracts.procedures.models import ProjectNode, ReadNode
from cruxible_client.contracts.query.grammar import QueryBudgets
from cruxible_core.procedures.execution import ProcedureExecutor, StateTapReadResultV1
from cruxible_core.storage.cas import BodyAccessContext
from tests.test_procedures import test_procedure_execution as base

# Executor-level fixtures: a computed project feeds the late read (D-R37 h1
# closes ``project`` at authoring; the executor still runs it).
pytestmark = pytest.mark.executor_only

CLAIM_TYPE = base._pin("claim-type", "ClaimType", "test.next")
CONTRACT_IN = base._pin("contract-in", "Contract", "input/v1")
CONTRACT_OUT = base._pin("contract-out", "Contract", "output/v1")


class _Reader:
    """Each Claim read names the next Subject; ``rows`` per read, or a refusal."""

    def __init__(self, *, rows: int = 1, refuse: str | None = None, suffix: str = "") -> None:
        self.rows = rows
        self.refuse = refuse
        self.suffix = suffix
        self.reads: list[tuple[str, object, int]] = []

    def read_accepted_claim(  # type: ignore[no-untyped-def]
        self, *, claim_type, subject_kind, subject_id, cardinality, limit, coordinate, ceiling
    ):
        del claim_type, subject_kind, cardinality, limit
        self.reads.append((subject_id, coordinate, ceiling))
        if self.refuse is not None and subject_id.startswith(self.refuse):
            raise ValueError("the reader refused this Subject")
        return StateTapReadResultV1(
            # ``suffix`` changes only what a later read (of a "-next" Subject) sees.
            value={
                "subject": subject_id,
                "next": subject_id + "-next" + (self.suffix if "-next" in subject_id else ""),
            },
            effective_budgets=QueryBudgets(max_results=max(1, ceiling), max_traversal_depth=0),
            rows=self.rows,
        )


def _read(node_id: str, subject: str) -> ReadNode:
    return ReadNode(
        node_id=node_id,
        claim_type=base.named(CLAIM_TYPE),
        subject_kind="test.item",
        subject_id=subject,
        as_=node_id,
    )


def _procedure(*, late: bool, read_rows: int | None = None) -> Any:
    """``a`` (admission) -> [``p`` computed] -> ``b`` (admission, or late off ``p``)."""

    nodes: list[Any] = [_read("a", "$input.value")]
    if late:
        nodes.append(ProjectNode(node_id="p", fields={"next": "$steps.a.next"}, as_="p"))
        nodes.append(_read("b", "$steps.p.next"))
    else:
        nodes.append(_read("b", "$steps.a.next"))
    nodes.append(
        ProjectNode(node_id="result", fields={"a": "$steps.a", "b": "$steps.b"}, as_="result")
    )
    caps = (
        base._hard_caps()
        if read_rows is None
        else base.procedure_hard_caps(
            preset="execution", max_provider_calls=0, max_items=100, max_read_rows=read_rows
        )
    )
    definition = base.procedure_definition(
        name="late-chain" if late else "admission-chain",
        contract_in=base.named(CONTRACT_IN),
        contract_out=base.named(CONTRACT_OUT),
        nodes=tuple(nodes),
        hard_caps=caps,
    )
    return base._accepted(definition, pins=(CONTRACT_IN, CONTRACT_OUT, CLAIM_TYPE))


def _execute(tmp_path: Path, accepted: Any, reader: _Reader, *, with_reader: bool = True) -> Any:
    tmp_path.mkdir(parents=True, exist_ok=True)
    fixture = base._fixture(tmp_path)
    prepared = base._prepare(accepted, fixture, reader, invocation_input={"value": "wi-1"})
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=base._Authority(accepted.artifact_digest),
        contract_validator=base._Contracts(),
        state_reader=reader if with_reader else None,
    ).execute(prepared, accepted)
    stored = fixture.journal.all_records(fixture.stream, prepared.admission.journal_partition_id)
    final = base.parse_journal_payload(
        fixture.bodies.read(
            stored[-1].record.payload_digest,
            access=BodyAccessContext(principal_id="test", can_read_body=True),
        )
    )
    return prepared, result, final


#: The admission-chain Procedure's identity and result, pinned before D-R38: a
#: read fed only by input and an admission read keeps every byte of both.
_ADMISSION_RUN_ID = "RUN-4b1cabd8ac394b55f2b9ed312a7a872bf8cdc2cc6116e29c1f21346cce27087a"
_ADMISSION_REPLAY_KEY = "sha256:c9522541c309935deacbed8f8d0d9337ee9ba1242867b0636ab7e6f7792d8a30"
_ADMISSION_RESULT = "sha256:c0cfc12aacfd6efa827d9d235ba42020d5cdd6588d8fc4a34b46037397734cbf"


def test_an_existing_procedures_reads_bind_at_admission_byte_for_byte(tmp_path: Path) -> None:
    accepted = _procedure(late=False)
    assert late_state_taps(accepted.procedure.definition) == frozenset()
    reader = _Reader()
    prepared, result, final = _execute(tmp_path, accepted, reader)
    assert [item.input_name for item in prepared.admission.accepted_state_inputs] == ["a", "b"]
    assert prepared.admission.run_id == _ADMISSION_RUN_ID
    assert prepared.admission.semantic_replay_key_digest == _ADMISSION_REPLAY_KEY
    assert result.status == "succeeded"
    assert final["semantic_result_digest"] == _ADMISSION_RESULT
    assert "late_reads" not in final
    # Both reads ran at admission, none in the run.
    assert len(reader.reads) == 2


def test_a_read_fed_by_runtime_output_runs_late_and_enters_the_result_digest(
    tmp_path: Path,
) -> None:
    accepted = _procedure(late=True)
    assert late_state_taps(accepted.procedure.definition) == frozenset({"b"})
    reader = _Reader()
    prepared, result, final = _execute(tmp_path / "one", accepted, reader)
    assert result.status == "succeeded", result.refusal
    # Only the admission read is admitted; the late read is the run's own record.
    assert [item.input_name for item in prepared.admission.accepted_state_inputs] == ["a"]
    (late,) = final["late_reads"]
    assert late["input_name"] == "b"
    assert late["read_coordinate"] == prepared.admission.accepted_coordinate.model_dump(mode="json")
    assert final["output"]["b"] == {"subject": "wi-1-next", "next": "wi-1-next-next"}
    # The late read ran at the admitted coordinate, under what admission left.
    assert [(subject, coordinate) for subject, coordinate, _ in reader.reads] == [
        ("wi-1", prepared.admission.accepted_coordinate),
        ("wi-1-next", prepared.admission.accepted_coordinate),
    ]
    assert final["budget"]["observed"]["read_rows"] == 2

    # The same admission, a different late result: same run, a different result digest.
    other_prepared, other, other_final = _execute(tmp_path / "two", accepted, _Reader(suffix="x"))
    assert other.status == "succeeded"
    assert other_prepared.admission.run_id == prepared.admission.run_id
    assert other_final["semantic_result_digest"] != final["semantic_result_digest"]


@pytest.mark.parametrize(("read_rows", "refused"), [(2, False), (1, True)])
def test_a_late_read_draws_on_what_admission_left_and_refuses_past_the_ceiling(
    tmp_path: Path, read_rows: int, refused: bool
) -> None:
    # One row each: the admission read leaves read_rows - 1 for the late read.
    accepted = _procedure(late=True, read_rows=read_rows)
    reader = _Reader()
    _prepared, result, final = _execute(tmp_path, accepted, reader)
    if not refused:
        assert result.status == "succeeded", result.refusal
        assert reader.reads[-1][2] == 1  # the ceiling the late read was handed
        assert final["budget"]["observed"]["read_rows"] == 2
        return
    assert result.status == "refused"
    assert result.refusal.code == "read_ceiling_exceeded"
    assert result.refusal.node_id == "b"
    assert final["refusal"]["node_id"] == "b"
    assert final["output"] is None


def test_a_late_reads_refusal_is_the_runs_refusal_at_its_node(tmp_path: Path) -> None:
    accepted = _procedure(late=True)
    _prepared, result, _final = _execute(tmp_path, accepted, _Reader(refuse="wi-1-next"))
    assert result.status == "refused"
    assert result.refusal.code == "state_tap_refused"
    assert result.refusal.node_id == "b"


def test_a_late_read_without_the_runs_reader_fails_closed(tmp_path: Path) -> None:
    accepted = _procedure(late=True)
    _prepared, result, final = _execute(tmp_path, accepted, _Reader(), with_reader=False)
    assert result.status == "failed"
    assert final["output"] is None


def test_a_late_read_may_name_an_earlier_late_read() -> None:
    definition = _procedure(late=True).procedure.definition
    nodes = (*definition.nodes[:-1], _read("c", "$steps.b.next"), definition.nodes[-1])
    chained = definition.model_copy(update={"nodes": nodes})
    assert late_state_taps(chained) == frozenset({"b", "c"})
