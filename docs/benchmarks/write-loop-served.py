"""Measure real SDK -> Unix HTTP daemon writes against freshly generated state.

Example (use the same command, changing only --repo/--output, for comparisons):
  .venv/bin/python docs/benchmarks/write-loop-served.py --repo /path/to/worktree \
      --population 1000 --history 8 --repeats 3 --output /tmp/write-loop.json

Setup uses the lawful adoption fixture; all measured operations use real HTTP.
The first loop follows daemon startup and SDK orientation (reported separately),
not an OS disk-cache flush. Later loops reuse the processes but advance accepted
state, so "warm" does not mean repeated reads of an unchanged coordinate.
No live instances or existing keys are used. State is removed unless --keep-state.
Optional --profile writes client-side cProfile data beside the JSON report; it
includes network wait, not daemon CPU. Setup and daemon startup are excluded.
Acceptance is profiled by default inside the daemon service worker, producing
<output-stem>.server.accept-N.pstats. These timings include profiling overhead;
use matching instrumentation on both sides of a comparison. The default batch
has nine in-place revisions and nine creates. Forty-eight unsigned, unreferenced
review commits exercise recovery's handling of real proposal leftovers.
Use --no-server-profile for plain latency, or --profile-write-phases to also
profile compile/preflight and submit inside their service worker calls. Readback
grades are recorded. Under this fixture's admission policy, the SDK's coordinator
self-source does not support the claim: the diagnostic returns current, uncovered
claims. This measures lawful
accepted writes and readback, not a supported-evidence customer proof.
"""

from __future__ import annotations

