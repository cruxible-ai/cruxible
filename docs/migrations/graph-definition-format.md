# Procedure definition format v2 (the graph format)

A procedure definition stops being a flat list executed in order and becomes a
typed DAG. This page is the operator's account of what changed, what breaks, and
what to do about it.

## What actually changed

The edges already existed. The four assert kinds are predicates on the edge to
the next step whose false branch is unconditionally "abort". Format v2 gives
those predicates a **second successor** — and that is the whole feature.

Nothing about a format-v1 definition moves. Its digest, its execution order, its
receipts and its stored bytes are unchanged, and a golden corpus of frozen
0.3.2-normalized dumps asserts that after every batch that touches the
definition model.

## The discriminator

One field decides the format, and it is the only signal:

```yaml
graph_format: 2      # absent -> format v1
```

Detection is never inferred from content. Procedure steps carry arbitrary
`dict[str, Any]` payloads, so a valid, current v1 definition whose provider
input happens to contain `next` and `parameters` would be mis-detected by any
content sniffer and routed through v2 digest rules — which would break v1
reproduction on live data. The corpus carries exactly those collisions as
regression entries.

Two rules govern the declaration:

| Situation | Outcome | Why |
|---|---|---|
| a graph construct with no `graph_format: 2` | **refused** | it would be digested under v1 rules AND would parse on an old core, which then mis-executes it |
| `graph_format: 2` with no graph construct yet | **warning** | well-formed, and sometimes deliberate: pre-committing a definition to the v2 digest namespace |

## The new node kinds

All of them are procedure-only. The configured-workflow grammar is untouched, so
a workflow cannot parse one — the type system is the refusal.

**Guard** — a predicate with two labelled successors.

```yaml
- id: exposed_gate
  guard:
    left: count(assets, items)
    op: gt
    right: 0
  on_true: build_patch_now
  on_false: severity_gate
  message: no exposed assets
```

`on_false` defaults to `$abort`, which terminates the run with the node's
message — exactly what the assert kinds it supersedes already do. `on_true`
defaults to falling through to the next step.

**Flow wrapper** — an unconditional successor override on a non-guard node.

```yaml
- step:
    id: build_patch_now
    provider: build_decision
    input: {action: patch}
    as: decision
  next: propose
```

It has no `id` of its own: the node's identity is the wrapped step's id. A
second, independently-settable id would be an unconstrained alias for one node.

**Project** — assemble one output object from named alias references.

```yaml
- id: shape
  project:
    fields:
      action: $steps.decision.action
      asset_id: $input.asset_id
  as: result
```

`returns` still names an alias and is still a top-level string. It is not
replaced by the projection node; the projection node is the thing it names.

## The predicate grammar is closed

```
pred    := cmp | "all_of" [pred+] | "any_of" [pred+] | "not_of" pred
cmp     := operand OP operand [ ":" value_type ]
operand := literal
         | "$input" ["." path]
         | "$steps." alias ["." path]
         | "count(" alias "," selector ")"
         | "exists(" ref ")"
         | "truncated(" alias ")"
         | "@param:" name
```

No arithmetic, no string functions, no user-defined predicates, no composition
beyond the three connectives, and no `$item` — per-item payloads do not exist at
branch time, so a predicate over one could be neither evaluated nor analysed.

A guard must declare **exactly one production**. `{}`, a half-written comparison
and two productions at once are each refused by name.

Connectives are short-circuit-free: every comparison is evaluated so the receipt
records every operand value. A branch nobody can explain afterwards is not
auditable.

`@param:` parses but is refused at compile time until governed parameters ship.

## Structural rules

Control edges are **forward-only**: a target must appear after its source in
`steps`. That makes acyclicity a syntactic check and keeps the step list a valid
topological order.

| Refusal | Meaning |
|---|---|
| unknown target | a control edge names a step that is not in this definition (and is not `$abort`) |
| back-edge | a control edge targets a step at or before its source |
| unreachable step | no path from the entry step reaches it |
| duplicate alias | two producers of one alias on the same path (format v2 only) |
| graph node in a repeat body | branching inside a bounded loop is out of scope |
| a wrapped write kind | the wrapper cannot smuggle an excluded step kind past the procedure subset |

