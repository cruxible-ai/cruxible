"""Metadata-only lookup microbenchmark; never publishes synthetic generations."""

import ast
import json
import statistics
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from tests.test_playbill._support import initialize_local

from cruxible_core.playbill import instance as module

source = subprocess.check_output(
    ["git", "show", "534da597:src/cruxible_core/playbill/instance.py"], text=True
)
cls = next(
    n
    for n in ast.parse(source).body
    if isinstance(n, ast.ClassDef) and n.name == "PlaybillInstance"
)
method = next(
    n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "coordinate_for_oid"
)
namespace = dict(vars(module))
exec(
    compile(ast.Module(body=[method], type_ignores=[]), "<baseline-coordinate-for-oid>", "exec"),
    namespace,
)
baseline = namespace["coordinate_for_oid"]
rows = []
with tempfile.TemporaryDirectory(prefix="history-lookup-microbench-") as tmp:
    instance, _ = initialize_local(Path(tmp))
    recovered = instance._recovered
    for count in (27, 100, 1000, 10000):
        history = tuple(replace(recovered.head, oid=f"{i:064x}", sequence=i) for i in range(count))
        epoch = replace(recovered, history=history, head=history[-1])
        instance._recovered = epoch
        ids = (history[0].oid, history[count // 2].oid, history[-1].oid)
        # Exact output parity, including full coordinate construction/path checks.
        for oid in ids:
            assert baseline(instance, oid) == instance.coordinate_for_oid(oid)
        cold = []
        for _ in range(7):
            instance._history_lookup = None
            start = time.perf_counter()
            instance.coordinate_for_oid(ids[-1])
            cold.append(time.perf_counter() - start)
        before, after = [], []
        iterations = 999
        for _ in range(7):
            for reader, samples in (
                (baseline, before),
                (module.PlaybillInstance.coordinate_for_oid, after),
            ):
                start = time.perf_counter()
                for i in range(iterations):
                    reader(instance, ids[i % 3])
                samples.append((time.perf_counter() - start) / iterations)
        rows.append(
            {
                "generations": count,
                "iterations_per_sample": iterations,
                "samples": 7,
                "baseline_seconds_per_lookup": statistics.median(before),
                "indexed_warm_seconds_per_lookup": statistics.median(after),
                "indexed_first_lookup_seconds": statistics.median(cold),
                "exact_coordinate_parity": True,
            }
        )
result = {
    "baseline_commit": "534da597",
    "scope": (
        "Lookup-only synthetic recovered generation metadata in temporary initialized instance; "
        "same history for before/after; includes unchanged strict path resolution and full "
        "coordinate construction; excludes recovery, Git blob reads, HTTP, projection checking. "
        "Synthetic generations never published."
    ),
    "rows": rows,
}
Path("/tmp/accepted-history-lookup-benchmark.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
