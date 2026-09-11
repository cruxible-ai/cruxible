"""The node digest memo and the keyed replay index reproduce the kernels exactly."""

from __future__ import annotations

from datetime import timedelta

from cruxible_client.contracts.procedures.graph import (
    compute_procedure_node_digests_v3,
    compute_procedure_node_digests_v4,
)
from cruxible_core.procedures import graph_digests
from cruxible_core.procedures.readings import (
    append_procedure_reading,
    build_procedure_reading,
    procedure_reading_partition_id,
    reading_replay_key,
)
from cruxible_core.procedures.resolution import derive_resolution_activations
from tests.test_indexes.test_resolution_contracts import (
    NOW,
    _accepted,
    _accepted_v4,
    _actor,
    _coordinate,
)
from tests.test_procedures.test_procedure_readings import _writer


def test_node_digest_memo_reproduces_the_graph_law_for_v3_and_v4() -> None:
    graph_digests.clear_node_digest_memo()
    for accepted in (_accepted(), _accepted_v4()):
        definition = accepted.procedure.definition
        direct = (
            compute_procedure_node_digests_v3(definition)
            if definition.graph_format == 3
            else compute_procedure_node_digests_v4(definition)
        )
        cold = graph_digests.cached_node_digests(
            definition, definition_digest=accepted.procedure.definition_digest
        )
        warm = graph_digests.cached_node_digests(
            definition, definition_digest=accepted.procedure.definition_digest
        )
        assert dict(cold) == direct and warm is cold
        first = derive_resolution_activations(
            accepted, accepted_coordinate=_coordinate(), activated_at=NOW
        )
        graph_digests.clear_node_digest_memo()
        second = derive_resolution_activations(
            accepted, accepted_coordinate=_coordinate(), activated_at=NOW
        )
        assert first == second


def test_node_digest_memo_is_bounded_and_keyed_on_the_exact_digest() -> None:
    graph_digests.clear_node_digest_memo()
    accepted = _accepted()
    for index in range(graph_digests.NODE_DIGEST_MEMO_CAPACITY + 5):
        graph_digests.cached_node_digests(
            accepted.procedure.definition, definition_digest=f"sha256:{index:064x}"
        )
    assert len(graph_digests._memo) == graph_digests.NODE_DIGEST_MEMO_CAPACITY  # noqa: SLF001
    assert "sha256:" + "0" * 64 not in graph_digests._memo  # noqa: SLF001
    graph_digests.clear_node_digest_memo()


def test_keyed_replay_index_returns_the_same_record_as_the_partition_scan(tmp_path) -> None:
    accepted = _accepted()
    partition_id = procedure_reading_partition_id(accepted)
    journal, bodies, stream, writer = _writer(tmp_path, partition_id=partition_id)
    reading = build_procedure_reading(
        accepted,
        accepted_coordinate=_coordinate(),
        subject_grain="procedure_unit",
        grade="observation",
        verdict="satisfied",
        observed_at=NOW,
        recorded_at=NOW,
        actor_context=_actor(),
        value={"count": 1},
        idempotency_key="reading-index",
    )
    stored = append_procedure_reading(
        writer,
        reading=reading,
        accepted=accepted,
        accepted_coordinate=_coordinate(),
        stream=stream,
        bodies=bodies,
    )
    key = reading_replay_key(reading)
    assert key is not None
    scanned = append_procedure_reading(
        writer,
        reading=reading,
        accepted=accepted,
        accepted_coordinate=_coordinate(),
        stream=stream,
        bodies=bodies,
    )
    indexed = append_procedure_reading(
        writer,
        reading=reading,
        accepted=accepted,
        accepted_coordinate=_coordinate(),
        stream=stream,
        bodies=bodies,
        replay_index={key: (stored, reading)},
    )
    assert scanned == stored == indexed
    assert len(journal.all_records(stream, partition_id)) == 1

    later = reading.model_copy(update={"recorded_at": NOW + timedelta(seconds=1)})
    later_key = reading_replay_key(later)
    assert later_key == key
