"""Read-anchor ergonomics baseline harness.

Runs each task in tasks.json as the scripted sequence of CLI calls a naive
cold-start agent would make, and records per call:
  - stdout bytes and estimated tokens (bytes / 4)
  - wall latency (two runs: cold-ish and warm; the warm run is headline)
  - metadata share: fraction of response bytes attributable to
    metadata / actor_context / provenance blobs (computed by re-serializing
    the JSON response with those keys stripped)
and verifies the final answer against known-good values.

Usage:
    uv run python benchmarks/read_anchor/capture.py \
        [--tasks benchmarks/read_anchor/tasks.json] \
        [--out benchmarks/read_anchor/baseline_results.json] \
        [--only TASK_ID ...]

Security: the operation-instance bearer token is read from its token file at
runtime and injected into the child process environment only. It is never
stored in results, printed, or logged.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
METADATA_KEYS = {"metadata", "actor_context", "provenance"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def load_bearer_token(source: dict) -> str:
    """Read the bearer token from its file at runtime. Never store it."""
    path = Path(os.path.expanduser(source["file"]))
    obj = json.loads(path.read_text())
    for key in source["json_path"]:
        obj = obj[key]
    if not isinstance(obj, str) or not obj:
        raise RuntimeError("bearer token source did not resolve to a token")
    return obj


def strip_metadata(obj):
    """Recursively remove metadata/actor_context/provenance keys."""
    if isinstance(obj, dict):
        return {
            k: strip_metadata(v)
            for k, v in obj.items()
            if k not in METADATA_KEYS
        }
    if isinstance(obj, list):
        return [strip_metadata(v) for v in obj]
    return obj


def serialized_bytes(obj) -> int:
    return len(json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def build_env(instance_cfg: dict) -> dict:
    env = dict(os.environ)
    src = instance_cfg.get("bearer_token_source")
    if src:
        env["CRUXIBLE_SERVER_BEARER_TOKEN"] = load_bearer_token(src)
    home_env = instance_cfg.get("home_dir_env")
    if home_env:
        home_dir = os.environ.get(home_env) or instance_cfg["home_dir_default"]
        env["HOME"] = home_dir
    return env


def build_argv(instance_cfg: dict, call_args: list[str]) -> list[str]:
    if instance_cfg.get("local"):
        # Local (serverless) mode: run the branch CLI from inside the copied
        # instance root; CruxibleInstance.load() walks up from cwd.
        argv = ["uv", "run", "--project", str(REPO_ROOT), "cruxible"]
        return argv + list(call_args)
    argv = ["uv", "run", "cruxible", "--server-url", instance_cfg["server_url"]]
    if instance_cfg.get("instance_id"):
        argv += ["--instance-id", instance_cfg["instance_id"]]
    return argv + list(call_args)


def run_call(
    argv: list[str], env: dict, cwd: Path = REPO_ROOT
) -> tuple[bytes, bytes, int, float]:
    t0 = time.perf_counter()
    proc = subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, timeout=300
    )
    latency = time.perf_counter() - t0
    return proc.stdout, proc.stderr, proc.returncode, latency


# --------------------------------------------------------------------------
# correctness checkers (keyed by expect.kind)
# --------------------------------------------------------------------------

def _final_json(outputs: list[bytes]):
    return json.loads(outputs[-1].decode("utf-8"))


def check_neighbor_edge(expect, outputs):
    doc = _final_json(outputs)
    for n in doc.get("neighbors", []):
        ent = n.get("entity") or {}
        if (
            n.get("relationship_type") == expect["relationship_type"]
            and ent.get("entity_id") == expect["entity_id"]
            and (
                "direction" not in expect
                or n.get("direction") == expect["direction"]
            )
        ):
            return True, f"found {expect['relationship_type']} -> {expect['entity_id']}"
    return False, f"edge {expect['relationship_type']} -> {expect['entity_id']} not found"


def check_json_contains_string(expect, outputs):
    text = outputs[-1].decode("utf-8")
    ok = expect["value"] in text
    return ok, f"'{expect['value']}' {'present' if ok else 'MISSING'} in final response"


def check_result_ids(expect, outputs):
    doc = _final_json(outputs)
    if doc.get("layout") == "graph":
        # Graph layout: results[] carry node indexes into nodes[].
        nodes = doc.get("nodes", [])
        ids = sorted(
            {
                nodes[ref["result"]]["entity_id"]
                for ref in doc.get("results", [])
                if isinstance(ref.get("result"), int)
            }
        )
    else:
        ids = sorted({item["result"]["entity_id"] for item in doc.get("items", [])})
    total = doc.get("total")
    if total != expect["expected_total"]:
        return False, f"total={total}, expected {expect['expected_total']}"
    if "expected_ids" in expect and ids != sorted(expect["expected_ids"]):
        return False, f"result ids {ids} != expected {sorted(expect['expected_ids'])}"
    if "required_ids" in expect:
        missing = [i for i in expect["required_ids"] if i not in ids]
        if missing:
            return False, f"missing required ids: {missing}"
    return True, f"total={total}, ids verified"


def check_total_equals(expect, outputs):
    doc = _final_json(outputs)
    total = doc.get("total")
    ok = total == expect["expected_total"]
    return ok, f"total={total}, expected {expect['expected_total']}"


def check_graph_edge(expect, outputs):
    """Edge assertion for the expanded bounded-neighborhood (nodes/edges) shape."""
    doc = _final_json(outputs)
    for e in doc.get("edges", []):
        if (
            e.get("relationship_type") == expect["relationship_type"]
            and expect["entity_id"] in (e.get("to_id"), e.get("from_id"))
        ):
            return True, f"found {expect['relationship_type']} -> {expect['entity_id']}"
    return False, f"edge {expect['relationship_type']} -> {expect['entity_id']} not found"


def check_stats_list_match(expect, outputs):
    stats = json.loads(outputs[0].decode("utf-8"))
    listing = json.loads(outputs[1].decode("utf-8"))
    counts = stats.get("entity_counts") or stats.get("entities") or {}
    stats_count = counts.get(expect["entity_type"])
    list_total = listing.get("total")
    ok = stats_count == list_total == expect["expected_total"]
    return ok, (
        f"stats {expect['entity_type']}={stats_count}, list total={list_total}, "
        f"expected {expect['expected_total']}"
    )


CHECKERS = {
    "neighbor_edge": check_neighbor_edge,
    "graph_edge": check_graph_edge,
    "json_contains_string": check_json_contains_string,
    "result_ids": check_result_ids,
    "total_equals": check_total_equals,
    "stats_list_match": check_stats_list_match,
}


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def measure_cli_overhead(env: dict) -> float:
    """Median wall time of a no-network CLI invocation (uv + click startup)."""
    samples = []
    for _ in range(3):
        t0 = time.perf_counter()
        subprocess.run(
            ["uv", "run", "cruxible", "--version"],
            cwd=REPO_ROOT, env=env, capture_output=True,
        )
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples)


def run_task(
    task: dict,
    instance_cfg: dict,
    *,
    calls: list[dict] | None = None,
    expect: dict | None = None,
    flow: str = "baseline",
    instance_name: str | None = None,
) -> dict:
    calls = calls if calls is not None else task["calls"]
    expect = expect if expect is not None else task["expect"]
    cwd = Path(instance_cfg["cwd"]) if instance_cfg.get("cwd") else REPO_ROOT
    env = build_env(instance_cfg)
    call_records = []
    outputs: list[bytes] = []

    for call in calls:
        argv = build_argv(instance_cfg, call["args"])
        # Run 1 (cold-ish), then Run 2 (warm daemon) — warm is headline.
        _, _, _, latency1 = run_call(argv, env, cwd)
        stdout, stderr, code, latency2 = run_call(argv, env, cwd)
        outputs.append(stdout)

        n_bytes = len(stdout)
        record = {
            "args": call["args"],
            "purpose": call["purpose"],
            "exit_code": code,
            "bytes": n_bytes,
            "est_tokens": round(n_bytes / 4),
            "latency_run1_s": round(latency1, 3),
            "latency_run2_s": round(latency2, 3),
        }
        if code != 0:
            record["stderr_tail"] = stderr.decode("utf-8", "replace")[-500:]
        # Metadata share, for JSON responses.
        try:
            doc = json.loads(stdout.decode("utf-8"))
            orig = serialized_bytes(doc)
            stripped = serialized_bytes(strip_metadata(copy.deepcopy(doc)))
            record["compact_json_bytes"] = orig
            record["metadata_stripped_bytes"] = stripped
            record["metadata_share"] = round(1 - stripped / orig, 3) if orig else 0.0
        except (ValueError, UnicodeDecodeError):
            record["metadata_share"] = None
        call_records.append(record)

    checker = CHECKERS[expect["kind"]]
    try:
        correct, detail = checker(expect, outputs)
    except Exception as exc:  # noqa: BLE001 - verification must never crash the run
        correct, detail = False, f"checker error: {exc!r}"

    total_bytes = sum(c["bytes"] for c in call_records)
    return {
        "id": task["id"],
        "flow": flow,
        "instance": instance_name or task["instance"],
        "question": task["question"],
        "num_calls": len(call_records),
        "total_bytes": total_bytes,
        "total_est_tokens": round(total_bytes / 4),
        "total_latency_run2_s": round(
            sum(c["latency_run2_s"] for c in call_records), 3
        ),
        "total_latency_run1_s": round(
            sum(c["latency_run1_s"] for c in call_records), 3
        ),
        "correct": correct,
        "verification_detail": detail,
        "calls": call_records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tasks", default=str(Path(__file__).parent / "tasks.json")
    )
    parser.add_argument(
        "--out", default=str(Path(__file__).parent / "baseline_results.json")
    )
    parser.add_argument("--only", nargs="*", default=None, help="task ids to run")
    parser.add_argument(
        "--flow",
        choices=["baseline", "ergonomic", "ergonomic_v2"],
        default="baseline",
        help=(
            "baseline runs each task's original call sequence; ergonomic / "
            "ergonomic_v2 run the task's variant of that name (calls/expect/"
            "instance overrides). Tasks without the requested variant are "
            "skipped (v2 variants exist only where the new machinery changes "
            "the flow; identical flows carry their ergonomic numbers forward)."
        ),
    )
    args = parser.parse_args()

    spec = json.loads(Path(args.tasks).read_text())
    tasks = spec["tasks"]
    if args.only:
        tasks = [t for t in tasks if t["id"] in args.only]

    overhead = measure_cli_overhead(dict(os.environ))
    results = []
    for task in tasks:
        calls = expect = None
        instance_name = task["instance"]
        if args.flow != "baseline":
            variant = task.get(args.flow)
            if not variant:
                print(f"skipping {task['id']} (no {args.flow} variant)", flush=True)
                continue
            calls = variant["calls"]
            expect = variant.get("expect")
            instance_name = variant.get("instance", task["instance"])
        instance_cfg = spec["instances"][instance_name]
        print(f"running {task['id']} [{args.flow}] ...", flush=True)
        result = run_task(
            task,
            instance_cfg,
            calls=calls,
            expect=expect,
            flow=args.flow,
            instance_name=instance_name,
        )
        status = "OK " if result["correct"] else "FAIL"
        print(
            f"  [{status}] calls={result['num_calls']} "
            f"bytes={result['total_bytes']} "
            f"tokens~{result['total_est_tokens']} "
            f"warm={result['total_latency_run2_s']}s "
            f"({result['verification_detail']})",
            flush=True,
        )
        results.append(result)

    out = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "harness": "benchmarks/read_anchor/capture.py",
        "notes": {
            "latency": "each call is run twice; run2 (warm daemon) is headline",
            "est_tokens": "stdout bytes / 4, rounded",
            "metadata_share": (
                "1 - stripped/original compact-JSON bytes after recursively "
                "removing keys: metadata, actor_context, provenance"
            ),
            "cli_process_overhead_s": round(overhead, 3),
        },
        "tasks": results,
    }
    Path(args.out).write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote {args.out}")
    return 0 if all(r["correct"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
