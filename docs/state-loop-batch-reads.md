# Bounded reads for state management

`World.prefetch` fills existing attribute caches from a bounded selection at the
World's accepted coordinate. It does not select a winner among contenders.

```python
world = pb.world()
tasks = [world.dev.roadmap_item[task_id] for task_id in selected_task_ids]
world.prefetch(
    subjects=tasks,
    predicates=[world.dev.roadmap_item.implementation_state],
    page_size=128,
    max_claims=4096,
)
for task in tasks:
    print(task.implementation_state)
```

An omitted predicate selection fetches all live Claims for the selected subjects.
Strings may use subject `kind/id` addresses or full `subjects/kind/id.json` paths;
predicate strings are fully qualified. Each request permits at most 256 subjects
and 256 predicates, and returns at most 256 Claims per page. Prefetch follows
explicit cursors, pins the evaluation time across pages, and installs caches only
when the selection is complete. Exceeding `max_claims`, a coordinate mismatch, or
non-advancing pagination raises a refusal without installing a partial cache.
Refreshing the connection invalidates the World through the existing coordinate
check.

`pb.claim_views(claims)` reads up to 256 Claim identities in one request, returning
the same typed fields as `pb.claim_view`, including admission capture accounts and
lifecycle. Identity reads preserve input order and include retired Claims, like
the existing individual read. Exact IDs or unique prefixes are accepted. Larger
identity collections must be explicitly partitioned by the caller.

The underlying `read_playbill_claim_batch` transport accepts
`ClaimReadBatchRequestV1` from `cruxible_client.contracts.claim_reads`. It requires
an accepted coordinate and either Claim identities or explicit subject paths.
Selector responses have `truncated` and `cursor` fields. Cursors bind the entire
selection, coordinate and evaluation time; they cannot continue another query.
The service binds one verified projection and reads shared admission artifacts
once for the selected batch. SQL selects matched identities without materializing
unrelated Claim views; this does not promise an indexed constant-time SQL scan.

Projection stamping uses `get_playbill_claim_backings(instance_id, claim_ids=...,
at=...)`. This separate bounded operation returns ordered `ProjectionClaimBackingV1`
values from original accepted artifact bytes, using the coordinate's compiler
codec. It checks exact identity and live lifecycle and computes the statement
digest. It does not evaluate support or evidence freshness. Missing and retired
Claims refuse the complete request. No new store or authoritative state is
introduced by either read API.
