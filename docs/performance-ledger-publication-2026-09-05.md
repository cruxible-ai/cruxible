# Asynchronous ledger publication

Implemented on `codex/state-loop-design`, based on `a0c6303c`. Core integration
is `e3581400`, with exact Git snapshot publication in `40420a8e` / `d4066adc` and
served status/barriers in `e3555f31`. No primary-branch changes, push, live mirror
configuration or daemon deployment were performed.

## Behavior

| Boundary | Before | After |
|---|---|---|
| Submission, approval, activation, withdrawal | Wait for remote publication after durable local work | Record a pending request and return; worker publishes |
| Remote push | Mutable refs, forced projections and non-atomic updates | Exact object IDs, atomic update, explicit leases, non-forced main |
| Publication status | Main OID read after push | Exact acknowledged ref snapshot and request sequence |
| Remote review wait | Coupled to the write | `ledger publish --timeout 60 --json` |
| Concurrent writes | Callers can push concurrently | Cross-process publication lock; pending work coalesces |
| Reopening | Last-attempt record | Schedule reconciliation from local ledger and durable evidence |

The signed generation ledger and accepted Git tree remain authoritative. Evidence
and notes retain their existing local durability semantics. Publication status,
request sequences and the queue are operational, rebuildable state. No acceptance
check or recovery proof was removed.

An explicit publish response names `wait_sequence`. It is acknowledged only when
that field is non-null and `published_sequence >= wait_sequence`. A newer request
can remain pending after this barrier succeeds. Scheduling failure has no wait
watermark; changing destinations interrupts the original destination's request.
Timeout returns pending/publishing rather than falsely claiming completion.
The existing `set-mirror` keeps its bounded initial publication wait.

## Measurement

| Median of three paired fresh fixtures | Synchronous completion | Background publication |
|---|---:|---:|
| Submit one-document candidate | 1.809 s | 0.495 s |
| Remote acknowledgement from submission start | 1.809 s | 1.817 s |

The fixture uses a real local bare Git remote and injects exactly one second of
transport delay before each push. Both modes use the same new publisher; the
baseline waits for its acknowledgement inside the submission callback. Setup is
outside timers. Each sample produces one candidate and one push, and verifies
that the final acknowledged refs exactly match the local public refs.

This isolates moving publication off the response path; it is not a benchmark
of a hosted forge or the old Git implementation. Remote publication itself did
not become faster. The unmirrored 162-Claim benchmark does not receive this
network-wait improvement.

- [Raw samples](benchmarks/ledger-publication-2026-09-05.json)
- [Reproducible harness](benchmarks/ledger-publication.py): run in this worktree
  with its root, `src`, and client package `src` on `PYTHONPATH`.

## Correctness and review

Exact snapshots retain their objects during the push. Known review refs use
expected-old-ID leases, main must fast-forward, and rejection is atomic. Unexpected
nonempty remote values refuse publication unless local ancestry or exact
settlement evidence proves safe recovery. The latter also repairs a lost record
of an uncertain successful attempt. A successful push records the captured
snapshot, never a later `main`.

All review reconciliation now shares a cross-process lock, refreshes stale
accepted epochs and renders approval notes under the candidate lock. This
prevents an older workspace reconciliation from resurrecting settled branches
or overwriting newer approvals. The review lock is separate from network I/O.

Worker state writes use a separate short lock and unique atomic temporary files.
A request arriving during worker shutdown cannot lose its wakeup. Persistent
status-write failures cannot cause unlimited worker replacement. Failed thread
starts cannot strand future explicit requests behind a dead thread handle.

Independent review found and drove regressions for these races, then approved
the final implementation. Named verification scopes:

- 28 exact Git snapshot tests across SHA-1 and SHA-256.
- 35 barrier/HTTP-client/CLI/permission and target-inventory tests.
- 58 combined local mirror, asynchronous worker, failure, review concurrency,
  proposal note/prose and approval concurrency tests.
- Three additive receipt/surface-inventory checks, three queue-status tests, and
  the existing mirror repair-loop case.
- Ruff, formatting, whitespace checks and focused Mypy passed for changed layers.

No full suite or golden journal corpus ran. The served surface was regenerated
once for the final additive interface under maintainer-authorized succession
`2026-09-05:asynchronous-ledger-publication`.

## Limits and follow-ups

- Each remote command has a 30-second deadline; discovery plus push can take
  roughly 60 seconds. Automatic attempts are bounded to three per request. New
  writes, reopening, or explicit publication can restart work afterward.
- Remotes must support atomic Git push. The exact refspec/lease argument budget
  is currently 64 KiB; larger snapshots report a visible failure instead of
  splitting the atomic update. Large review inventories need a scalable transport.
- Git leases protect known refs, not an entire namespace against an unrelated
  external writer creating a new ref during a push. Later publication detects
  unexpected remote refs. Acknowledgement describes this snapshot at that time.
- Normal completion, errors and timeouts clean temporary object pins. Process
  death can leave inert pins; their cleanup remains separate housekeeping.
- Compact projection markers, activation recovery optimization and SDK revision
  conveniences remain separate work. No renderer was introduced.
