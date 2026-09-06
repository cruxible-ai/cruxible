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

## The world is typed

Do not pass strings. `pb.world()` reads the accepted vocabulary once and hands
it back as objects, so the names in your program are the names the daemon
accepted rather than spellings you hope match:

~~~python
w = pb.world()

w.sec.package.cryptography              # SubjectRef, by attribute
w.sec.vulnerability["cve-2026-69247"]   # SubjectRef, by index for any ID
w.sec.vuln.affects_package              # ClaimTypeRef for that predicate
w.sec.vuln.severity.high                # a value only this predicate admits
w.sec.vuln.severity.cardinality         # object_kind, cardinality, permitted_roles,
                                        # allowed_object_subject_kinds, referent_sensitivity
~~~

Dotted kinds nest, so `w.sec.package` and `w.dev.batch` are namespaces on the
same tree as the predicates. A Subject that does not exist refuses `AbsentSubject`
naming the kind, the ID and the coordinate; an enum member that does not exist
refuses naming every member the literal schema admits; a non-enum schema
validates its constructor before the wire, so `w.dev.batch.landed_at("...")`
refuses a 39-character digest here rather than after a proposal. A value minted
under one ClaimType refuses under another, naming both.

Reading back goes through the same objects:

~~~python
vulnerability = w.sec.vulnerability["cve-2026-69247"]
vulnerability.affects_package   # tuple[ClaimView, ...] -- live Claims under that predicate
vulnerability.claims            # every live Claim about this Subject
vulnerability.explain()         # the governance and provenance context
~~~

`pb.world()` refreshes first, so a world is always built at the instance's
current accepted coordinate, and it refuses once that orientation moves, under
the same law as every other typed ref: a name that resolved at one coordinate
may name something else at the next. `pb.world()` reads no Subjects at all; the
first Subject access of ANY kind reads every Subject of every kind in one list,
because the served verb takes neither a kind filter nor a cursor. A read-back
(`subject.claims`, `subject.<predicate>`) walks every page of the accepted list
rather than answering with the first one.

Where a name the daemon accepted is not a name Python can spell -- a segment
that is a keyword, a Subject ID with a hyphen, a predicate leaf that collides
with a member, an enum member that collides with a structure field -- the fixed
surface wins attribute access and the accepted name is reached by index:
`w.kind("dev.class")`, `w.claim_type("sec.vuln.import")`,
`w.sec.vulnerability["cve-2026-69247"]`, `vulnerability["sec.vuln.claims"]` and
`w.sec.vuln.severity("cardinality")`. Where a dotted name is BOTH a predicate
and a Subject kind -- which the vocabulary above happens not to contain -- the
predicate wins attribute access and the kind is reached as `w.kind("dev.batch")`
or as that predicate's own `.as_kind`.

`cruxible playbill world stub --out world.pyi` writes the world down as types,
stamped with the coordinate it was read at, so an editor and a model both
complete the real vocabulary instead of `Any`. The generated classes are closed,
so a misspelled kind, Subject, predicate or enum member is a type error rather
than `Any`; the header says how to bind the runtime object to them. Regenerate
it after every activation; a stub types one coordinate and carries no authority
over the next.

## Write lifecycle

One authoring intent is one changeset. `pb.claim(...)` authors exactly one
Claim; `pb.changes(rationale=...)` opens a changeset that `.claim(...)`,
`.claim_type(...)`, `.subject(...)` and `.retire(...)` write into, and
`.prepare()` compiles the whole set as one intent. `.subject(...)` and
`.claim_type(...)` return a ref to what they define, usable as `subject=`,
`predicate=` or `value=` in that same set, so a set that defines a Subject and
says something about it never retypes the address:

~~~python
draft = pb.changes(rationale="Name the package this advisory affects.")
package = draft.subject(w.sec.package.define("click"))       # a ref, in this set
draft.claim(
    subject=w.sec.vulnerability["cve-2026-69247"],
    predicate=w.sec.vuln.affects_package,
    value=package,                                           # the same set defines it
    role="observation",
    rationale="The advisory names this package.",
    self_source="affects: click\n",
    supported_by=None, copied_from=None, qualifier=None,
    effective_period=None, revises=None, dispositions={},
    subject_definition=None, claim_type_definition=None,
)
intent = draft.prepare()
~~~

