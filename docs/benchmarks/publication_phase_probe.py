"""Diagnostic only: wall-clock nested phase accounting for served acceptance."""

import functools
import json
import threading
import time
from pathlib import Path


def install(prefix):
    from cruxible_core.playbill import (
        assembler,
        git,
        instance,
        projection_delta,
        recovery,
        settlement,
        workspace_advertisement,
    )
    from cruxible_core.runtime import playbill_api
    from cruxible_core.storage import playbill_projection

    local = threading.local()

    def traced(owner, name, label):
        original = getattr(owner, name)

        @functools.wraps(original)
        def call(*args, **kwargs):
            counters = getattr(local, "counters", None)
            if counters is None:
                return original(*args, **kwargs)
            start = time.perf_counter_ns()
            try:
                return original(*args, **kwargs)
            finally:
                row = counters.setdefault(label, {"calls": 0, "seconds": 0.0})
                row["calls"] += 1
                row["seconds"] += (time.perf_counter_ns() - start) / 1e9

        setattr(owner, name, call)

    for owner, name, label in [
        (git, "_command", "ledger_subprocess"),
        (workspace_advertisement, "_git", "workspace_subprocess"),
        (instance.PlaybillInstance, "prepare_generation", "prepare_generation"),
        (instance.PlaybillInstance, "advertise_workspace", "advertise_workspace"),
        (instance.PlaybillInstance, "_reconcile_proposal_review_refs", "review_ref_reconciliation"),
        (workspace_advertisement, "advertise_workspace_refs", "workspace_fetch"),
        (git.GitLedger, "_write_tree", "generation_tree_write"),
        (git.GitLedger, "_absent_objects", "blob_presence_check"),
        (git.GitLedger, "_list_tree", "projection_inventory"),
        (git.GitLedger, "read_tree_delta", "physical_delta_read"),
        (settlement, "evaluate_proposal_tree", "settlement_evaluation"),
        (assembler.ProjectionAssembler, "assemble", "projection_prebuild"),
        (projection_delta, "update_projection_database", "database_update"),
        (assembler, "projection_logical_digest", "database_digest_build"),
        (playbill_projection, "projection_logical_digest", "database_digest_verify"),
        (recovery, "prepared_generation_for_handoff", "handoff_verify"),
        (instance, "prepared_generation_for_handoff", "handoff_verify_bound"),
    ]:
        traced(owner, name, label)
    original = playbill_api.service_activate_playbill_proposal

    @functools.wraps(original)
    def acceptance(*args, **kwargs):
        start = time.perf_counter_ns()
        local.counters = {}
        try:
            return original(*args, **kwargs)
        finally:
            report = {
                "total_seconds": (time.perf_counter_ns() - start) / 1e9,
                "nested_phases": local.counters,
            }
            local.counters = None
            with Path(prefix + ".phases.jsonl").open("a") as f:
                f.write(json.dumps(report) + "\n")

    playbill_api.service_activate_playbill_proposal = acceptance


def main():
    """Launch the existing disposable harness with acceptance-only counters."""
    import subprocess
    import sys
    import tempfile

    probe_directory = Path(__file__).resolve().parent
    harness = probe_directory / "write-loop-served.py"
    source = harness.read_text()
    marker = "    import functools\n"
    assert source.count(marker) == 1
    source = source.replace(
        marker,
        marker + f"    import sys\n    sys.path.insert(0, {str(probe_directory)!r})\n"
        "    from publication_phase_probe import install\n    install(profile_prefix)\n",
        1,
    )
    args = sys.argv[1:]
    if "--repo" not in args:
        args = ["--repo", str(probe_directory.parents[1]), *args]
    if "--no-server-profile" not in args:
        args = ["--no-server-profile", *args]
    with tempfile.TemporaryDirectory(prefix="pb-publication-probe-") as temporary:
        driver = Path(temporary) / "driver.py"
        driver.write_text(source)
        result = subprocess.run([sys.executable, str(driver), *args], check=False)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
