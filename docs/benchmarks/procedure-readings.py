"""Phase timings for production measurement resolution and exact-grain readings.

Run from the repository root: ``uv run python docs/benchmarks/procedure-readings.py``.

The world is the served knowledge loop (two accepted Claims, one accepted
QueryDefinition, one guarded query-only Procedure). Each phase is timed
separately over fixed logical inputs with an explicit clock, at two measurement
batch sizes and two retained-reading history sizes, with a fresh-process
(memos cleared) sample and warm samples. Journal record counts and CAS body
reads are counted per phase where the seam exposes them. Nothing here is
published as accepted state; the temporary instance is discarded.

There is no "before" for production emission: the emitter did not exist. The
before/after columns cover the two local kernel optimizations only -- the node
digest memo behind grain computation, and the keyed replay index behind
``append_procedure_reading`` -- measured against the unmemoized paths.
"""

from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.test_playbill import test_procedure_measurement_readings as world  # noqa: E402

from cruxible_client.contracts.procedures.artifacts import (  # noqa: E402
    AcceptedProcedureV1,
    procedure_artifact_digest,
    procedure_path,
)
from cruxible_client.contracts.procedures.readings import (  # noqa: E402
    PlaybillProcedureMeasureRequestV1,
    PlaybillProcedureReadingsRequestV1,
)
from cruxible_core.playbill import cas as cas_module  # noqa: E402
from cruxible_core.playbill.procedures import graph_digests  # noqa: E402
from cruxible_core.playbill.procedures import readings as readings_module  # noqa: E402
from cruxible_core.playbill.procedures.resolution import (  # noqa: E402
    derive_resolution_activations,
)
from cruxible_core.service import playbill_measurements as measurements  # noqa: E402
from cruxible_core.service.playbill_procedure_runs import (  # noqa: E402
    ProcedureRunRequestV2,
    service_run_playbill_procedure,
)

SAMPLES = 5
OBSERVE_AT = world.OBSERVE_AT
RECORD_AT = world.RECORD_AT


class _Counter:
    """Count CAS body reads through the one read seam every phase shares."""

    def __init__(self) -> None:
        self.reads = 0
        self._original = cas_module.ContentAddressedBodyStore.read

    def __enter__(self) -> _Counter:
        counter = self

        def counting(store, digest, *args, **kwargs):  # type: ignore[no-untyped-def]
            counter.reads += 1
            return counter._original(store, digest, *args, **kwargs)

        cas_module.ContentAddressedBodyStore.read = counting  # type: ignore[method-assign]
        return self

    def __exit__(self, *_exc: object) -> None:
        cas_module.ContentAddressedBodyStore.read = self._original  # type: ignore[method-assign]


def _timed(fn, *, samples: int = SAMPLES):  # type: ignore[no-untyped-def]
    durations = []
    reads = []
    result = None
    for _ in range(samples):
        with _Counter() as counter:
            start = time.perf_counter()
            result = fn()
            durations.append(time.perf_counter() - start)
        reads.append(counter.reads)
    return {
        "median_seconds": statistics.median(durations),
        "min_seconds": min(durations),
        "max_seconds": max(durations),
        "samples": samples,
        "cas_reads_per_call": statistics.median(reads),
    }, result


def _clear_memos() -> None:
    graph_digests.clear_node_digest_memo()
    measurements._activation_memo.clear()  # noqa: SLF001
    measurements._reading_index_memo.clear()  # noqa: SLF001


def _measure(instance, procedure, *, run_id, names, minute):  # type: ignore[no-untyped-def]
    return measurements.service_measure_playbill_procedure(
        instance,
        name=procedure.identity.name,
        request=PlaybillProcedureMeasureRequestV1(
            run_id=run_id,
            measurement_names=tuple(sorted(names)),
            evaluation_time=OBSERVE_AT + timedelta(minutes=minute),
        ),
        actor_context=world._actor(instance),  # noqa: SLF001
        recorded_at=RECORD_AT + timedelta(minutes=minute),
    )


def _run(instance, procedure, *, minute):  # type: ignore[no-untyped-def]
    run = service_run_playbill_procedure(
        instance,
        name=procedure.identity.name,
        request=ProcedureRunRequestV2(
            evaluation_time=world.RUN_TIME + timedelta(minutes=minute), input={}
        ),
        actor_context=world._actor(instance),  # noqa: SLF001
    )
    assert run.status == "succeeded"
    return run


