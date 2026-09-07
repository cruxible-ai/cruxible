"""Compare derived-state phases in two isolated source checkouts.

Run with the Core environment Python, outside the canonical checkout:
  python docs/benchmarks/derived-state-scope.py \
    --baseline /path/to/before --after /path/to/after --output /tmp/scope.json

This is an in-process phase benchmark, not a served proposal/acceptance benchmark.
Fixture generation, imports, parent warming, GC, counters and full parity checks
are outside latency samples. Cold means empty derivation/membership caches and
cleared dependency-parser memo; it does not flush OS caches or restart Python.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def summary(samples):
    return {
        "samples_s": samples,
        "median_s": statistics.median(samples),
        "min_s": min(samples),
        "max_s": max(samples),
    }


def source_state(repo):
    paths = [
        "src/cruxible_core/playbill/authoring/lowering.py",
        "src/cruxible_core/playbill/evaluation_state_cache.py",
        "src/cruxible_core/playbill/derived_state.py",
        "src/cruxible_core/playbill/proposals.py",
        "src/cruxible_core/playbill/closure.py",
        "src/cruxible_core/playbill/claim_subject_index.py",
        "packages/cruxible-client/src/cruxible_client/contracts/merkle.py",
        "packages/cruxible-client/src/cruxible_client/_persistent.py",
    ]
    return {
        path: hashlib.sha256((repo / path).read_bytes()).hexdigest()
        for path in paths
        if (repo / path).exists()
    }


def worker(args):
    repo = args.repo.resolve()
    sys.path[:0] = [str(repo / "src"), str(repo / "packages/cruxible-client/src"), str(repo)]
    from tests.test_playbill.test_authoring_disposition_slots import _claim_in_slot
    from tests.test_playbill.test_incremental_closure import _path, _subject

    from cruxible_client.contracts.claims import LiteralClaimObject, claim_path, render_claim
    from cruxible_client.contracts.semantic import SemanticAddress
    from cruxible_client.contracts.subjects import render_subject
    from cruxible_core.playbill import closure, proposals
    from cruxible_core.playbill import evaluation_state_cache as cache_module
    from cruxible_core.playbill.authoring import lowering

    snapshot_mode = args.mode == "after"
    if snapshot_mode:
        from cruxible_core.playbill import derived_state
    hashes = source_state(repo)
    rows = []

    def reset_parser():
        for name in ("parse_dependency_artifact", "_dependency_artifact_bytes"):
            clear = getattr(getattr(closure, name, None), "cache_clear", None)
            if clear is not None:
                clear()

    def timed(fn):
        gc.collect()
        start = time.perf_counter()
        result = fn()
        return time.perf_counter() - start, result

    def make_root(tree):
        return derived_state.SnapshotTree(tree) if snapshot_mode else tree

    def edited(base, path, content):
        if snapshot_mode:
            candidate = base.fork()
            candidate[path] = content
            return candidate.snapshot()
        return {**base, path: content}

    def state_signature(state):
        return {
            "members": dict(state.members),
            "merkle": state.merkle.root.value,
            "dependency_root": state.dependencies.edge_root.value,
        }

    def evaluation_counts(cache, candidate):
        counts = {"dependency_parser_calls": 0, "semantic_projection_input_members": 0}
        original_parse = closure.parse_dependency_artifact
        original_project = cache_module.semantic_projection

        def parse(*a, **kw):
            counts["dependency_parser_calls"] += 1
            return original_parse(*a, **kw)

        def project(tree):
            counts["semantic_projection_input_members"] += len(tree)
            return original_project(tree)

        with (
            patch.object(closure, "parse_dependency_artifact", parse),
            patch.object(cache_module, "semantic_projection", project),
            patch.object(proposals, "semantic_projection", project),
        ):
            cache.derive(candidate)
        return counts

    def contender_counts(candidate, statement):
        counts = {"claim_parser_calls": 0}
        original = lowering.parse_claim

        def parse(*a, **kw):
            counts["claim_parser_calls"] += 1
            return original(*a, **kw)

        with patch.object(lowering, "parse_claim", parse):
            if snapshot_mode:
                with patch.object(derived_state, "parse_claim", parse):
                    lowering._ClaimPredicateIndex(candidate).claims_for(statement)
            else:
                lowering._ClaimPredicateIndex(candidate).claims_for(statement)
        return counts

    for population in args.sizes:
        subject_rows = {
            _path(f"item-{n:05d}"): render_subject(_subject(f"item-{n:05d}"))
            for n in range(population)
        }
        path = _path("item-00000")
        cold_samples = []
        cold_expected = proposals.build_tree_state(subject_rows)
        for _ in range(args.repeats):
            reset_parser()

            def bootstrap():
                cache = cache_module.EvaluationStateCache()
                return cache.derive(make_root(subject_rows))

            elapsed, result = timed(bootstrap)
            assert result == cold_expected
            cold_samples.append(elapsed)
        base = make_root(subject_rows)
        cache = cache_module.EvaluationStateCache()
        cache.derive(base)
        updates = []
        signatures = []
        for n in range(args.repeats):
            content = render_subject(_subject("item-00000", revision=n + 1))
            cache.derive(base)
            elapsed, result = timed(lambda: cache.derive(edited(base, path, content)))
            expected = proposals.build_tree_state({**subject_rows, path: content})
            assert result == expected
            signatures.append(digest(state_signature(result)))
            updates.append(elapsed)
        cache.derive(base)
        count_candidate = edited(base, path, render_subject(_subject("item-00000", revision=99)))
        rows.append(
            {
                "workload": "evaluation",
                "population": population,
                "cold_bootstrap": summary(cold_samples),
                "fresh_parent_update": summary(updates),
                "untimed_counters": evaluation_counts(cache, count_candidate),
                "parity": {
                    "full_cold_state_equal_each_sample": True,
                    "sample_state_sha256": signatures,
                },
            }
        )

        claim_rows = {}
        first = None
        for n in range(population):
            template = _claim_in_slot(claim_id=f"CLM-{n:032x}", qualifier=None)
            claim = template.model_copy(
                update={
                    "statement": template.statement.model_copy(
                        update={"subject": SemanticAddress.whole_artifact(_path(f"item-{n:05d}"))}
                    ),
                }
            )
            if n == 0:
                first = claim
            claim_rows[claim_path(claim.identity.name)] = render_claim(claim)
        assert first is not None
        statement = first.statement
        path = claim_path(first.identity.name)
        cold_samples = []
        for _ in range(args.repeats):
            elapsed, result = timed(
                lambda: lowering._ClaimPredicateIndex(make_root(claim_rows)).claims_for(statement)
            )
            assert result == (first,)
            cold_samples.append(elapsed)
        base = make_root(claim_rows)
        lowering._ClaimPredicateIndex(base).claims_for(statement)
        updates, signatures = [], []
        for n in range(args.repeats):
            changed_claim = first.model_copy(
                update={
                    "statement": first.statement.model_copy(
                        update={"object": LiteralClaimObject(value=f"revision-{n}")}
                    )
                }
            )
            content = render_claim(changed_claim)
            elapsed, result = timed(
                lambda: lowering._ClaimPredicateIndex(edited(base, path, content)).claims_for(
                    statement
                )
            )
            oracle = lowering._same_predicate_claims({**claim_rows, path: content}, statement)
            assert result == oracle == (changed_claim,)
            signatures.append(digest([claim.model_dump(mode="json") for claim in result]))
            updates.append(elapsed)
        counts = contender_counts(edited(base, path, content), statement)
        rows.append(
            {
                "workload": "contenders",
                "population": population,
                "cold_bootstrap": summary(cold_samples),
                "fresh_parent_update": summary(updates),
                "untimed_counters": counts,
                "parity": {"full_scan_equal_each_sample": True, "sample_claim_sha256": signatures},
            }
        )
        print(f"{args.mode}: completed population {population}", file=sys.stderr, flush=True)
    final_hashes = source_state(repo)
    if final_hashes != hashes:
        raise RuntimeError("measured production files changed during this worker; rerun")
    return {
        "mode": args.mode,
        "repo": str(repo),
        "source_sha256": hashes,
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
        "python": sys.version,
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--after", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 1000, 10000])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--repo", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--mode", choices=["before", "after"], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args), indent=2))
        return
    if not args.baseline or not args.after or not args.output:
        parser.error("--baseline, --after and --output are required")
    results = []
    for mode, repo in (("before", args.baseline), ("after", args.after)):
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            "--repo",
            str(repo),
            "--mode",
            mode,
            "--repeats",
            str(args.repeats),
            "--sizes",
            *map(str, args.sizes),
        ]
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        result = json.loads(subprocess.check_output(command, cwd=repo, env=environment, text=True))
        results.append(result)
    comparisons = []
    for before, after in zip(results[0]["rows"], results[1]["rows"], strict=True):
        assert (before["workload"], before["population"]) == (
            after["workload"],
            after["population"],
        )
        assert before["parity"] == after["parity"]
        comparisons.append(
            {
                "workload": before["workload"],
                "population": before["population"],
                "before_update_median_s": before["fresh_parent_update"]["median_s"],
                "after_update_median_s": after["fresh_parent_update"]["median_s"],
                "cross_version_parity": True,
            }
        )
    output = {
        "scope": "In-process evaluation and contender phases; no HTTP, Git, CAS or acceptance",
        "fixture": (
            "N Subjects for evaluation; N Claims on distinct Subject groups for "
            "contenders; one changed row and one matching contender."
        ),
        "repeats": args.repeats,
        "limits": [
            "Cold clears derivation and dependency-parser caches; process and OS remain warm.",
            "Fresh updates use one warmed parent; baseline restoration is outside timing.",
            "All raw map copy or persistent fork/seal costs are inside fresh-update timing.",
            "Cold full-state and scan parity checks and instrumentation are outside timing.",
            "Flat directory Merkle ancestor hashing remains proportional to sibling fan-out.",
            "Workers run sequentially; concurrent root checks and host load affect latency.",
            "Deterministic test artifacts only; no governed production instance accessed.",
        ],
        "comparisons": comparisons,
        "runs": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(comparisons, indent=2))


if __name__ == "__main__":
    main()
