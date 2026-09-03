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
two decisions or split the set rather than to add a disposition. There is no
member ceiling in the model; how many changed members one daemon receives in a
single submission is that operator's admission bound.

An intent that publishes more than one Claim owns one publication expectation
per publishing member: read them from `Intent.publications` and apply each in
turn, each preparing against the source as it stands after the last.

### Evolving vocabulary in one generation

Needing a new distinction is an epistemic move, and "I need this distinction,
and here is everything it changes" is one decision, so it is one generation.
`ChangeSetDraft.succeed_claim_type(successor, dependents=[...])` succeeds an
accepted ClaimType inside the same set as the Claims that speak the new
vocabulary. The successor is a whole ClaimType that names the predecessor by
identity and pins its exact current digest; a ClaimType with no predecessor is
a definition, and `.claim_type(...)` carries that instead.

Members lower in dependency order -- definitions, then successions, then the
Claims that read them, then retirements -- and `dependents` must be the exact
reverse-pin closure of the predecessor over the tree at that point: the accepted
tree as this set's own definition members left it, read at their staged bytes
rather than the accepted ones. A sibling Claim is never a dependent. It lowers
after the succession, under the SUCCESSOR vocabulary, and lands as an ordinary
member of the same generation. Defining a ClaimType and succeeding it in one set
is not expressible either -- both members author the same artifact path, so the
set refuses `playbill.authoring.change_set_member_path_collision` naming the two.
An inexact closure refuses
`playbill.authoring.claim_type_succession_closure_incomplete`, whose repair
lists every required dependent at its exact digest. Four dispositions, spelled
by the SDK helpers `carry`, `rescind`, `retire` and `re_author`:

| Helper | Wire disposition | What the dependent becomes |
|---|---|---|
| `carry(claim)` | `successor` | Re-pinned to the successor, otherwise unchanged. |
| `rescind(claim)` | `retire` + reason `was-rescinded` | A tombstone that keeps the exact statement it was accepted with, under the vocabulary it was accepted under. |
| `retire(claim, reason=..., effective_until=...)` | `retire` | An attributed retirement, landing with the succession. |
| `re_author(claim)` | `re_author` | Said again by a sibling Claim member of the same set, lowered under the successor. |

The standalone route knows a fourth word, `invalidation`, deprecated there and
answered with a warning. A change set parses it and always refuses typed --
`playbill.authoring.claim_type_succession_disposition_deprecated`, whose repair
names both roads -- because lowering has no warning channel, so admitting the
word would coerce it silently. Say `retire` with a reason, or take the
succession to `cruxible playbill claim-type migrate`.

A re-authored dependent keeps its own identity, its slot -- the Subject it is
about and the predicate it speaks -- and its exact predecessor digest: the
sibling member is that Claim revised (`revises=` the same Claim ID), stated
under the new vocabulary, which is why `explain` on the re-authored Claim still
names what it succeeds. A sibling that moves the Subject refuses; a re-authoring
says the same thing again, it does not say it about something else. The sibling
is named once, by `successor_claim_id` -- the Claim ID it revises, which is the
dependent's own, and what `re_author(claim)` writes. A sibling that does not exist, lowers under
another ClaimType, or revises another Claim refuses
`playbill.authoring.claim_type_succession_re_author_invalid`, naming both member
indices and the Claim ID the dependent requires.

A successor that changes `object_kind` refuses `carry` for any live Claim
dependent: its object no longer says what the ClaimType now means, so each such
dependent must be rescinded, retired or re-authored. Tombstones are exempt --
a retired Claim keeps the shape it was accepted with.

Evidence admission policies ride the successor: a dependent carried to a
successor with a stricter policy is re-graded under that policy, not refused, so
the succession lands and the re-grading is visible in the dependent's admission
accounts afterwards. A successor whose policy admits no accepted capture
contract is linted on both roads: preflight carries the same
`playbill.claim_type.evidence_policy_admits_no_accepted_contract` warning
`cruxible playbill claim-type migrate` reports, in the result's `lint`, as a
warning rather than a refusal.

`cruxible playbill claim-type migrate` remains the operator form of the same
law -- one succession, no siblings to re-author. Both roads build the candidate
with the same function, so `carry` and `retire` produce the same bytes whichever
road authored them. `re_author` has no operator analogue by design: the
standalone route cannot carry a live Claim through an object-kind change, so
there it can only rescind and mint a new lineage, and the identity does not
survive. Saying a committed Claim again, in place, under a new vocabulary is
what the change set adds.

The 2026-09-02 `sec.vuln.affects_package` migration -- a literal-valued
ClaimType becoming subject-valued, which took three generations days apart --
is one set:

~~~python
edges = pb.changes(rationale="Name the package instead of spelling it.")
for work_item, claim in affected:                      # each existing Claim
    edges.claim(
        subject=work_item,
        predicate="sec.vuln.affects_package",
        value=package_ref,                             # a SubjectRef now
        role="observation",
        rationale="The advisory names this package.",
        revises=claim,                                 # keeps the identity
        self_source=advisory_line,
        supported_by=None, copied_from=None, qualifier=None,
        effective_period=None, dispositions={}, publish_to=None,
        subject_definition=None, claim_type_definition=None,
    )
edges.succeed_claim_type(
    subject_valued_affects_package,                    # pins the current digest
    dependents=[re_author(claim) for _, claim in affected]
             + [rescind(claim) for claim in never_true],
)
intent = edges.prepare()
~~~

One generation: the tombstones, the re-authored edges and any Claim the set
states fresh under the successor all land together, and `next` reports nothing
outstanding about the vocabulary afterwards.

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