def _bench_batch(tmp: Path, *, names: tuple[str, ...], history: int) -> dict[str, object]:
    instance, _owner, procedure = world._world(tmp)  # noqa: SLF001
    accepted = AcceptedProcedureV1(
        path=procedure_path(procedure.identity.name),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    row: dict[str, object] = {"measurements": len(names), "retained_readings_before": history}

    # Zero-measurement baseline of the run path itself (no emitter involved).
    row["run_only"], _ = _timed(lambda: _run(instance, procedure, minute=0), samples=3)

    # Activation derivation: fresh (memos cleared) vs warm.
    observation = instance.accepted_coordinate()

    def derive() -> object:
        return measurements.measurement_activation_basis(
            instance, accepted=accepted, observation=observation
        )

    _clear_memos()
    row["activation_fresh"], _ = _timed(lambda: (_clear_memos(), derive())[1], samples=3)
    row["activation_warm"], _ = _timed(derive)

    # Resolution-only evaluation (no run named): evidence + law + append.
    _clear_memos()
    row["resolve_fresh_first_evaluation"], _ = _timed(
        lambda: _measure(instance, procedure, run_id=None, names=names, minute=0), samples=1
    )
    row["resolve_standing_answer_warm"], _ = _timed(
        lambda: _measure(instance, procedure, run_id=None, names=names, minute=1)
    )

    # Seed retained reading history with distinct runs.
    seed_runs = [_run(instance, procedure, minute=10 + i) for i in range(history)]
    for index, run in enumerate(seed_runs):
        _measure(instance, procedure, run_id=run.run_id, names=names[:1], minute=20 + index)

    # One new run credited: reading build + keyed index + append per measurement.
    fresh_run = _run(instance, procedure, minute=200)
    _clear_memos()
    row["credit_new_run_fresh"], _ = _timed(
        lambda: _measure(instance, procedure, run_id=fresh_run.run_id, names=names, minute=300),
        samples=1,
    )
    # Duplicate retry: every key stands, every reading replays.
    row["credit_same_run_retry_warm"], _ = _timed(
        lambda: _measure(instance, procedure, run_id=fresh_run.run_id, names=names, minute=301)
    )

    # Inspection over the retained history, fresh index vs warm index.
    def inspect() -> object:
        return measurements.service_list_playbill_procedure_readings(
            instance,
            name=procedure.identity.name,
            request=PlaybillProcedureReadingsRequestV1(limit=200),
            evaluation_time=RECORD_AT + timedelta(minutes=400),
        )

    _clear_memos()
    row["readings_list_fresh"], listed = _timed(lambda: (_clear_memos(), inspect())[1], samples=3)
    row["readings_list_warm"], _ = _timed(inspect)
    row["retained_readings_after"] = len(listed.readings)  # type: ignore[union-attr]

    # Kernel micro-benchmarks: node digest memo, and keyed replay index vs the
    # partition rescan inside append_procedure_reading.
    basis = measurements.measurement_activation_basis(
        instance, accepted=accepted, observation=observation
    )
    activations = derive_resolution_activations(
        accepted, accepted_coordinate=basis.coordinate, activated_at=basis.activated_at
    )

    def grain_direct() -> object:
        return graph_digests.compute_node_digests(accepted.procedure.definition)

    def grain_memo() -> object:
        return graph_digests.cached_node_digests(
            accepted.procedure.definition,
            definition_digest=accepted.procedure.definition_digest,
        )

    row["grain_digests_direct"], _ = _timed(grain_direct, samples=20)
    row["grain_digests_memo_warm"], _ = _timed(grain_memo, samples=20)

    journal, stream = measurements._journal(instance)  # noqa: SLF001
    partition = readings_module.procedure_reading_partition_id(accepted)
    index = measurements.reading_partition_index(
        instance, journal=journal, stream=stream, partition_id=partition
    )
    existing = next(iter(index.by_key.values()), None)
    if existing is not None:
        reading = existing.reading
        activation = next(
            item for item in activations if item.measurement_name == reading.measurement_name
        )
        writer = measurements._FencedWriter(instance, journal)  # noqa: SLF001
        writer.acquire(stream, partition)
        try:
            book = measurements._contract_state(  # noqa: SLF001
                instance, journal, stream, activation
            ).book

            def replay_scan() -> object:
                return readings_module.append_procedure_reading(
                    writer.writer,
                    reading=reading,
                    accepted=accepted,
                    accepted_coordinate=activation.subject.accepted_coordinate,
                    stream=stream,
                    bodies=instance.body_store(),
                    activations=(activation,),
                    resolution_book=book,
                )

            def replay_indexed() -> object:
                return readings_module.append_procedure_reading(
                    writer.writer,
                    reading=reading,
                    accepted=accepted,
                    accepted_coordinate=activation.subject.accepted_coordinate,
                    stream=stream,
                    bodies=instance.body_store(),
                    activations=(activation,),
                    resolution_book=book,
                    replay_index={
                        key: (entry.stored, entry.reading) for key, entry in index.by_key.items()
                    },
                )

            row["reading_replay_partition_rescan"], _ = _timed(replay_scan)
            row["reading_replay_keyed_index"], _ = _timed(replay_indexed)
        finally:
            writer.release()
    return row


def main() -> None:
    rows = []
    for names in (
        ("rows-present", "hot-claim"),
        (
            "rows-present",
            "hot-claim",
            "hot-arm-attested",
            "cold-arm-empty",
            "expired-early",
            "late-check",
        ),
    ):
        for history in (0, 40):
            with tempfile.TemporaryDirectory(prefix="procedure-readings-bench-") as tmp:
                rows.append(_bench_batch(Path(tmp), names=names, history=history))
    result = {
        "recorded_at": datetime.now(tz=UTC).isoformat(),
        "scope": (
            "Temporary knowledge-loop instance (2 Claims, 1 QueryDefinition, 1 guarded "
            "query-only Procedure); explicit clocks; phases timed in-process from the service "
            "layer, no HTTP; CAS body reads counted through the shared read seam; the "
            "synthetic instance is never published. No 'before' exists for production "
            "emission; before/after columns cover only the node digest memo and the keyed "
            "replay index."
        ),
        "rows": rows,
    }
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
