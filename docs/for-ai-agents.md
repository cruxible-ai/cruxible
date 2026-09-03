# Operating Playbill as an AI agent

Playbill is designed so first-order discovery is cheap and exact. Start with
filenames, identities, subjects, and compact summaries. Expand governance,
provenance, evidence, and history only when the task requires it.

## Operating rules

1. Treat accepted coordinates as state and candidate coordinates as provisional.
2. Store bytes before proposing an envelope, but never describe CAS presence as
   acceptance.
3. Review the frozen candidate before asking a human or another agent to sign.
4. Never request, transmit, or place a principal private key in a repository.
5. Use explain for governance/provenance context; do not infer authority from
   presentation metadata.
6. Search for an existing subject or Claim before minting an adjacent concept.
7. Record contradiction as negative evidence instead of creating only a new
   positive inverse.
8. Treat diagnostic actions as links to governed proposal operations, never as
   mutation authority.

## Discovery ladder

Use the cheapest sufficient layer:

1. grep or file listing for known identities and terms;
2. list Documents or, once implemented, Claim/Procedure summaries;
3. inspect a specific accepted subject;
4. request explain summary;
5. request evidence detail;
6. read body bytes or full history only when necessary.

This avoids loading an entire structured graph into context merely to answer a
local question. Stable subject identities, ClaimType contracts, Procedure
contracts, and recall-only tags are the intended “double-click” points.

## Write lifecycle

One authoring intent is one changeset. `pb.claim(...)` authors exactly one
Claim; `pb.changes(rationale=...)` opens a changeset that `.claim(...)`,
`.claim_type(...)`, `.subject(...)` and `.retire(...)` write into, and
`.prepare()` compiles the whole set as one intent. The set lowers once,
proposes once and generates once, and it admits or refuses whole -- one
malformed member refuses the intent, typed to that member's index. A Claim in
the set may read a Subject or ClaimType the same set defines. Two sibling Claims
contending for one cardinality-one slot cannot be authored in a single set at
all: dispositioning one needs the other's Claim ID, which the daemon mints only
at create from the already-frozen payload, so that refusal asks you to merge the
two decisions or split the set rather than to add a disposition. Succeeding an
accepted ClaimType stays on the claim-type proposal route, where the migration
a succession demands is decided. There is no member ceiling in the model; how
many changed members one daemon receives in a single submission is that
operator's admission bound.

An intent that publishes more than one Claim owns one publication expectation
per publishing member: read them from `Intent.publications` and apply each in
turn, each preparing against the source as it stands after the last.

Subject-valued Claims are typed relationships, not string literals. Pass an accepted
`SubjectRef` (or canonical `<subject-kind>/<subject-id>` address) as
`Playbill.claim(value=...)`; preflight refuses a missing endpoint with the
`propose_subject` repair and refuses endpoint kinds outside the accepted ClaimType.

For Documents:

~~~text
store body -> propose envelope -> inspect/review -> prepare challenge
-> sign locally -> submit public attestation -> activate
~~~

Do not combine stages. A proposal can be refused. Optional or candidate-required
approvals may become stale. Activation can lose a compare-and-set race. Handle
each typed result rather than assuming success.

## Source alignment

Local files do not enter the event stream automatically. A catalog declares
which files are indexed. sources check validates current alignment without
writing. sources compile emits a frozen path-free bundle. sources propose
submits that exact bundle.

CI may run check/compile as a lint, but acceptance still requires an explicit
proposal and activation, plus any candidate-committed approval requirements.

## MCP and CLI

The MCP tool set is Playbill-only and mirrors the same service core as CLI and
HTTP. Use MCP for structured agent calls and CLI for human-readable review or
local key custody.

The default MCP profile is curated around the write-side loop. Use the expert
profile only when work requires lower-level document, Claim, ClaimType, or other
diagnostic surfaces that the default catalog intentionally hides.

Approval is intentionally split:

- prepare_approval obtains the exact challenge;
- the client signs it with a local key;
- submit_approval sends only the public attestation.

The convenience CLI command playbill proposal approve performs those steps
without exposing the key to the daemon.

## Fail closed

Stop and surface the typed refusal when:

- the accepted parent changed;
- the candidate digest or compiler digest differs;
- a candidate-committed approval requirement is unsatisfied;
- a principal-lifecycle transition lacks the lifecycle actor's own signature;
- a principal is revoked or outside its authority;
- a source file escapes its declared root or is a symlink;
- a requested proof detail is not implemented;
- a coordinate is provisional when accepted state was requested.

Recovery is a governed principal operation, not a bypass for ordinary approval.
