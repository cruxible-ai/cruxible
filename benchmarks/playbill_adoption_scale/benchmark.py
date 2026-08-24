"""The PC-F Tier-1 adoption-scale benchmark (§12.3).

Never collected by pytest and never run by CI: it lives under `benchmarks/`,
carries no `test_` prefix, and builds a thousand-generation ledger that takes
tens of minutes. Run it explicitly.

    uv run python benchmarks/playbill_adoption_scale/benchmark.py run \\
        --root /path/to/scratch --suffix 100 --samples 10

It measures, and reports verbatim:

* fixture construction wall time, per phase;
* valid-checkpoint reopen over N clean process starts with a bounded suffix;
* forced genesis recovery wall time, and whether it reproduces the same
  generation, semantic, and logical projection digests;
* disposable-projection deletion and rebuild;
* peak RSS, Git invocations, blobs read, and bytes read, per measured run.

Each measured reopen happens in a **fresh process** (the `open` subcommand), so
no warmed cache, memo, or page-cache-resident Python object can leak between
samples the way it would inside one long-lived harness process.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from tests.test_playbill._adoption_fixture import (  # noqa: E402
    TIER_1,
    TRUST_ROOT_FILE,
    AdoptionFixtureProfile,
    build_fixture,
)

from cruxible_client.contracts.types import PlaybillTrustRoot  # noqa: E402
from cruxible_core.playbill.checkpoints import (  # noqa: E402
    CHECKPOINT_DIRECTORY,
    checkpoint_body,
    checkpoint_path,
    render_checkpoint,
)
from cruxible_core.playbill.git import GitLedger  # noqa: E402
from cruxible_core.playbill.instance import PlaybillInstance  # noqa: E402
from cruxible_core.playbill.serving import SERVING_MANIFEST_FILE  # noqa: E402

FIXTURE_FILE = "fixture.json"


@dataclass
class LedgerCounters:
    """Read accounting for one measured run, gathered at the ledger seam."""

    git_invocations: int = 0
    blobs_read: int = 0
    blob_bytes_read: int = 0
    trees_listed: int = 0


def _instrument(counters: LedgerCounters) -> None:
    """Count Git work without changing what any of it does."""

    original_git = GitLedger._git
    original_blobs = GitLedger.read_blobs
    original_list = GitLedger._list_tree

    def counted_git(self, arguments, **kwargs):  # type: ignore[no-untyped-def]
        counters.git_invocations += 1
        return original_git(self, arguments, **kwargs)

    def counted_blobs(self, oids):  # type: ignore[no-untyped-def]
        blobs = original_blobs(self, oids)
        counters.blobs_read += len(blobs)
        counters.blob_bytes_read += sum(len(value) for value in blobs.values())
        return blobs

    def counted_list(self, oid, *, with_sizes):  # type: ignore[no-untyped-def]
        counters.trees_listed += 1
        return original_list(self, oid, with_sizes=with_sizes)

    GitLedger._git = counted_git  # type: ignore[method-assign]
    GitLedger.read_blobs = counted_blobs  # type: ignore[method-assign]
    GitLedger._list_tree = counted_list  # type: ignore[method-assign]


def _peak_rss_bytes() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes; Linux reports kibibytes.
    return usage if sys.platform == "darwin" else usage * 1024


def _projection_files(managed_root: Path) -> list[Path]:
    projections = managed_root / "projections"
    return [
        path
        for path in sorted(projections.iterdir())
        if path.name.startswith("projection-") or path.name == SERVING_MANIFEST_FILE
    ]


def _saved_checkpoint(root: Path, suffix: int) -> Path:
    return root / f"checkpoint-suffix-{suffix}.json"


def _prepare_checkpoint(
    root: Path,
    managed_root: Path,
    trust_root: PlaybillTrustRoot,
    suffix: int,
) -> int:
    """Build the bounded-suffix checkpoint once and save a copy for every sample.

    Recovery advances the checkpoint to the head it just served, so a benchmark
    measuring a bounded suffix has to re-place it before each sample. Deriving it
    per sample would mean a full recovery per sample outside the timed region and
    would double the run for nothing, so it is derived once here through the
    published checkpoint API and each sample copies the saved bytes into place.
    """

    instance = PlaybillInstance.open(managed_root, trust_root=trust_root)
    history = instance.accepted_history()
    target = max(1, history[-1].sequence - suffix)
    generation = next(item for item in history if item.sequence == target)
    parent = next(item for item in history if item.sequence == target - 1)
    body = checkpoint_body(
        instance_id=instance.descriptor.instance_id,
        object_format=instance.descriptor.git_object_format,
        compiler=instance.descriptor.compiler,
        genesis=instance.descriptor.genesis,
        sequence=target,
        git_oid=generation.oid,
        semantic_root=generation.semantic_root.tagged,
        generation_root=generation.generation_root.tagged,
        parent_generation_root=parent.generation_root.tagged,
        tree=instance._ledger.read_tree(generation.oid),
    )
    _saved_checkpoint(root, suffix).write_bytes(
        render_checkpoint(body, written_at="2026-01-01T00:00:00.000000Z")
    )
    return history[-1].sequence - target


def _restore_checkpoint(root: Path, managed_root: Path, suffix: int) -> None:
    """Put the saved bounded-suffix checkpoint back, byte for byte."""

    directory = managed_root / CHECKPOINT_DIRECTORY
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    checkpoint_path(directory).write_bytes(_saved_checkpoint(root, suffix).read_bytes())


def command_build(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    profile = TIER_1 if args.profile == "tier-1" else _profile_from(args)
    started = time.monotonic()
    fixture = build_fixture(root, profile, resume=args.resume)
    elapsed = time.monotonic() - started
    summary = {
        "profile": profile.name,
        "managed_root": str(fixture.managed_root),
        "members": fixture.member_count,
        "declared_members": profile.expected_members,
        "generations": fixture.head_sequence,
        "build_seconds": round(elapsed, 3),
        "phase_seconds": {name: round(value, 3) for name, value in fixture.timings.seconds.items()},
        "peak_rss_bytes": _peak_rss_bytes(),
    }
    (root / FIXTURE_FILE).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


def _profile_from(args: argparse.Namespace) -> AdoptionFixtureProfile:
    return AdoptionFixtureProfile(
        name=args.profile,
        subjects=args.subjects,
        claim_types=args.claim_types,
        documents=args.documents,
        query_definitions=args.query_definitions,
        seed_claims=args.seed_claims,
        generations=args.generations,
    )


def command_open(args: argparse.Namespace) -> int:
    """One clean process start; prints exactly what it measured."""

    root = Path(args.root).resolve()
    managed_root = Path(args.managed_root)
    trust_root = PlaybillTrustRoot.model_validate_json(
        (root / TRUST_ROOT_FILE).read_text(encoding="utf-8")
    )
    if args.mode == "genesis":
        checkpoint_path(managed_root / CHECKPOINT_DIRECTORY).unlink(missing_ok=True)
        suffix = None
    else:
        _restore_checkpoint(root, managed_root, args.suffix)
        suffix = args.suffix
    if args.rebuild_projection:
        for path in _projection_files(managed_root):
            path.unlink()

    counters = LedgerCounters()
    _instrument(counters)
    started = time.monotonic()
    instance = PlaybillInstance.open(managed_root, trust_root=trust_root)
    elapsed = time.monotonic() - started
    recovered = instance._recovered
    print(
        json.dumps(
            {
                "mode": args.mode,
                "suffix_generations": suffix,
                "rebuilt_projection": bool(args.rebuild_projection),
                "seconds": round(elapsed, 4),
                "head_sequence": recovered.head.sequence,
                "git_oid": recovered.head.oid,
                "semantic_root": recovered.head.semantic_root.tagged,
                "generation_root": recovered.head.generation_root.tagged,
                "logical_digest": (
                    None if recovered.projection is None else recovered.projection.logical_digest
                ),
                "peak_rss_bytes": _peak_rss_bytes(),
                "git_invocations": counters.git_invocations,
                "blobs_read": counters.blobs_read,
                "blob_bytes_read": counters.blob_bytes_read,
                "trees_listed": counters.trees_listed,
            }
        )
    )
    return 0


def _sample(root: Path, managed_root: Path, **flags: object) -> dict[str, object]:
    arguments = [sys.executable, str(Path(__file__).resolve()), "open", "--root", str(root)]
    arguments += ["--managed-root", str(managed_root)]
    for name, value in flags.items():
        option = "--" + name.replace("_", "-")
        if isinstance(value, bool):
            if value:
                arguments.append(option)
        else:
            arguments += [option, str(value)]
    completed = subprocess.run(
        arguments,
        capture_output=True,
        check=True,
        cwd=str(REPOSITORY_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPOSITORY_ROOT)},
    )
    return dict(json.loads(completed.stdout.decode().strip().splitlines()[-1]))


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def command_run(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    summary = json.loads((root / FIXTURE_FILE).read_text(encoding="utf-8"))
    managed_root = Path(summary["managed_root"])

    trust_root = PlaybillTrustRoot.model_validate_json(
        (root / TRUST_ROOT_FILE).read_text(encoding="utf-8")
    )
    placed = _prepare_checkpoint(root, managed_root, trust_root, args.suffix)
    checkpointed = [
        _sample(root, managed_root, mode="checkpoint", suffix=args.suffix)
        for _ in range(args.samples)
    ]
    rebuilt = [
        _sample(
            root,
            managed_root,
            mode="checkpoint",
            suffix=args.suffix,
            rebuild_projection=True,
        )
        for _ in range(args.rebuild_samples)
    ]
    genesis = [_sample(root, managed_root, mode="genesis") for _ in range(args.genesis_samples)]

    digests = {
        (row["git_oid"], row["semantic_root"], row["generation_root"], row["logical_digest"])
        for row in checkpointed + rebuilt + genesis
    }
    report = {
        "fixture": summary,
        "placed_suffix_generations": placed,
        "reproduced_identical_digests": len(digests) == 1,
        "digests": [
            {
                "git_oid": item[0],
                "semantic_root": item[1],
                "generation_root": item[2],
                "logical_digest": item[3],
            }
            for item in sorted(digests)
        ],
        "checkpoint_reopen": _statistics(checkpointed),
        "projection_rebuild_reopen": _statistics(rebuilt),
        "forced_genesis_recovery": _statistics(genesis),
        "samples": {
            "checkpoint_reopen": checkpointed,
            "projection_rebuild_reopen": rebuilt,
            "forced_genesis_recovery": genesis,
        },
    }
    rebuild_delta = (
        report["projection_rebuild_reopen"]["p95_seconds"]
        - report["checkpoint_reopen"]["p95_seconds"]
    )
    report["projection_rebuild_only_p95_seconds"] = round(rebuild_delta, 4)
    print(json.dumps(report, indent=2))
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


def _statistics(rows: list[dict[str, object]]) -> dict[str, object]:
    seconds = [float(row["seconds"]) for row in rows]
    return {
        "samples": len(rows),
        "min_seconds": round(min(seconds), 4),
        "median_seconds": round(statistics.median(seconds), 4),
        "p95_seconds": round(_percentile(seconds, 0.95), 4),
        "max_seconds": round(max(seconds), 4),
        "peak_rss_bytes": max(int(row["peak_rss_bytes"]) for row in rows),
        "git_invocations": max(int(row["git_invocations"]) for row in rows),
        "blobs_read": max(int(row["blobs_read"]) for row in rows),
        "blob_bytes_read": max(int(row["blob_bytes_read"]) for row in rows),
        "trees_listed": max(int(row["trees_listed"]) for row in rows),
        "suffix_generations": rows[0]["suffix_generations"],
    }


def command_profile(args: argparse.Namespace) -> int:
    """Decompose one checkpointed reopen, so a missed budget names its own cause."""

    import cProfile
    import pstats

    root = Path(args.root).resolve()
    summary = json.loads((root / FIXTURE_FILE).read_text(encoding="utf-8"))
    managed_root = Path(summary["managed_root"])
    trust_root = PlaybillTrustRoot.model_validate_json(
        (root / TRUST_ROOT_FILE).read_text(encoding="utf-8")
    )
    suffix = _prepare_checkpoint(root, managed_root, trust_root, args.suffix)
    _restore_checkpoint(root, managed_root, args.suffix)
    profiler = cProfile.Profile()
    profiler.enable()
    PlaybillInstance.open(managed_root, trust_root=trust_root)
    profiler.disable()
    print(f"# decomposition of one checkpointed reopen, suffix={suffix} generations")
    pstats.Stats(profiler).sort_stats("cumulative").print_stats(args.entries)
    pstats.Stats(profiler).sort_stats("tottime").print_stats(args.entries)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    build = subcommands.add_parser("build", help="construct the fixture ledger")
    build.add_argument("--root", required=True)
    build.add_argument("--profile", default="tier-1")
    build.add_argument("--subjects", type=int, default=TIER_1.subjects)
    build.add_argument("--claim-types", type=int, default=TIER_1.claim_types)
    build.add_argument("--documents", type=int, default=TIER_1.documents)
    build.add_argument("--query-definitions", type=int, default=TIER_1.query_definitions)
    build.add_argument("--seed-claims", type=int, default=TIER_1.seed_claims)
    build.add_argument("--generations", type=int, default=TIER_1.generations)
    build.add_argument(
        "--resume",
        action="store_true",
        help="continue an interrupted build from what it already accepted",
    )
    build.set_defaults(handler=command_build)

    opened = subcommands.add_parser("open", help="one clean measured process start")
    opened.add_argument("--root", required=True)
    opened.add_argument("--managed-root", required=True)
    opened.add_argument("--mode", choices=("checkpoint", "genesis"), default="checkpoint")
    opened.add_argument("--suffix", type=int, default=100)
    opened.add_argument("--rebuild-projection", action="store_true")
    opened.set_defaults(handler=command_open)

    run = subcommands.add_parser("run", help="measure an already-built fixture")
    run.add_argument("--root", required=True)
    run.add_argument("--suffix", type=int, default=100)
    run.add_argument("--samples", type=int, default=10)
    run.add_argument("--rebuild-samples", type=int, default=10)
    run.add_argument("--genesis-samples", type=int, default=2)
    run.add_argument("--output")
    run.set_defaults(handler=command_run)

    profile = subcommands.add_parser("profile", help="decompose one checkpointed reopen")
    profile.add_argument("--root", required=True)
    profile.add_argument("--suffix", type=int, default=100)
    profile.add_argument("--entries", type=int, default=25)
    profile.set_defaults(handler=command_profile)

    args = parser.parse_args(argv)
    handler = args.handler
    return int(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