Such a ref asserts no reference expectation, because the artifact it names does
not exist at the coordinate yet; the set lowers definitions before the members
that read them. The set lowers once,
proposes once and generates once, and it admits or refuses whole -- one
malformed member refuses the intent, typed to that member's index. A Claim in
the set may read a Subject or ClaimType the same set defines. Two sibling Claims
contending for one cardinality-one slot cannot be authored in a single set at
all: dispositioning one needs the other's Claim ID, which the daemon mints only
at create from the already-frozen payload, so that refusal asks you to merge the
two decisions or split the set rather than to add a disposition. There is no
member ceiling in the model; how many changed members one daemon receives in a
single submission is that operator's admission bound.

No member of a set publishes itself into a page. Writing a Claim's own body back
into the source it was authored from is the overlap the two-block-kinds law
refuses, and the `publish_to` option that did it is gone; a passage that states a
Claim is a source block (write the prose, capture the page, cite the span), and a
passage that reflects several accepted Claims is a projection block declared with
`playbill block repin`.

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
names every required dependent by identity -- a dependent carries no digest of
its own, so the digest each required row also reports is a read, not something
to copy back. Four dispositions, spelled
by the SDK helpers `carry`, `rescind`, `retire` and `re_author`:

| Helper | Wire disposition | What the dependent becomes |
|---|---|---|
| `carry(claim)` | `successor` | Re-pinned to the successor, otherwise unchanged. |
| `rescind(claim)` | `retire` + reason `was-rescinded` | A tombstone that keeps the exact statement it was accepted with, under the vocabulary it was accepted under. |
| `retire(claim, reason=..., effective_until=...)` | `retire` | An attributed retirement, landing with the succession. `was-wrong` for a false statement, `was-rescinded` for withdrawn authority, `superseded` for a statement that stood under a shape a later ruling replaced. |
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
        effective_period=None, dispositions={},
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
`SubjectRef` -- `w.sec.package.cryptography` is one -- or a canonical
`<subject-kind>/<subject-id>` address as `Playbill.claim(value=...)`; preflight refuses a missing endpoint with the
`propose_subject` repair and refuses endpoint kinds outside the accepted ClaimType.

For Documents:

~~~text
store body -> propose envelope -> inspect/review -> prepare challenge
-> sign locally -> submit public attestation -> activate
~~~

Do not combine stages. A proposal can be refused. Optional or candidate-required
approvals may become stale. Activation can lose a compare-and-set race. Handle
each typed result rather than assuming success.

## Reviewing a proposal

The ledger is Git, so review is Git. The daemon fetches its own refs into the
attached workspace on every proposal, so a reviewer diffs the candidate against
accepted state with standard tooling:

~~~text
git diff playbill/accepted...playbill/proposals/<proposal-id>
~~~

The candidate commit's message is the change set's own summary -- what it does,
then one line per member -- and the daemon's records are attached to the SAME
commit the branch points at, as Git notes read by name (`git notes
--ref=refs/notes/playbill-eval show playbill/proposals/<proposal-id>`; from a
clone of the mirror, `git fetch origin '+refs/notes/*:refs/notes/*'` first): `refs/notes/playbill-eval` carries the admission and the
evaluation verdict with every diagnostic behind a refusal, and
`refs/notes/playbill-approval` carries the canonical approval list with each
signer's own attestation. Nothing parses those messages; every fact an agent
should act on is in `proposal review --json` or in the notes.

`proposal review` without `--json` prints the pointer and the note refs rather
than re-rendering the change set. `playbill review open` / `review close`, which
materialized a detached worktree under `.playbill/review/`, are deprecated in
favour of the diff above and are removed in 0.6.0.

The proposal ref in that diff is keyed by proposal DIGEST, not by actor and
name: an actor's own transport ref `refs/proposals/<actor>/<name>` is extended
by every resubmission, while the branch a reviewer reads projects exactly one
evaluated candidate. Use the id `proposal list` prints.

An agent with no attached workspace reads the same refs from the ledger mirror.
`orient --json` carries `orientation.mirror_url` when the instance publishes to
one, and `playbill ledger clone-url` asks for it directly; clone that, and
`origin/main` is accepted state while `origin/proposals/<proposal-id>` is the
candidate. Local write completion does not imply remote visibility. Before a
remote review, run `playbill ledger publish --json` and require non-null
`wait_sequence` with `published_sequence >= wait_sequence`; retry or inspect
`detail` if the bounded wait is unacknowledged. The background publisher combines
pending work and reports pending/failure through `ledger_mirror_behind` in
`playbill next`. Publication receipts name the exact acknowledged ref snapshot.

The change set's own summary reaches that commit only if a door carried one.
`pb.changes(rationale="...")` and the `rationale` field on the tagless
change-set input both send it, and the daemon writes it as the candidate
commit's subject. Say why the set exists; what it does is already the roll
underneath.

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