Alias availability is computed as a MUST-dataflow — the intersection over
predecessors. An alias produced on one arm only is genuinely unavailable after
the join, and reading it there is a runtime failure no per-step check can see.

The duplicate-alias rule applies to format-v2 definitions only. A stored v1
definition with a duplicate alias compiles today, and refusing it now would be a
behaviour change on shipped instances dressed up as a new analysis.

## Two digests per node

| Flavour | Successors | Control targets | Role |
|---|---|---|---|
| **local** | no | no | the provenance subject — readings, findings, calibration |
| **subtree** | yes | yes, by edge label | definition identity, arm-root invalidation, near-duplicate retrieval |

Excluding control targets from the local preimage is what makes the arm-history
guarantee true: swapping a guard's arms changes the subtree and leaves the local
alone, so readings bound to a decision point survive a topology edit of the
question it still asks.

The definition digest is the **virtual root's** subtree digest. The root commits
`name`, `description`, both contracts, `returns`, `precondition`, `budget`,
`declared_tier`, `evidence_outputs` and `graph_format`; the steps arrive through
its single successor. Under v1 the root was the entry node's digest, so all of
those were outside the identity — a definition could change without saying so.

## Pins

Acceptance records one row per `(node, resolved dependency)`, each a **payload
plus its digest**. A bare digest can be compared and cannot be read.

| Kind | Payload |
|---|---|
| `provider` | all nine locked-provider fields |
| `query` | query name, definition digest, execution options |
| `artifact` | all four locked-artifact fields |
| `parameter` | name, revision digest, value type, and the **value** |

Verification is two checks. **Integrity** recomputes the digest from the stored
payload, for every kind, with no external input: a mismatch is corruption or
tampering. **Currency** recomputes the payload from the definition plus the
config and lock in force, for provider, query and artifact pins only.

Parameter pins have no currency check by design. The payload carries the value,
so it IS the executable dependency; the only external candidate is the live
revision, which is exactly what the pin exists to ignore.

A format-v2 procedure with no per-node pins is refused. A format-v1 procedure
with none is not — its coarse acceptance digests remain authoritative.

## Upgrading, and what breaks

**A 0.3 core cannot read a v2 definition, and that is deliberate.** It raises
`extra_forbidden` on `graph_format` at all three definition parse paths. The fix
is to upgrade the core, not to work around the refusal.

| Artifact | 0.3 outcome |
|---|---|
| v2 definition from `state.db` | loud refusal |
| v2 definition from a snapshot | loud refusal, twice over |
| v2 definition via state-diff | loud refusal |
| pins, node digests | unreachable — no 0.3 code references them |
| new columns on `procedures` | ignored harmlessly |
| run receipts carrying pin material | **readable** — the material lives in the root node's `detail`, which is arbitrary by contract |

Snapshots: the reader accepts format 1 and 2, because a 0.4 core must read 0.3
snapshots — that is the upgrade path. The writer always emits 2, so a 0.3 reader
refuses it loudly at its own exact-version gate rather than behaving differently
depending on what an instance happens to hold.

Storage migration `0009_procedure_graph` adds
`procedures.definition_format_version` and creates `procedure_node_digests` and
`procedure_acceptance_node_pins`. It is additive and auto-applies at startup.
Existing rows take format 1, which is the truth rather than a default.

## What is NOT sacrificed

Backwards compatibility is sacrificeable for a better product; **provenance
integrity is not**, and the two are routinely confused because both look like
"old things keep working".

- A stored digest is never recomputed under a different rule. Live v1
  definitions converge on v2 through a governed re-acceptance, which mints a new
  identity rather than rewriting the old one.
- The format-v1 digest function and its golden corpus stay forever. They are
  archival infrastructure, not legacy debt: receipts outlive procedures, so a
  historical receipt's `definition_digest` must still resolve after the last v1
  procedure is gone.
- Losing the ability to *author* v1 is fine. Losing the ability to *verify* it
  is not.
