# Code Review

## Verdict

Approved with comments. The standalone harness exercises actual SDK and Unix
HTTP paths in disposable instances with fresh custody. Its evidence-grade
limitation and profiler overhead are explicit and constrain the conclusions.

## Manual Review Priority

- Priority: P1
- Reason: Performance conclusions depend on faithful workload and timing scope.
- Suggested Human Review Focus: Repository selection, instance isolation,
  instrumented versus wall timings, mixed Claim revisions and grade reporting.

## Scope Reviewed

- Changed files: `docs/benchmarks/write-loop-served.py` (standalone benchmark).
- Untracked files: Before/after JSON, phase summaries and small diagnostic JSON.
- Tests examined: Successful two-write small smoke, mixed smoke, representative
  before/after runs and final write-phase diagnostic reported by implementer.
- Commands run: Parent read source, inspected complete raw results, extracted
  daemon profile phases and ran Ruff successfully. Implementer ran formatting
  checks and the actual harness workloads. No product test suite was duplicated.

## Findings

No blocking findings. The fixture's current but uncovered Claims measure lawful
accepted writes, not supported evidence. The report explicitly separates that
benchmark from the actual program-instance checkpoint.

## Complexity Assessment

Setup and the full readback scale with fixture population and contender history;
both are accounted separately. Acceptance profiling perturbs wall time, so the
paired profile scope must remain identical. Two samples are not a p95 estimate.

## Architecture Assessment

Benchmark-only service wrappers profile within the synchronous daemon worker,
not merely the client wait or ASGI loop. Repository paths select both client and
server code. Fresh generated keys, isolated state roots and cleanup of the
spawned daemon avoid touching live instance custody. No production hook is added.

## Test Coverage Assessment

The representative workload has nine revisions, nine creates, 1,000 seed Claims,
eight added history steps and 48 real unsigned orphan commits. Readback binds the
accepted coordinate and checks expected identities/counts/values; advertisement
must succeed. Later diagnostic metadata records actual evidence grades. Missing
concurrent load and remote mirror behavior are not claimed as covered.

## Documentation Assessment

CLI options, profiling, connection/setup exclusions and evidence grades are
documented. The final root wording qualifies unsupported coordinator-source
evidence to this fixture's admission policy rather than describing it as a
universal SDK limitation.

## Overall Contribution

Makes the measured acceptance bottleneck reproducible and distinguishes
component improvements from complete user-visible latency.

## Open Questions

None blocking this diagnostic artifact.

## Suggested Follow-Ups

Add a representative supported-evidence fixture and idle unprofiled repeated
runs before using this harness for latency budgets or service commitments.
