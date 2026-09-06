"""Disposable, isolated consumption-service and scalar-store append benchmark.

Run this portable harness with --repo pointing at each compared source tree.
All fixture construction, history seeding, snapshot copies and correctness checks
are excluded from timing. Timed calls are real record_consumption/store methods,
not SDK/HTTP: authentication, claim resolution and transport are excluded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def integers(value):
    return tuple(int(item) for item in value.split(","))


def file_snapshot(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--histories", type=integers, default=(0, 128))
    parser.add_argument("--batches", type=integers, default=(1, 32))
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if min(args.histories) < 0 or min(args.batches) < 1 or args.repeats < 1:
        parser.error("histories must be nonnegative; batches/repeats must be positive")
    repo = args.repo.resolve(strict=True)
    sys.path[:0] = [str(repo / "src"), str(repo / "packages/cruxible-client/src"), str(repo)]
    for key in tuple(os.environ):
        if key.startswith("CRUXIBLE_"):
            del os.environ[key]
    from tests.test_playbill._adoption_fixture import _Builder
    from tests.test_playbill._support import initialize_local

    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.projection import AcceptedCoordinate
    from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_digest
    from cruxible_core.playbill import consumption
    from cruxible_core.playbill.actor_context import GovernedActorContext
    from cruxible_core.playbill.review_operational import ReviewOperationalStore

    snapshot_head = repo / ".benchmark-source-head"
    head = (
        snapshot_head.read_text().strip()
        if snapshot_head.exists()
        else subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    )
    report = {
        "source_repo": str(repo),
        "source_head": head,
        "source_kind": "git-archive" if snapshot_head.exists() else "git-checkout",
        "source_file_hashes": {
            name: hashlib.sha256((repo / name).read_bytes()).hexdigest()
            for name in (
                "src/cruxible_core/playbill/consumption.py",
                "src/cruxible_core/playbill/review_operational.py",
            )
        },
        "environment": {
            "platform": platform.platform(),
            "python": sys.version,
            "executable": sys.executable,
        },
        "workload": {
            "history_receipts": args.histories,
            "batch_sizes": args.batches,
            "repeats": args.repeats,
            "scope": "Isolated real record_consumption and repeated scalar store.append; "
            "accepted Subjects in a disposable real instance. No HTTP/SDK/read resolution. "
            "Each sample restores identical initialized-store bytes; includes one epoch "
            "in addition to history receipts. Duplicate retry runs immediately afterward. "
            "No cold-process or OS-cache claim; wrappers add matching instrumentation overhead.",
            "instrumentation": "_load_partition calls/returned records and elapsed time; "
            "epoch/build/append method time; fsync count. Returned records count measures "
            "fully validated partition rows, not every internal Pydantic validation. "
            "Nested method timings overlap and must not be summed.",
        },
        "rows": [],
    }
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    now = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)

    def context(profile):
        return consumption.ConsumptionContextV1(
            actor_context=GovernedActorContext(
                actor_type="service_account",
                actor_id="owner",
                org_id="bench",
                operation_id="consumption-benchmark",
                timestamp=now,
            ),
            access_profile_id=profile,
        )

    @contextmanager
    def instrument():
        stats = {"load_partition_calls": 0, "partition_records_validated": 0, "fsync_calls": 0}
        originals = []

        def wrap(owner, name, label):
            original = getattr(owner, name)

            def counted(*positional, **keywords):
                started = time.perf_counter()
                try:
                    result = original(*positional, **keywords)
                    if label == "load_partition":
                        stats["partition_records_validated"] += len(result)
                    return result
                finally:
                    stats[label + "_calls"] = stats.get(label + "_calls", 0) + 1
                    stats[label + "_seconds"] = (
                        stats.get(label + "_seconds", 0) + time.perf_counter() - started
                    )

            originals.append((owner, name, original))
            setattr(owner, name, counted)

        wrap(ReviewOperationalStore, "_load_partition", "load_partition")
        wrap(ReviewOperationalStore, "append", "append")
        if hasattr(ReviewOperationalStore, "append_batch"):
            wrap(ReviewOperationalStore, "append_batch", "append_batch")
        wrap(consumption, "ensure_consumption_epoch", "epoch")
        wrap(consumption, "build_consumption_receipt", "receipt_build")
        wrap(os, "fsync", "fsync")
        try:
            yield stats
        finally:
            for owner, name, original in reversed(originals):
                setattr(owner, name, original)

    def measured(call):
        with instrument() as stats:
            started = time.perf_counter()
            result = call()
            seconds = time.perf_counter() - started
        return result, {"seconds": seconds, **stats}

    with tempfile.TemporaryDirectory(prefix="pb-consumption-bench-", dir="/private/tmp") as tmp:
        root = Path(tmp)
        started = time.perf_counter()
        (root / "fixture").mkdir()
        instance, owner = initialize_local(root / "fixture")
        subjects = tuple(
            SubjectShell(
                identity=ArtifactIdentity(kind="Subject", name=f"benchmark.item/item-{index:05d}"),
                subject_kind="benchmark.item",
                subject_id=f"item-{index:05d}",
            )
            for index in range(max(args.batches))
        )
        builder = _Builder(instance, owner, approver=owner)
        builder.accept(
            {f"subjects/{item.identity.name}.json": render_subject(item) for item in subjects},
            phase="benchmark-subjects",
        )
        instance.refresh()
        coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
        generation = instance.accepted_history()[-1].sequence
        artifacts = tuple((item.identity, subject_digest(item).tagged) for item in subjects)
        store = instance.review_operational_store()
        consumption.ensure_consumption_epoch(
            instance,
            coordinate=coordinate,
            generation=generation,
            actor_context=context("epoch").actor_context,
        )
        empty = root / "empty-store"
        shutil.copytree(store.root, empty)
        report["fixture_setup_seconds"] = time.perf_counter() - started
        report["accepted_coordinate"] = coordinate.model_dump(mode="json")
        report["accepted_generation"] = generation

        def restore(snapshot):
            shutil.rmtree(store.root)
            shutil.copytree(snapshot, store.root)

        for history in args.histories:
            started = time.perf_counter()
            restore(empty)
            for index in range(history):
                consumption.record_consumption(
                    instance,
                    context=context(f"history-{index}"),
                    operation="playbill.subject.get",
                    coordinate=coordinate,
                    artifacts=artifacts[:1],
                )
            seeded = root / f"history-{history}"
            shutil.copytree(store.root, seeded)
            seed_seconds = time.perf_counter() - started
            seed_snapshot = file_snapshot(seeded)
            for batch in args.batches:
                selected = artifacts[:batch]
                ctx = context("measured")
                expected = tuple(
                    consumption.build_consumption_receipt(
                        context=ctx,
                        operation="playbill.subject.get",
                        coordinate=coordinate,
                        artifact_identity=identity,
                        artifact_digest=digest,
                    )
                    for identity, digest in selected
                )
                for repeat in range(args.repeats):
                    snapshots = {}
                    for mode in ("record_consumption", "raw_scalar_append"):
                        restore(seeded)
                        assert file_snapshot(store.root) == seed_snapshot

                        def call():
                            if mode == "record_consumption":
                                return consumption.record_consumption(
                                    instance,
                                    context=ctx,
                                    operation="playbill.subject.get",
                                    coordinate=coordinate,
                                    artifacts=selected,
                                )
                            return tuple(
                                store.append(
                                    family="consumption",
                                    partition_id="receipts",
                                    event_id=receipt.receipt_id,
                                    payload=receipt,
                                    coordinate=coordinate,
                                    generation=generation,
                                    actor_context=ctx.actor_context,
                                    recorded_at=ctx.actor_context.timestamp,
                                )
                                for receipt in expected
                            )

                        first, fresh = measured(call)
                        after = file_snapshot(store.root)
                        retry, duplicate = measured(call)
                        assert first == retry and after == file_snapshot(store.root)
                        if mode == "record_consumption":
                            assert first == expected
                        events = store.events(family="consumption")
                        assert len(events) == history + batch + 1
                        snapshots[mode] = after
                        report["rows"].append(
                            {
                                "history_receipts": history,
                                "batch_size": batch,
                                "repeat": repeat,
                                "mode": mode,
                                "seed_seconds": seed_seconds,
                                "fresh": fresh,
                                "duplicate": duplicate,
                                "exact_retry_bytes_unchanged": True,
                                "event_count_after": len(events),
                                "snapshot_sha256": hashlib.sha256(
                                    json.dumps(after, sort_keys=True).encode()
                                ).hexdigest(),
                            }
                        )
                    assert snapshots["record_consumption"] == snapshots["raw_scalar_append"]
                    args.output.write_text(json.dumps(report, indent=2) + "\n")
                    print(
                        json.dumps(
                            {
                                "history": history,
                                "batch": batch,
                                "repeat": repeat,
                                "full_seconds": report["rows"][-2]["fresh"]["seconds"],
                                "full_retry_seconds": report["rows"][-2]["duplicate"]["seconds"],
                            }
                        ),
                        flush=True,
                    )
        report["verified"] = (
            "Full service and scalar append produce identical complete store bytes; "
            "duplicate retries never mutate them."
        )
        args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