import argparse
import cProfile
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def serve(socket: str, profile_prefix: str, scope: str) -> None:
    """Profile inside the synchronous service worker, not the ASGI event loop."""
    import functools

    from cruxible_core.runtime import playbill_api
    from cruxible_core.server.app import run_server

    def traced(name, original):
        counter = iter(range(1_000_000))

        @functools.wraps(original)
        def call(*args, **kwargs):
            profile = cProfile.Profile()
            ordinal = next(counter)
            try:
                return profile.runcall(original, *args, **kwargs)
            finally:
                profile.dump_stats(f"{profile_prefix}.{name}-{ordinal}.pstats")

        return call

    if scope != "none":
        playbill_api.service_activate_playbill_proposal = traced(
            "accept", playbill_api.service_activate_playbill_proposal
        )
    if scope == "write":
        from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator

        for name in ("compile", "compile_input", "submit"):
            setattr(
                AuthoringIntentCoordinator,
                name,
                traced(name, getattr(AuthoringIntentCoordinator, name)),
            )
    run_server(socket_path=socket)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--population", type=int, default=1000)
    parser.add_argument("--history", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--claims-per-write", type=int, default=18)
    parser.add_argument("--orphan-proposals", type=int, default=48)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--server-profile", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--profile-write-phases", action="store_true")
    parser.add_argument("--keep-state", action="store_true")
    args = parser.parse_args()
    if args.profile_write_phases and not args.server_profile:
        parser.error("--profile-write-phases requires --server-profile")
    if (
        min(args.population, args.repeats, args.claims_per_write) < 1
        or min(args.history, args.orphan_proposals) < 0
    ):
        parser.error("population/repeats must be positive; history must be nonnegative")
    repo = args.repo.resolve(strict=True)
    roots = [repo / "src", repo / "packages/cruxible-client/src", repo]
    sys.path[:0] = [str(path) for path in roots]
    # Strip inherited daemon/client configuration before importing either side.
    for key in tuple(os.environ):
        if key.startswith("CRUXIBLE_"):
            del os.environ[key]
    root = Path(tempfile.mkdtemp(prefix="pb-served-bench-", dir="/private/tmp"))
    os.environ.update(
        CRUXIBLE_STATE_ROOT=str(root / "server-state"),
        CRUXIBLE_MODE="admin",
        CRUXIBLE_CLIENT_TIMEOUT_S="900",
        CRUXIBLE_CLI_CONTEXT_PATH=str(root / "empty-context.json"),
        PYTHONPATH=os.pathsep.join(str(path) for path in roots),
    )
    # Imports deliberately follow --repo selection; never silently benchmark the
    # interpreter's installed distribution instead of the declared checkout.
    import httpx
    from tests.test_playbill._adoption_fixture import (
        AdoptionFixtureProfile,
        _Builder,
        _digest_id,
        build_fixture,
    )

    from cruxible_client import Playbill
    from cruxible_client.contracts.attestations import ApprovalStatement
    from cruxible_client.contracts.canonical import canonical_bytes
    from cruxible_client.contracts.claim_reads import ClaimReadBatchRequestV1
    from cruxible_client.contracts.principal_rendering import render_principal
    from cruxible_client.transport.http import CruxibleClient
    from cruxible_core.playbill.instance import PlaybillInstance
    from cruxible_core.playbill.keys import generate_client_principal_key
    from cruxible_core.playbill.signing import LocalEd25519ApprovalSigner
    from cruxible_core.server.registry import get_registry

    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "repo": str(repo),
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
        "working_tree": subprocess.check_output(
            ["git", "status", "--short"], cwd=repo, text=True
        ).splitlines(),
        "population": args.population,
        "history": args.history,
        "repeats": args.repeats,
        "claims_per_write": args.claims_per_write,
        "orphan_proposals": args.orphan_proposals,
        "server_profile": args.server_profile,
        "profile_write_phases": args.profile_write_phases,
        "scope": "SDK claim draft/prepare/submit/status; HTTP approval challenge/sign/submit; "
        "SDK accept; coordinate-pinned HTTP full Claim readback; explicit SDK refresh. "
        "Half of each batch revises seed Claim identities; remainder creates observations. "
        "Attached Git workspace; no floor or mirror. See server_profile for instrumentation. "
        "Cold means fresh daemon process, after separately timed SDK connect/orient; "
        "warm loops advance the accepted coordinate. Disk caches are uncontrolled.",
        "rows": [],
    }
    daemon = None
    client = None
    pb = None
    log = None
    try:
        profile = AdoptionFixtureProfile(
            name="served-write",
            subjects=min(100, args.population),
            claim_types=8,
            documents=2,
            query_definitions=2,
            seed_claims=args.population,
            generations=args.history,
            claims_per_generation=1,
        )
        started = time.perf_counter()
        fixture = build_fixture(root / "fixture", profile)
        operator = generate_client_principal_key(
            root / "operator-custody",
            principal_id="operator",
            kind="ordinary",
            forbidden_roots=(fixture.managed_root,),
        )
        recovered = PlaybillInstance.open(
            fixture.managed_root, trust_root=fixture.instance.trust_root
        )
        builder = _Builder(
            recovered,
            fixture.owner,
            approver=fixture.owner,
        )
        builder.sequence = fixture.head_sequence
        builder.accept(
            {"principals/operator.json": render_principal(operator.principal)},
            phase="http-operator",
        )
        parents = recovered.accepted_history()[-8:]
        for index in range(args.orphan_proposals):
            parent = parents[index % len(parents)].oid
            recovered._ledger.proposal_review_commit(
                tree_oid=recovered._ledger.tree_oid(parent),
                base_oid=parent,
                actor_id="owner",
                timestamp="2026-01-02T00:00:00Z",
                message=f"Disposable unsigned proposal {index}",
            )
        workspace = root / "fixture/workspace"
        subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "-c",
                "user.name=Benchmark",
                "-c",
                "user.email=benchmark@example.invalid",
                "commit",
                "--no-gpg-sign",
                "--allow-empty",
                "-qm",
                "Disposable benchmark workspace",
            ],
            check=True,
        )
        report["setup_seconds"] = time.perf_counter() - started
        report["seed_phase_seconds"] = fixture.timings.seconds
        report["seed_generation"] = builder.sequence
        instance_id = f"inst_adoption_{profile.name.replace('-', '_')}"
        registry = get_registry()
        registry.create_governed_instance_with_id(instance_id)
        registry.update_governed_instance_location(
            instance_id, fixture.managed_root, workspace_root=root / "fixture/workspace"
        )
        trust = registry.state_root / "trust" / f"{instance_id}.json"
        trust.parent.mkdir(parents=True)
        trust.write_bytes(
            canonical_bytes(fixture.instance.trust_root.model_dump(mode="json")) + b"\n"
        )
        socket = root / "daemon.sock"
        log = (root / "daemon.log").open("wb")
        started = time.perf_counter()
        daemon = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--daemon-entry",
                str(socket),
                str(args.output.with_suffix(".server")),
                "none"
                if not args.server_profile
                else ("write" if args.profile_write_phases else "accept"),
            ],
            cwd=repo,
            env=dict(os.environ),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(socket)), base_url="http://localhost"
        ) as probe:
            deadline = time.monotonic() + 90
            while True:
                if daemon.poll() is not None:
                    raise RuntimeError("daemon exited during startup")
                try:
                    if probe.get("/health").is_success:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError("daemon startup timed out")
                time.sleep(0.05)
        report["startup_seconds"] = time.perf_counter() - started
        started = time.perf_counter()
        pb = Playbill.connect(
            target=f"unix:{socket}",
            instance=instance_id,
            workspace=root / "fixture/workspace",
            context=root / "empty-context.json",
        )
        report["connect_seconds"] = time.perf_counter() - started
        client = CruxibleClient(socket_path=str(socket))
        for iteration in range(args.repeats):
            row = {"iteration": iteration, "temperature": "cold" if iteration == 0 else "warm"}
            phases = {}
            profiler = cProfile.Profile() if args.profile else None
            if profiler:
                profiler.enable()

            def timed(name, call):
                start = time.perf_counter()
                result = call()
                phases[name] = time.perf_counter() - start
                return result

            qualifier = f"served-benchmark-{iteration:04d}"
            revised_ids = {
                f"Claim:CLM-{_digest_id(profile, 'claim', member)}"
                for member in range(min(args.claims_per_write // 2, args.population))
            }
            subject_paths = tuple(
                sorted(
                    {
                        f"subjects/project.work_item/wi-{member % profile.subjects:05d}.json"
                        for member in range(args.claims_per_write)
                    }
                )
            )
            total = time.perf_counter()

            def make_draft():
                changes = pb.changes(rationale=f"Served benchmark batch {iteration}")
                for member in range(args.claims_per_write):
                    previous = f"CLM-{_digest_id(profile, 'claim', member)}"
                    revising = f"Claim:{previous}" in revised_ids
                    changes.claim(
                        subject=f"project.work_item/wi-{member % profile.subjects:05d}",
                        predicate=f"project.work_item.attribute_{member % profile.claim_types:04d}",
                        value="ready",
                        role="observation",
                        rationale=f"Served write-loop observation {iteration}/{member}.",
                        self_source=f"Benchmark {iteration}/{member}: ready\n",
                        supported_by=None,
                        copied_from=None,
                        qualifier=None if revising else f"{qualifier}-{member:04d}",
                        effective_period=None,
                        revises=previous if revising else None,
                        dispositions={
                            f"CLM-{_digest_id(profile, 'claim', index)}": "contradict"
                            for index in range(args.population + args.history)
                            if index % profile.subjects == member % profile.subjects
                            and index % profile.claim_types == member % profile.claim_types
                        }
                        if revising
                        else {},
                        subject_definition=None,
                        claim_type_definition=None,
                    )
                return changes

            draft = timed("draft", make_draft)
            intent = timed("prepare", draft.prepare)
            if intent.refused:
                raise RuntimeError(f"prepare refused: {intent.diagnostics!r}")
            timed("submit", intent.submit)
            status = timed("submitted_status", intent.status)
            if status.proposal_id is None:
                raise RuntimeError(f"submit did not produce proposal: {status}")
            proposal = status.proposal_id
            challenge = timed(
                "approval_challenge",
                lambda: client.prepare_playbill_approval(
                    instance_id, proposal, signer_id="reviewer"
                ),
            )

            def sign():
                signer = LocalEd25519ApprovalSigner.open(
                    signer_id="reviewer",
                    private_key_path=root / "fixture/reviewer-custody/reviewer.ed25519",
                    expected_public_key=challenge.signer_principal["public_key"],
                    forbidden_roots=(fixture.managed_root,),
                )
                return signer.sign(ApprovalStatement.model_validate(challenge.statement))

            attestation = timed("sign", sign)
            timed(
                "approval_submit",
                lambda: client.submit_playbill_approval(
                    instance_id, proposal, attestation=attestation.model_dump(mode="json")
                ),
            )
            receipt = timed("accept", lambda: pb.accept(proposal))
            if receipt.status != "accepted" or receipt.accepted_coordinate is None:
                raise RuntimeError(f"accept failed: {receipt}")

            assert receipt.workspace_advertisement.status == "updated", (
                receipt.workspace_advertisement
            )

            def readback():
                cursor = None
                found = []
                while True:
                    page = client.read_playbill_claim_batch(
                        instance_id,
                        request=ClaimReadBatchRequestV1(
                            at=receipt.accepted_coordinate,
                            subject_paths=subject_paths,
                            cursor=cursor,
                            limit=256,
                        ),
                    )
                    assert page.coordinate == receipt.accepted_coordinate
                    found.extend(
                        item
                        for item in page.claims
                        if (item.statement.qualifier or "").startswith(qualifier + "-")
                        or item.envelope.get("identity") in revised_ids
                    )
                    if not page.truncated:
                        break
                    assert page.cursor and page.cursor != cursor
                    cursor = page.cursor
                assert len(found) == args.claims_per_write, f"readback count: {len(found)}"
                assert all(item.statement.object.value == "ready" for item in found)
                return found

            observed = timed("readback", readback)
            timed("refresh", pb.refresh)
            row.update(
                seconds=phases,
                total_seconds=time.perf_counter() - total,
                accepted_coordinate=receipt.accepted_coordinate.model_dump(mode="json"),
                readback_identities=[item.envelope.get("identity") for item in observed],
                readback_grades=[
                    {
                        "identity": item.envelope.get("identity"),
                        "current_verdict": next(
                            fact["value"]
                            for fact in item.facts
                            if fact.get("schema_id") == "playbill.claim.current_verdict"
                        ),
                        "admission_statuses": [
                            account.status for account in item.admission_accounts
                        ],
                    }
                    for item in observed
                ],
                workspace_advertisement=receipt.workspace_advertisement.model_dump(mode="json"),
            )
            if profiler:
                profiler.disable()
                profiler.dump_stats(str(args.output.with_suffix(f".{iteration}.pstats")))
            report["rows"].append(row)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(row), flush=True)
        report["success"] = True
    except BaseException as exc:
        report["success"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
        if log:
            log.flush()
            args.output.with_suffix(".daemon.log").write_bytes((root / "daemon.log").read_bytes())
        raise
    finally:
        if client:
            client.close()
        if pb:
            pb.close()
        if daemon:
            daemon.terminate()
            try:
                daemon.wait(timeout=15)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait()
        if log:
            log.close()
        report["retained_state"] = str(root) if args.keep_state else None
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        if not args.keep_state:
            shutil.rmtree(root)
    print(f"Report: {args.output}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--daemon-entry":
        serve(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        main()
