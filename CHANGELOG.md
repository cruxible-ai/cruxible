# Changelog

## Unreleased

- **Authoring reads reuse validated history and request narrower state.**
  Unchanged event bytes reuse their parsed validation across store instances;
  appended or altered events retain canonical, chain, and operation checks.
  Projection registration reads only current publication fields. Private event
  snapshots share identical committed payloads, and returned models remain
  caller-owned. Historical journal bytes and public wire contracts are unchanged.

- **Batch Claim authoring reuses a staged contender index.** Lowering parses the
  initial Claim population once and updates changed paths as members are staged,
  preserving sibling dispositions, retirements, and ClaimType succession output.
  Single-Claim reads also reuse accepted-history law evidence without caching
  fresh admission accounts or source-dependent evaluations.

- **Historical authoring events remain readable.** Working source observations
  preserve whether `source_content_base64` was absent, explicitly null, or
  populated. Reading older events no longer inserts a default null into their
  digest preimages and blocks unrelated authoring. Existing journal bytes and
  commitments remain unchanged; projection-window checks still apply.

- **Review is Git, because the ledger is Git.** A reviewer holding the ledger
  could see a change set's bytes but not what the daemon made of them, and the
  commit that carried it said only "Record Playbill proposal". Three things
  change together. The candidate commit's message is now the change set's own
  summary -- a subject naming what it does, then one line per member as
  `<disposition> <kind> <address> [qualifier]`, with the untruncated summary
  kept in the body when it did not fit 72 columns -- and the settled generation
  keeps `Accept Playbill generation N` over the same roll. It is prose and only
  prose: a guardrail states over the whole package that nothing asks Git for a
  commit message, so no fact can migrate out of the evidence store into an
  unversioned subject line. The daemon's own records travel beside it as notes
  on the candidate commit, written through the same call the generation
  descriptor has always used: `refs/notes/playbill-eval` carries the admission
  and the evaluation verdict with every diagnostic behind a refusal, and
  `refs/notes/playbill-approval` carries the canonical approval list with each
  signer's own Ed25519 attestation. Both are byte-identical projections of the
  proposal evidence store, which stays the source of record -- activation reads
  the store for policy and refuses to settle a candidate whose note disagrees
  with it, while a note that is merely absent is repaired rather than stranding
  a proposal admitted before the refs existed. `playbill proposal review` now
  prints that pointer and those ref names instead of a second rendering of the
  diff; `--json` is unchanged and remains the structured read, and `proposal
  approve` still renders the whole candidate, because that rendering is what a
  signature covers. `playbill review open` and `playbill review close`, which
  materialized a detached worktree under `.playbill/review/`, are DEPRECATED in
  favour of `git diff playbill/accepted...playbill/proposals/<proposal-id>` in
  the attached workspace; they still work and now emit the structured
  deprecation warning, and are removed in 0.6.0.

- **Evidence never comes from a projection block, and the daemon says so.** A
  page is a source: its bytes are captured, its capture is evidence, and a
  passage of it can be cited. A projection block inside that page is not --
  it is prose held to accepted Claims, and a Claim citing it would be a page
  attesting itself into concrete. That law was a client convention: a guard in
  the SDK, gated on the evidence role, that a copy citation, any other Claim,
  and any raw wire caller walked past. It is now enforced at the daemon, at
  both doors. Lowering resolves each citation's span against the cited source's
  own bytes and refuses `playbill.projection.evidence_from_projection` when it
  touches a stamped block, whatever the role or origin; the citation gate every
  proposal evaluation runs does the same over the capture's bytes, so a
  hand-built candidate tree is refused too. A page that declares a block is
  handed over with the observation (additive, optional `source_content_base64`,
  digest-checked) and kept, so the capture is the manifest of its own windows. A
  span that cannot be proved outside the windows of a source the instance
  registers blocks in refuses `playbill.projection.window_unverifiable` rather
  than passing. Overlap with the author's own prose outside every window stays
  allowed: that is what a source block IS.

- **`publish_to` is deleted.** The SDK option that authored a Claim and wrote
  that same Claim's body back into its own page minted a block whose single
  backing was the publishing Claim itself -- a source block projected as its own
  projection, which is precisely the overlap the two-block-kinds law refuses. It
  was also the only discoverable way to get a governed block into a page, so the
  discoverable road was the forbidden one. It is removed rather than deprecated,
  because a deprecation window on this shape is a window in which a page can
  still attest itself. An intent carrying `insertion_target` refuses typed as
  `playbill.authoring.insertion_target_removed`, naming the two roads that
  remain: a source block (write the prose, capture the page, cite the span it
  states) and a projection block (`playbill block repin` over accepted Claims).
  The mint, the preparation, the confirmation and their served verbs
  (`playbill authoring prepare-publication`, `playbill authoring
  confirm-insertion`, and the two HTTP routes and MCP tools behind them) go with
  it. Registrations an instance already holds stay readable, foldable and
  depublishable: `playbill authoring abandon-insertion` and `playbill block
  depublish` are unchanged.

- **A projection block is a held list and a watched query, and `block sync`
  reports instead of converging.** A stamp may now carry any number of Claim and
  artifact backings together with at most one query backing. The held list is
  what the block is accountable for; the watched query surfaces candidates for
  it, and when its semantic result digest moves `playbill next` emits the new
  `projection_candidates_changed` warning naming the rows that entered and left,
  repaired by `block repin --claim <entered>` to hold them or by `block repin`
  alone to re-stamp, which is the agent's explicit no. The ceilings that made
  the marker a layout constraint rise with it: 512 backings per block (from 64)
  inside a 128 KiB stamp (from 16 KiB), so a table of governed rows is not cut
  in two at a row number that means nothing to a reader. Because nothing renders
  a block, `block sync` no longer converges one: every block reports
  `unchanged`, `stale` (a held backing moved) or `dirty` (the prose moved away
  from the stamp), both repaired by a repin, and both counting as refusals for
  the exit code so an activation's closing sweep cannot answer clean over a page
  that has drifted. Two edits remain: `--detach`, and `--accept-local`, which
  says the prose in the page IS the block and records that by re-stamping the
  block on it -- the held list and declared coordinate untouched, the marker
  line rewritten, the declaration re-recorded. It writes because under this
  model the stamp is the alignment proof: a flag that merely silenced the row
  would claim an alignment nothing checked, and `next` would go on reporting
  the same page dirty. `--check` previews it as `would_sync` and writes
  nothing. `--discard-local` is its deprecated spelling -- it never discarded
  anything here -- accepted for one release behind the structured deprecation
  warning and removed in 0.6.0.

- **Every projection block is registered with the instance, whichever road
  declared it.** `block repin` records a declaration through a new served route
  (`POST /api/v1/{instance_id}/playbill/blocks/declare`), and the registration
  fold unions that with the bound publications an instance already holds, keyed
  on the pair the page itself names. `unregistered_projection_block` and the
  `workspace detach` refusal key on that fold instead of on whether a block id
  happens to begin `pub-`, which was a spelling the retired publication road
  minted and left every agent-declared block unchecked. `block depublish`
  releases either kind. The declaration is protocol state and commits nothing
  about what a block says.

- **Vocabulary that no producer could reach is gone.** The `self_published`
  citation origin had no writer anywhere in the product, so the
  `self_published_source_stale` row it fed could never fire and the coverage
  renderer's "published copy" line could never print; both are removed, and no
  accepted artifact carries the origin. `projection_backing_stale` stops
  answering `hand_edit` for a change a verb performs: `playbill.block.depublish`
  joins the repair vocabulary and the retired- and overturned-backing rows name
  it -- but only when there is nothing left to hold. A block holds a LIST, so
  one member of up to 512 retiring is repaired by a repin that drops it, and the
  registration is released only when every held member has retired or been
  overturned; the row carries a `backing_state` discriminator so the two are
  told apart. A registered block whose marker has left the page also names
  `block depublish`, whose repair used to be to restore the block a ruling had
  told the author to delete. The block-sync reasons that describe the removed
  publication road (`block_not_publication_origin`,
  `block_publication_registry_unavailable`, `block_successor_body_missing`,
  `block_successor_body_ambiguous`) go with it. Three others do not describe
  that road: `block_multi_backing` and `block_query_backing` were retired by the
  held-list rules, and `workspace_source_catalog_missing` never had a producer
  at all. Those stay in the vocabulary, unproduced, and are removed in a later
  release -- narrowing a served vocabulary is a wire removal, and
  deprecate-then-remove governs it. The same reasoning keeps
  `PlaybillBlockSyncReadResultV1`'s body fields optional rather than forbidden:
  the daemon never sends one, and a payload minted before this batch still
  parses. The repairs that named `block sync --all` for a drifted block name
  `block repin`, because sync converges nothing now. One row goes with the
  one-backing gate rather than with the vocabulary:
  `playbill.projection.backing_lineage_unreadable` was reachable only through
  that gate, and a block that may hold 512 backings cannot walk 512 succession
  chains on every queue read. Neither fault it named is unrefused -- a cycle in
  `predecessor_digest` is a cycle in SHA-256 and cannot be built, and more than
  one live successor is refused by `block sync` as `block_successor_ambiguous`
  with every candidate listed, and answered by `block repin --backing DIGEST`.
  `docs/cli-reference.md` says where each is refused.

- **An orient is a read again.** Deriving every Claim's verdict and resolution
  status crossed the client's own three-minute default timeout at a few hundred
  Claims, so `Playbill.connect()` could fail against a healthy instance. That
  derivation is memoized per process on the instance, the accepted coordinate,
  the exact Claim set, and a fingerprint of the two stores a verdict reads
  besides the accepted tree -- the content-addressed body store and the
  attestation ledger -- so a second read of the same state evaluates no verdicts
  at all. The evaluation instant is deliberately NOT part of that key: every
  served read stamps a fresh one, so keying on it would mean never serving a
  second read. Time is not ignored, though. A verdict is a step function of the
  instant whose only breakpoints are what it compares against -- a Claim's
  effective interval, a capture's observation, source expiry and freshness
  horizon, an attestation's validity window -- so the entry records the interval
  over which its answer holds and is served for any instant inside it, and
  crossing a breakpoint re-derives. `playbill next` asks the same derivation
  first, so one answer serves every fold in the request instead of the queue
  walking every live Claim itself. It is a cache and nothing else: bounded, cold
  after a restart, cleared on activation, and not a projection table. The
  registration fold is likewise read once per `next` rather than three times
  plus once per block.

- **The ledger's per-blob ceiling rises to 64 MiB, and the two member budgets
  become one number.** A change set's own ledger record costs up to 11,264 bytes
  per lowered entry, so a 4 MiB blob ceiling settled at most 372 entries while
  proposal admission advertised 5,000 -- and the disagreement arrived at
  activation, after the compile had been paid for. `max_change_set_record_bytes`
  rises with the read ceiling (a guardrail holds them equal), 5,000 entries
  project to about 53.7 MiB, and the budget a caller is told is the budget that
  settles. It also lifts a ceiling on evidence: a captured source over 4 MiB was
  admissible and then unreadable, and a source that size may now back a Claim.
  Raising a read limit is backward compatible in the only direction that
  matters -- every blob already accepted was written under the narrower ceiling.
  Operators mirroring a ledger to GitHub should note that GitHub warns over
  50 MB and rejects over 100 MB, which is a deployment concern rather than a
  second product ceiling.

- **An authoring anchor may quote a URL, and a lowering refusal is never a
  bare 500.** Every security feed interleaves reference URLs with the fields a
  Claim rests on, and the locator rule on source selectors -- right for every
  field that names an address -- applied to the anchor too, so no anchor could
  span "which CVE" and "what severity"; the compile route then died on it with
  an unhandled `internal server error` whose only diagnosis was the daemon
  log. An anchor is quoted source bytes and a URL inside it is bytes: the
  locator rule now exempts exactly that field (credential material is still
  refused there). A working selection the capture contracts refuse comes back
  typed as `playbill.authoring.working_selection_refused` naming the selector
  and the repair, and any other validator fault inside lowering is rendered as
  `playbill.authoring.lowering_invalid` with the validator's own message,
  logged server-side, rather than escaping the route as a 500.

- **A published block has a way out, and a worktree has a way to move.** Two
  verbs the lifecycle was missing. `cruxible playbill block depublish SOURCE_ID
  BLOCK_ID` releases the publication registration that demands a block's frame:
  `bound` was terminal, so a page that had been published once carried that
  block, with that id, forever, and removing the marker made `playbill next`
  emit a blocking row whose repair was to restore the block a later ruling had
  told the author to delete. Retiring a backing Claim now releases the markers
  it backs for the same reason. Depublishing is the FIRST of two steps -- it
  touches the registration and edits no page -- so until the marker leaves the
  file `next` reports it as `unregistered_projection_block` with the repair
  `remove_or_register_projection_block`, a warning rather than a blocking row.
  `cruxible playbill workspace detach --instance-id ID` releases a governed
  host from the Git worktree it is attached to. A worktree belongs to exactly
  one host, and re-binding it named two repairs: "archive and rebuild that
  host", which was not a verb, and "choose another Git worktree", which splits
  a repository in two. Detaching changes no governed state and deletes nothing;
  it is local-socket callers only, for the same reason attaching is, and it
  refuses while the host still registers published blocks in that worktree,
  naming them and the `block depublish` repair, because detaching under them
  would leave a page carrying markers no host owns. Both verbs are served
  (`POST /api/v1/{instance_id}/playbill/blocks/depublish`,
  `POST /api/v1/{instance_id}/playbill/workspace-detach`) and both reach MCP
  (`cruxible_playbill_block_depublish`,
  `cruxible_playbill_host_workspace_detach`). `block sync --detach` also now
  strips the markers of a host this worktree has left, instead of refusing with
  a repair that re-attaches it.

- **A Claim can be retired as `superseded`.** `ClaimRetirementReason` gains a
  third member beside `was-rescinded` and `was-wrong`. The two roads to one
  succession have to give one answer: a shape withdrawn in favour of a
  successor was neither rescinded nor wrong, and saying either of those about
  it put a false reason in the ledger. It is accepted anywhere a retirement
  reason is (CLI, SDK, MCP and the served retire and ClaimType-migration
  routes), and is an enum widening on input only: nothing that was accepted
  before is refused now.

- **The SDK carries exact content as itself.** `pb.claim(value=ExactContent(
  ...))` builds the exact-content object the wire already carried, keeping the
  bytes rather than a rendering of them, and refuses before the wire when the
  ClaimType is not an exact-content type
  (`playbill.sdk.exact_content_claim_type_mismatch`). The
  `claim-exact-content` example is on the served example vocabulary. The
  tagless CLI and MCP input gains an optional `content_base64` beside `text`,
  exactly one of which must be given -- so `text` is no longer a required
  field of that object, which is a request-side widening.

- **BEHAVIOUR CHANGE: an ordinary revision may not move a Claim's subject or
  its predicate.** `revises=` produced a successor that could point at a
  different Subject or assert a different predicate while claiming to be the
  same Claim's next version, which makes a lineage a Claim's history of
  something else. Both now refuse typed at preflight
  (`playbill.claim.revision_subject_moved`,
  `playbill.claim.revision_predicate_moved`), at the single evaluation
  chokepoint every authoring road reaches, and the refusal names the accepted
  subject or predicate and the written one, with the repair: put the accepted
  one back, or retire this Claim and author a new lineage about the Subject you
  mean. Machine-generated successions -- a ClaimType re-derivation, an
  attributed retirement -- move neither axis and are unaffected.

- **BEHAVIOUR CHANGE: a Provider egress receipt's `observer_backend` must be a
  namespaced lowercase token.** The field was a free string on a receipt a
  deployment reads to decide which observer's word it is taking, so
  `Cloud Proxy` and `cloud proxy` were two different observers with one
  meaning. It now matches `^[a-z0-9]+(?:[.\-][a-z0-9]+)*$`, which admits
  `cloud.netns-proxy` and refuses whitespace, empty segments and a trailing
  newline. Nothing in the tree produced a value this refuses; core is the only
  writer, and no persisted or pinned artifact carries the field. On the same
  lane, the provider-lane status gains a `not_applicable` member so a build
  that cannot run Provider code says so instead of reporting `unavailable`
  with a reason code that means something else.

- **An MCP client with no daemon is validated through the served request
  model.** The in-process door reached the facade directly, so a payload the
  HTTP route's request model refuses -- a control character in a decommission
  reason, say -- passed the MCP door and raised a raw pydantic error from
  inside the write. Both doors now decide through the same model object. The
  decision is identical; the rendering is not, and cannot be: in process there
  is no HTTP response to put a 422 in, so the local door raises a typed
  refusal naming the operation. The maintainer's call was the smaller change:
  `allow_local` is RETAINED for mutating verbs, because MCP-first clients with
  no daemon write through it today and taking it away is a capability removal.
  Retiring it for mutating verbs is still owed, and when it happens it goes
  through the structured-warning deprecation policy -- a warned release, then
  removal -- rather than breaking in place.

- **The frozen-surface pin now digests the request and response SCHEMA.** The
  served-surface inventory hashed FastAPI's `{"$ref": ...}`, which is a
  component NAME, so adding, renaming, retyping or removing a field inside a
  served request model moved no pin at all, and two routes carrying one model
  shared one digest. It now digests the resolved schema. Consumers pinning this
  artifact should expect request-body digests to move on a release that adds no
  route, verb or tool: that movement is the freeze working rather than noise.
  Per-tool `facade_operations` rows are a reachability closure too -- the verbs
  a handler names itself, the verbs its local adapter objects reach, and the
  verbs its sibling handlers reach -- so a deployment deciding per-verb what
  may be reached over MCP no longer reads an empty list for a tool that reaches
  the facade through an adapter.

- **A daemon can allocate more than one Playbill host per bootstrap secret.**
  Host creation was authorized only while the runtime bootstrap secret was
  UNCLAIMED, and every other credential a daemon holds is instance-scoped, so
  after the first `credential claim-bootstrap` nothing on the daemon could
  allocate a second host; the only repair was a restart, which mints a fresh
  secret at the same path and takes every hosted instance down with it. Creating
  a host now joins the daemon-wide operator actions the bootstrap secret
  authorizes repeatably, alongside server info, restart and stop. Claiming the
  admin credential stays one-shot. The refusal an instance-scoped credential
  gets on any daemon-wide operation now names what to present instead.

- **An open proposal that can never activate can be withdrawn.** A proposal
  refused at activation by a hard limit was admitted, evaluated, permanently
  unactivatable and permanently open, so `proposal list` accumulated tombstones
  with no verb to retire them. `cruxible playbill proposal withdraw PROPOSAL_ID
  --reason TEXT` (served, and `cruxible_playbill_proposal_withdraw` over MCP)
  writes one immutable withdrawal record beside the admission and reports the
  proposal `settled` with terminal reason `withdrawn`. It touches no accepted
  state and leaves every byte of the candidate readable, and it is terminal in
  fact: approval, activation and readmission all refuse a withdrawn proposal
  with `playbill.proposal_withdrawn`, naming who withdrew it, when, and why.
  The submitting actor may withdraw, and so may a daemon-wide operator, whose
  authority already allocates and stops hosts -- otherwise a proposal whose
  author's credential label was rotated would be withdrawable by nobody. An
  open or stale proposal may be withdrawn, a settled one may not, and a second
  withdrawal repeats the first answer rather than rewriting its reason.

- **A workspace-wide block sync no longer refuses on a source that merely
  quotes marker bytes.** `block sync --all` -- and the sync `proposal activate`
  runs as its last step -- inferred its targets from every catalogued source,
  so a captured report ABOUT the marker grammar was parsed as a projection page
  and refused `block_marker_malformed`, whose repair is to hand-edit a file that
  is exact accepted bytes. An inferred walk now notes such a source
  `skipped: source_not_projection_target` and moves on, so a lawful activation
  stops exiting non-zero over it. A path the caller names explicitly still
  refuses: there the caller asserted the file declares a block. So does a page
  that DOES declare one, in every selection mode -- a stamped page whose
  closing marker was deleted, or one repeating a block identity, is a
  projection target with a defect, and only a source declaring nothing at all
  is skipped.

- **A change set the ledger could not record is refused before it is
  compiled.** The ledger writes its record OF a change set as one blob holding
  an entry per CHANGED PATH -- so a ClaimType succession writes one per
  dependent it dispositions and a Claim retirement one per Claim in its closure
  -- measured against the per-blob ceiling; a set well inside every advertised
  receive budget could still exceed it, and only found out at activation, after
  a ten-minute compile that could not be reused. The admitted limits now
  advertise that ceiling and the largest measured cost of one entry across
  every member kind, and preflight refuses an oversized set typed
  (`playbill.authoring.change_set_record_too_large`), naming the projected
  record size, the ceiling and the entry count that fits: before lowering when
  the entries a set already declares exceed the bound, and on the exact lowered
  count -- still before the compile -- when they do not. Every durable identity
  computed over the receive limits -- a proposal id, a preflight certificate
  digest, an admission record's canonical bytes -- takes the RECEIVE bounds
  alone, so advertising a new ceiling does not restate the identity of anything
  written before it, and an instance upgraded across this release keeps reading
  the authoring intents and admissions it already holds.
  Compiling was also the step that could take the daemon out under memory
  pressure: an allocation failure during lowering is now logged and refused as
  `playbill.authoring.compile_budget_exceeded` instead of propagating untyped,
  and the daemon installs a fatal-fault handler writing to
  `<state-root>/daemon/logs/fatal.log`, beside its request log, so a death it
  cannot refuse is at least never silent in the log an operator follows.
  Chunking the record so a five-thousand-member set can be settled is a later,
  non-patch change.

- **A daemon discovers its isolated Provider executors from what is
  installed.** At start the daemon iterates the `cruxible.isolated_executors`
  entry-point group and registers every executor it advertises, so the registry
  that decides whether the shared hosted profile may run Provider code is built
  from the packages actually present rather than from an environment variable's
  claim. Discovery is all-or-nothing: every advertised executor is loaded and
  its registration read before any of them reaches the registry, so an entry
  point that cannot be loaded, does not implement the executor seam, or
  collides with a backend id already registered stops the daemon with a typed
  refusal -- naming the entry point, its target, its group and the distribution
  that advertises it -- and registers nothing, not even the executors whose
  entry points loaded before it. `server info` now reports the registered
  backend ids on the Provider lane; core registers none. Installation on the
  daemon's `sys.path` is the whole trust boundary; see
  `docs/hosted-runtime-image.md`.

- **Reads no longer re-read what an accepted generation already proved.** An
  accepted tree, a serving projection piece's verification, the Claim read
  history index and the durable publication fold are each done once and reused
  behind the same proofs they always ran; the resolution fold lists the
  projection once instead of once per slot, and every read that wanted one path
  asks for that path rather than the whole generation. Measured on a 740-Claim
  instance: `orient` goes from 868 s to 9.2 s on the first request after a
  restart -- which carries a once-per-process recovery replay -- and 3.0 s on
  every request after that; `list` and `search` from ~900 s to 3 s; `next` from
  29 min to 46 s cold and 27 s warm; and `block sync --check` over 26 blocks
  from 15 min to 6 s. No read answers differently: a tampered projection piece
  is still refused, and replay drops every memo.

- **BEHAVIOUR CHANGE: "overturned" now means a Claim lost a slot, not that it
  failed its own admission.** A many-cardinality slot selects every eligible
  contender, so a Claim that is not selected there lost to nothing; it reads
  `refused`. `overturned` is kept for a single-valued slot the Claim did not
  take. A governed block whose backing Claim was merely uncovered therefore
  stops reporting `projection_backing_stale`. The two depublication rows also
  carry the `hand_edit` operation their required change always described, since
  no verb republishes a retired or overturned backing.

- **BEHAVIOUR CHANGE: a source block and a projection block are never the same
  block.** Publishing a projection block whose backing Claim cites bytes of that
  same source inside the body being framed is refused, typed
  (`playbill.authoring.publication_claim_projected_as_itself`), naming the
  overlapping citations. Separately, a coordinator self-source citation is no
  longer skipped ahead of admission: a ClaimType whose evidence-admission policy
  names that contract for the Claim's role covers text authored in a governed
  page, and a ClaimType that does not name it still reads such a Claim as
  uncovered.

- **A defective coverage scan reports once per source, and names why.** When a
  source's own coverage scan never arrived, came back less than complete, or had
  a whole class of its evidence discarded by a per-source count cap, no citation
  to that source can be proved whatever the citation says. `next` now emits one
  `citation_source_unobserved` row for such a source, carrying
  `unobserved_cause: "source_scan_incomplete"`, the `source_scan_notes` the scan
  reported (a count-cap note among them when a cap did bite) and
  `collapsed_citation_count`, instead of one look-alike row per citation. A
  healthy scan is unchanged: a citation unobserved there is a finding about that
  citation and keeps its own row.

- **`CRUXIBLE_CLIENT_CONNECT_TIMEOUT_S`** (default 900 s) gives the single
  orientation an SDK `Playbill.connect()` runs its own read budget, separate
  from `CRUXIBLE_CLIENT_TIMEOUT_S` (default 180 s) and never below it, so a
  healthy but large instance cannot read as an unreachable server. Both knobs
  are documented in the CLI reference. `CRUXIBLE_CLIENT_TIMEOUT_S` is now
  validated: a value that is not a positive number is refused as a typed
  configuration error, where `0`, a negative number or unparsable text used to
  be accepted or raise an untyped error.

- **The accepted world is typed Python, not strings.** `pb.world()` reads the
  accepted ClaimType vocabulary once and hands it back as objects: dotted kinds
  nest (`w.sec.package`, `w.dev.batch`), a Subject answers by attribute or by
  index, a predicate is a `ClaimTypeRef` carrying its own structure, and a
  literal schema's enum members are values that can only state a Claim under
  their own predicate -- passing one to another ClaimType refuses, typed, at the
  builder rather than after a proposal. A non-enum schema validates before the
  wire, so a 39-character digest refuses at the call site. Every ref carries the
  coordinate the world was read at and refuses once the connection's orientation
  moves. Read-back (`subject.claims`, `subject.<predicate>`) walks every page of
  the accepted list, so a Subject with more Claims than one page still answers in
  full. `cruxible playbill world stub [--out]` writes the whole vocabulary out as
  a coordinate-stamped `.pyi` whose classes are closed, so a misspelled kind,
  Subject, predicate or enum member is a type error rather than `Any`.

- **BEHAVIOUR CHANGE: `ChangeSetDraft.subject(...)` and `.claim_type(...)`
  return a ref, not the draft.** They now answer a `PendingSubjectRef` /
  `PendingClaimTypeRef` at the current coordinate, usable directly as
  `subject=`, `predicate=` or `value=` elsewhere in the same set, so a set that
  defines a Subject and says something about it never retypes the address.
  Callers that chained on the return (`draft.subject(...).claim(...)`) must
  write the two calls separately; `.claim(...)` and `.retire(...)` still return
  the draft and still chain.

- **BEHAVIOUR CHANGE: a `Playbill` connection no longer demands a workspace
  source catalog.** The catalog is resolved the first time a surface actually
  selects from the working tree, so a read-only connection -- orientation,
  search, `world()`, the new stub leaf -- works from any directory. The refusal
  is unchanged and still typed; it now lands on `pb.file(...)` and the other
  workspace-selecting surfaces rather than at connect, which also means a
  malformed catalog is reported when it is first used.

- **BEHAVIOUR CHANGE: a fresh Playbill ledger is SHA-1, not SHA-256.** An
  instance initialized with no attached workspace now writes a SHA-1 Git
  ledger, because common Git viewers do not recognize a SHA-256 repository and
  a ledger nobody can open is not evidence anyone can read (maintainer ruling
  2026-09-03). An attached workspace's own format still wins. `playbill init
  --object-format` (request field `git_object_format` on HTTP, the SDK and MCP)
  chooses explicitly; an explicit value that contradicts the attached workspace
  refuses with the typed `playbill.init.object_format_conflict` before any state
  is written. Instances already initialized keep their pinned format forever and
  reopen unchanged.

- **Operations verbs for ending a daemon and an instance.** `cruxible server
  stop` asks a running daemon to shut down gracefully over the configured
  transport and reports what it OBSERVED: the daemon must stop answering, and
  when its state root is a directory on the machine running the command its
  lock must be free. It exits non-zero with the typed
  `cruxible.server.stop_not_confirmed` when the root was not released, and says
  plainly that release is not observable when the daemon is bound to TCP on
  another host. `server start` now takes an exclusive `flock` on
  `<state-root>/daemon/lock` before it opens any store, so a second daemon over
  one state root refuses with the typed `cruxible.server.state_root_locked`
  naming the holder's pid and transport; the kernel frees the lock however the
  holder died, so a stale file never blocks the next start.
  `cruxible playbill instance decommission` (HTTP `POST
  /api/v1/{instance_id}/playbill/instance/decommission`, MCP
  `cruxible_playbill_instance_decommission`, ADMIN) ends one instance's governed
  writes without deleting a byte: every governed write door refuses typed
  `playbill.instance.decommissioned`, reads keep serving at the accepted
  coordinate, `orient` and `next` report the terminal state, and archiving the
  directory stays the operator's own step.

- **Subject profiles list incoming relationships.** `subject get` (CLI, SDK,
  MCP) now returns the live Claims whose subject-valued object is the profiled
  Subject, grouped by predicate and carrying claim ids, so an `affects_package`
  edge from a vulnerability is visible from the package.

- **The Subject address is the canonical CLI argument.** `subject get
  KIND/NAME` and `subject history KIND/NAME` are the supported forms, and
  `explain <subject address>` resolves a Subject instead of answering a Document
  404. The two-argument `KIND ID` spelling still works and emits a structured
  deprecation warning; both surfaces are registered for removal in 0.6.0 (see
  `DEPRECATIONS.md`).

- **MCP `cruxible_playbill_init` carries the `seed` decision.** It gains
  `seed: bool = True` with the same typed `unseeded` row and repair as the CLI,
  SDK and HTTP surfaces, retiring the earlier "MCP always seeds" declaration.

- **Source-read receipts record the real on-disk names.**
  `SourceReadReceiptV1.relative_path` is now the component spelling the kernel
  confirmed rather than the spelling that was requested, and the new
  `requested_path` carries the request alongside it. Receipts written before
  this law still verify.

- **SECURITY: the shared hosted profile executes no customer code.**
  `runtime/execution_policy.py`'s gate is now enforced before every Provider
  child process, before Provider seed materialization, and at the served
  `procedure run` / `line run` / `provider seed` boundaries -- and before any
  tenant secret is resolved, so no secret material is materialized for a run
  that will be refused. Execution under `CRUXIBLE_HOSTED_SERVER_PROFILE=shared`
  is permitted only when an isolated executor is REGISTERED in the running
  build -- registration goes through a typed seam
  (`register_isolated_executor()` / `registered_isolated_executors()` taking the
  new additive `IsolatedExecutorRegistrationV1` contract), and core registers
  none, so the profile refuses with `customer_code_execution_unsupported` and
  the detail `isolation backend not implemented`. Naming
  `CRUXIBLE_HOSTED_ISOLATED_EXECUTION_BACKEND=docker` no longer re-enables
  spawning the Provider directly on the host, and an
  unrecognised non-empty profile value refuses typed with
  `hosted_profile_unknown` instead of being read as "not shared" (maintainer
  ruling 2026-09-03).

- **SECURITY: the terminal instance state closes the whole write plane.**
  Every governed write on a decommissioned instance -- approvals,
  curation rulings, Claim attestations, predictions and settlements, Procedure
  binds and Procedure/Line runs -- refuses typed, not only proposal submission.
  The decommission reason is bounded prose with no control characters, and the
  daemon state-root lock file is narrowed to `0600` even when an earlier daemon
  left it wider.

- **Playbill v1 is wire-frozen (P2-B5).** Governed Lines can trigger due
  occurrences over HTTP, the CLI, the SDK, and MCP under their accepted
  mandates; candidate review trees carry deterministic derivative cards;
  calibration readings succeed through explicit derivation; provider outputs
  verify against an external materialization seal manifest; and prediction
  declarations settle from accepted observation evidence or a governed
  terminal. The exact facade, HTTP, MCP, and CLI inventories are now pinned and
  move only with ratified succession evidence, every closed refusal vocabulary
  resolves to a structured repair -- a served command where one repairs it and
  an explicit hand edit otherwise -- every time-bearing field in the internal
  taxonomy declares one of four clocks, and the semantic compiler coordinate
  advances to semantic revision 19, labelled `p2-b5`, to commit the card
  renderer while every earlier coordinate stays installed and replayable. A
  Line occurrence's evaluation instant comes from the daemon clock; a caller
  may assert one only within the daemon's skew bound, which an operator
  configures in `daemon/procedure-runs.json` and which defaults to the
  ProcedureMandate skew.

- **Operator and agent workflows now close their setup and selection loops.**
  Server status reports exact compiler compatibility per governed host, and
  `playbill host show` exposes one host without acquiring authority. Proposal
  commands resolve full digests, unique prefixes, and current target refs
  through one typed read; Claim reads add a shared statement-first projection.
  A single client/daemon digest handshake now gates both CLI and SDK entry, while
  `server install-service` and `playbill workspace attach` provide credential-free,
  atomic local setup. Inherited-Git advisories are retained in affected JSON
  receipts, subject-valued Claim authoring preserves typed Subject objects, and
  projection repair detects body-only accepted revisions.
  Server instance counts now mean governed daemon hosts, and instance-scoped
  host inspection no longer discloses the daemon-local managed-root path.

- **Workspace files enter through bounded reads and governed Provider seeds
  (P2-B4).** The daemon resolves `workspace.file` only inside attached or
  operationally allowed roots. It decides on the real on-disk name of every
  component and denies Git metadata, Playbill control paths, custody and key
  files, and the whole daemon state root case-insensitively, so a capitalized
  spelling refuses on case-folding volumes exactly as it does elsewhere. Each
  authorized read commits its exact receipt alongside Provider invocation
  evidence. The real adapter remains in `cruxible-providers`; core ships its
  exact interface, local wheel/materialization pins, conformance double, and an
  ordinary proposal seed used by initialization and by CLI/SDK/HTTP. The
  Provider write family has no MCP parity yet, so `provider seed` is
  deliberately absent from MCP. Because that seed is a local materialization,
  initialization refuses rather than trusting an unchecked checkout; `playbill
  init --no-seed` (request field `seed`) is the explicit opt-out that creates the
  instance, skips the seed step, and returns a typed `unseeded` Provider-seed row
  whose `repair` names configuring `seed_materializations` and running `playbill
  provider seed`. MCP `cruxible_playbill_init` carries no such field and always
  seeds. This prerelease batch also corrects
  `PlaybillProviderInterfaceEntry` v1 in place to match the already-served
  ProviderInterface discovery row; the generated client snapshot pins that
  reshape. Core's governed `none` effect class is the no-external-mutation
  counterpart of the adapter stub's pinned `pure` vocabulary. The governed seed
  intentionally records only its verified `local_env` launch floor even though
  the external package manifest also advertises an unseeded container backend.
  Both new wires mint semantic revision 18, whose immutable label `p2-b4-u2` is
  what `server status` and `playbill host show` now report as the current
  compiler revision; revision 17 stays installed and still replays.

- **Client custody follows one Git-worktree boundary.** Initialization,
  approval, principal changes, and Claim attestations now anchor custody checks
  to the worktree containing the process CWD; outside a worktree, no workspace
  root is inferred. Inherited `GIT_DIR` and `GIT_WORK_TREE` selectors are ignored
  for that decision, with a typed advisory written to stderr when they select a
  different worktree.

- **Playbill workspace context and deterministic repair move together (PC-DF3).**
  Publication blocks can sync through the read-only sync-backing route while
  preserving local edits with `block_locally_modified`, `--discard-local`,
  `--check`, and `--detach`; accepted activation syncs attached workspaces last
  unless `--no-sync` is selected. Git workspaces now observe
  `playbill/accepted` and proposal remote branches, with `review open` and
  `review close` managing detached review worktrees. Attached hosts persist
  `.playbill/coverage.json` v2 atomically, with `--replace` required for a
  differing file, and `context show` separates config attachment from daemon
  registration. The `next` vocabulary now covers workspace attachment,
  projection repair, curation, and stale publication-block synchronization.
  The public `PlaybillBlockSyncOutcome` enum adds `skipped` for an unattached
  activation-driven sync; an explicit unattached `block sync` remains refused.

- **Source execution now lands versioned Capture evidence (P2-B4).** Source
  occurrences share the governed Provider driver and journal an erasure-safe
  output commitment before producing Capture v2 envelopes. Provider and
  Procedure Captures bind exact daemon-resolved producer receipts, while mixed
  Capture v1/v2 landing and replay preserve every retained v1 wire and digest.

- **Provider local-runtime fences are bounded and observable (P2-B2).** Provider
  child groups are killed and reaped on every completion path, with deterministic
  same-session and best-effort cross-session descendant sweeps, typed process-lease
  failures, manager-folded crash recovery, bounded lazy re-arm, and a non-blocking
  Provider-lane status. Local operators can tune the documented
  `daemon/provider-runtime.json`; non-Provider daemon surfaces remain available
  while that lane is degraded. Transient process-table failures remain
  diagnostic-only and surface their bounded count, retained ring occupancy, and
  last typed message in the existing Provider-lane detail.

- **Human-readable governed state and a single state root (PC-HR).** Current
  compiler artifacts use pretty canonical JSON at `.json` paths while frozen
  historical compilers retain their compact `.yaml` verifier. The daemon state
  root is now `~/.cruxible` (or `CRUXIBLE_STATE_ROOT`), the workspace floor is
  fixed at the containing worktree's `.playbill/floor`, and workspace Git refs
  are advisory. Pre-PC-HR nested state is refused with a typed re-seed error so
  neither instances nor the auth-required latch can be silently orphaned.
  `playbill floor export --output`, `floor_output.path`, `--state-dir`, and
  `CRUXIBLE_SERVER_STATE_DIR` are removed; claim-type migration output is human
  readable by default with `--json` for structured output.

- **Procedure compute-interior journals take a pre-release clean cut (PC-P2A).**
  Direct runs now bind replay-stable accepted-state material and semantic run
  identity in the v2 admission payload. Run journals written before this batch
  do not carry that payload and are intentionally unreadable by the new served
  reader; rebuild pre-release worlds rather than treating those journals as
  recoverable v2 runs. SDK runs use the current lane by default and select the
  read-only replay lane only when the caller explicitly supplies `at=`.

- **Claim admission now separates corroboration scope from freeze scope (PC-C4).**
  Corroboration requirements apply only to Claims authored under their own
  ClaimType, while freeze requirements continue to protect every applicable
  ClaimType on the same Subject. Claim v2/v3 admission laws advance in place
  for this prerelease correction. Next v2 deltas also report removed item IDs
  as presentation-only public wire without changing the whole-queue digest.

- **Claim retirement closure is one coordinated disposition.** A retiring
  dependent may advance a Claim-target pin by one verified succession hop only
  when that target also retires in the same complete ChangeSet; the earlier
  pre-release mixed-disposition ClaimType migration (live target successor plus
  dependent retirement) is deliberately withdrawn. Publication confirmation
  now binds the declared block frame rather than whole-file digests; prerelease
  v1-shaped in-flight insertion expectations must be recreated after this wire
  contraction.

- **Governed approval-policy spine and solo initialization (PC-C3).** New
  instances carry the `governance/approval-policy.yaml` singleton in the signed
  genesis tree. One ordinary principal is sufficient by default; operators can
  opt into creator-excluded independent approval with a second principal and
  `playbill init --require-independent-approval`. Tightening is an ordinary
  governed successor while loosening must satisfy the current independent
  policy. Local key directories remain attribution/repository-hygiene
  boundaries, not security boundaries; organization review rides repository
  protection, with real custody separation reserved for the Cloud broker seam.

- **Principal-authored Claim attestations (PC-ATT).** A signed evidence-plane
  ledger records exact-Claim observations against accepted coordinates, exposes
  idempotent append through SDK, HTTP, MCP, and CLI composition, and contributes
  deterministic outstanding-evidence and attestation-threshold rows to
  `playbill next`. An admin recovery command rolls forward the sole durable
  unpublished event after an interrupted append without rewriting governed state.

- **Scheduled 0.5.0 deprecations collected.** The already-absent legacy
  outcome-record and outcome-profile functions and the
  `ProcedureTransitionResult.warnings` string-list surface have been removed
  from the live deprecation registry. Their original schedules remain in
  `DEPRECATIONS.md` as release history, and newly registered deprecations now
  default to removal in 0.6.0.

- **Claim corroboration and role-free artifact governance (PC-C1).** Claim
  admission can now require daemon-bound, accepted-state QueryDefinitions and
  persists deterministic evaluation accounts for proposal and replay checks.
  Per-artifact authority-role wire is removed; principals instead declare the
  closed `ordinary`, `recovery`, or instance-owned `daemon` kind. `principal
  add` uses `--kind` rather than `--role`. PC-C3 subsequently moved approval
  policy into the governed singleton spine and restored solo initialization.
  This remains a pre-release clean cut and existing prerelease worlds must be
  rebuilt.

- **Activation attribution + lifecycle key-possession (PC-G12q fix round).**
  Activation mutation receipts now carry a required `activated_by` naming the
  governed actor (public client contract re-pinned); the HTTP request log
  independently records the credential for the same request. Principal
  lifecycle transitions require the proposing actor's own cryptographic
  approval with its current key (the affected principal only for
  self-rotation) — the one case where a creator's
  self-signature is mandatory rather than refused. PC-C3 now derives ordinary
  candidate approval behavior from the governed approval-policy singleton.

- **Playbill delegates authorization to repository ref governance (PC-G12q).**
  The in-daemon approval quorum is withdrawn: candidates require no approval by
  default, creator-suffices activation remains a separate attributed act, and
  non-creators may still record verified voluntary approvals. PC-C3 supersedes
  the interim forced-independent posture with a governed opt-in approval policy.

- **Playbill publication prepare retries now distinguish live persistence from
  terminal replay (pre-release compatibility note).** The exported
  `insertion_prepare_operation_v2_key` helper now requires the live expectation
  digest, preventing a stale source replay from serving a superseded preparation.
  Terminalizing prepare attempts retain a digest-free expectation-plus-observation
  identity so a lost prepared-to-expired or prepared-to-currency-changed response
  replays byte-identically after the terminal state change. Prepare events written
  by intermediate pre-release builds under the former preimage do not cache-hit;
  recreate any in-flight authoring intent carried across such an upgrade.

- **Playbill publication confirmations now require their discriminating wire tag.**
  The confirmation endpoint accepts the explicit v1 or v2 request variants; a
  legacy v1-shaped body that omits `tag` now receives a typed request refusal
  instead of being inferred. This is an intentional pre-release behavior change
  needed to keep the two confirmation protocols unambiguous.

- **Governance: role gates leave the hot path (PC-G12e).** Ordinary governed
  artifacts are now admitted by credential tier, principal identity, and
  semantic law — role labels no longer gate proposal, approval, settlement,
  or recovery, and every acceptance-law and compiler coordinate was re-pinned
  in place (pre-release: existing ledgers do not replay across this change;
  rebuild from fixtures). Creator self-approval refuses
  `playbill.approval.creator_forbidden`. Attestation-consequence thresholds lose their
  hard-coded minimum (declared `>=0`); threshold counting uses distinct
  principal identities in the queue fold only. Revoked keys can never be
  re-armed with the same material, and dormant role bytes are immutable
  pending a future governance redesign.

- **Playbill citation liveness now fails closed at every observation boundary.**
  Source-local scan proofs are withheld whenever the matching occurrence cards
  are clipped or rejected, partial scan budgets can no longer assert factual
  absence, and instance-invisible source observations disclose no liveness
  detail. Legacy drift observations now name `observed_window_digest` (instead
  of `observed_commitment_digest`) and require `claim_id`; a legacy observation
  for an invisible Claim is rejected with a typed 400 rather than silently
  ignored.

- **ClaimType migration now carries retired Claim dependents forward.** The
  shipped request previously omitted retired Claims from the dependent set.
  Callers must now disposition each retired Claim as `successor` so its stored
  adjudication evidence is re-derived against the successor ClaimType while its
  retired lifecycle is preserved. Omitting one refuses with
  `playbill.claim_type.migration_dependent_set_mismatch` and names the missing
  Claim; run v2 preflight to obtain the complete required dependent inventory.

- Add `playbill whoami` and a deterministic open/settled proposal inventory
  across HTTP, client, MCP, and CLI so agents can recover their writer identity
  and pending work without reconstructing credential or proposal context.

- Version the Playbill floor as `playbill-floor-export-v2`: JSON cards are now
  deterministically pretty-printed, and the v2 manifest inventory and floor
  digest bind those exact grep-friendly bytes while v1 readers remain intact.

- Curate the default Playbill MCP catalog around authoring and discovery, with
  `expert`/`full` retaining the complete surface, and render one deduplicated
  orientation header on human `playbill search`, `list`, and `orient` output.

- **PC-DEL3 removes the unreleased legacy Playbill wire and zero-use surfaces.**
  The unused native projection family is gone. Coverage results and source
  observations now expose only their live v3/v4 variants; publication
  authoring exposes only v2. The Claim-v1 direct write path and parser are
  retired in favor of ClaimInput through the AuthoringIntent coordinator, and
  seed apply is retired while the pure seed planner remains. The parked
  coverage hook remains non-executable. Claim-v1 compatibility had previously
  been announced through 0.5.0; this unreleased lineage removes it early, so
  pre-release fixtures and ledgers carrying `playbill-claim-v1` must be rebuilt
  or migrated before upgrading.

- **Flow-A authoring remains the canonical Claim path after the retirement.**
  Client-side source binding (`playbill authoring bind`), model-generated
  authoring examples (including the `cruxible_playbill_authoring_example`
  MCP tool), bare and qualified Claim reads, and the actor/repair-authority
  refusal texts all continue unchanged.

Every user-visible fix or feature adds its entry here in the same change
that lands it; entries move under a version heading when the release is
tagged. Work items for these changes live on the active release line in
the project's own state instance.

- **The 0.4.0 deprecation removals landed (BREAKING).** Every surface the
  registry stamped `removal_version: 0.4.0` and that a caller could stop sending
  is gone, along with its acceptance path, its registry entry, and the
  transport emitters that carried its warning. Removed: the `approve` feedback
  action (use `accept`), the `flag` feedback action (use
  `cruxible attest record --stance contradict`), the `group_override` feedback
  write path (retired outright — see below), and the retired declared-actor inputs
  `source`, `proposed_by`, `resolved_by`, and `opened_by` (the kind is derived
  from `actor_context`). These were accepted-and-ignored compatibility inputs
  through 0.3; sending one now FAILS, at every public boundary, by name — `422`
  on HTTP, a typed tool error on MCP (the tool does not run), a `ValidationError`
  on the client input contracts and the Python request models, and a
  `BadParameter` naming the offending item on `feedback batch`. Each refusal
  names the retired key and what to send instead. The refusal is scoped to the
  retired names: an unrelated unknown field is still tolerated, because
  forbidding extras wholesale is a wider contract change than this schedule
  promised. Retired ACTIONS refuse the same way, as an unknown enum member on
  HTTP/MCP and an unknown `--action` choice on the CLI. `FeedbackInputAction`
  is removed from the client contracts: the input vocabulary and the write
  vocabulary are the same thing again, so `FeedbackAction` is both.
  **Migration:** drop the arguments and pass `actor_context`; replace `approve`
  with `accept`. **Unaffected:** stored history still reads `approve` and `flag`
  rows, the derived `source` / `proposed_by` / `resolved_by` / `opened_by` READ
  projections still compute from the actor context, and edges already carrying
  `assertion.group_override` still read and still suppress a re-proposal — the
  flag is now write-once history that nothing can set again.
  The `DEPRECATIONS.md` and 0.3.0 changelog rows stay as the historical record.
- **`group_override` is retired outright, with no public replacement.** An
  earlier draft of the entry above named `force_review` as the migration; that
  was wrong, and the correction is recorded here rather than quietly dropped.
  **What is gone:** every way for a caller to SET `assertion.group_override` —
  the `--group-override` CLI flags, the HTTP and MCP inputs, the client kwargs,
  and the service write path behind them. There is no replacement input, on any
  transport. **What still works:** edges that 0.2.x/0.3 instances already
  stamped. `group/governance.py` still reads the stored flag, so such an edge
  still lifts a proposal's review priority and still blocks auto-resolution, and
  reads still report it. The flag became write-once history, not dead weight.
  **What `force_review` actually is:** a per-call boolean argument to
  `service_propose_group` (also raised by a workflow's `require_review` policy)
  that forces ONE proposal to be reviewed. It is Python-level and lives inside
  the service layer — it is on no HTTP route, no MCP tool, no CLI command, and
  no client method. It sets nothing on an edge and persists nothing, so it is
  not an equivalent of a durable edge-level override and cannot migrate one.
  **Future work:** exposing a per-proposal review forcer on the public
  transports is a separate, demand-gated work item, not part of this release.
- **The legacy outcome deprecations were rescheduled to 0.5.0.** The legacy
  outcome record and outcome profile functions (`outcome record`,
  `outcome profile`, `service_outcome`, `service_get_outcome_profile`, and their
  MCP/HTTP/client equivalents) were stamped `removal_version: 0.4.0` and were
  NOT removed; the maintainer has ruled the window moves to **0.5.0**, and their
  warnings now say so on every transport. The rationale is that the stated
  replacement does not exist yet: resolution contracts carry no equivalent of an
  outcome profile's coded vocabulary, its `required_scope_keys`, or the
  profile-drift analysis that `analyze outcomes` reports; four shipped kits
  configure `outcome_profiles`, and the blueprint `outcome_metric` hook names an
  outcome profile as its available target. Removing the only writer first would
  leave that config, the `outcomes` table, `list outcomes`, and
  `analyze outcomes` alive with nothing able to feed them, and porting the
  missing machinery onto the resolution-contract rail is post-Playbill-branch
  work. Both entries now state `removal_version="0.5.0"` explicitly instead of
  inheriting the registry default, so the new commitment does not move when the
  default does. `DEFAULT_REMOVAL_VERSION` itself moves to `0.5.0` for the same
  reason a schedule exists: 0.4 is the release under development, so a notice
  registered today and left on the old default would have been born past due.
- **Procedure definitions can be graphs.** A definition is a typed DAG rather
  than a flat list: guard nodes carry a closed predicate grammar and two
  labelled successors, flow wrappers declare an unconditional successor, and a
  projection node assembles one output object from named aliases. The format is
  declared by one field, `graph_format: 2`, which is the ONLY signal — content
  is never sniffed, because a valid existing definition whose provider input
  happens to contain `next` and `parameters` would be mis-detected and routed
  through the wrong digest rules. A definition using a graph construct without
  declaring the format is refused; declaring it without using one warns.
  Definitions carry two digests per node — a local digest that excludes
  successors and control targets, and a subtree digest that folds them in — so
  retargeting an edge no longer changes the identity of the decision point it
  leaves. The definition digest becomes a virtual root committing every
  definition field, closing the gap where a budget, tier or contract could
  change without the identity changing. Acceptance now records one pin per node
  dependency, each a payload plus its digest, verified as integrity (every kind,
  always) and currency (only what is executable); the run receipt carries the
  pin material in its root node's detail, so a run id recovers the exact
  accepted world. Format v1 is untouched: its digests, execution order and
  stored bytes are unchanged, pinned by a frozen golden corpus that includes
  real third-party definitions, and its verifier is retained permanently as
  archival infrastructure. A 0.3 core refuses a v2 definition loudly at every
  parse path rather than mis-executing it; snapshots read format 1 and 2 and
  always write 2. Storage migration `0009_procedure_graph` is additive and
  auto-applies. See `docs/migrations/graph-definition-format.md`.
- **Procedure blueprints have a document format.** A blueprint is a portable,
  digest-addressed document that packages a procedure library: its own fully
  qualified contracts, its reference-state/ontology dependencies, its query
  slots (read sockets that install a default named query), its compute slots
  (swappable stages declared by contract, with billing-mode compatibility
  constraints and an opt-in outcome-metric hook), and its procedures. The new
  `cruxible_core.blueprint` module parses and validates a document, computes a
  content digest over a canonical form plus an ordered attachment manifest, and
  lowers it into the artifacts an installer submits: a config-overlay fragment
  and concrete `ProcedureDefinition`s with slot references resolved from a
  caller-supplied binding map, checked against a caller-supplied provider
  catalog. Binding is fail-closed: a provider missing from the catalog is
  refused rather than assumed compatible, and a bound provider must match the
  slot's contract names, intersect its billing modes, and claim every
  capability tag it requires. Refusals are typed and field-pathed — one issue
  per violated constraint — and an unbindable slot lists the near-matching
  providers and why each failed. This
  release ships the artifact only — there is no installer, no trigger runtime,
  and no binding registry. `triggers:` and `pipelines:` parse and validate but
  refuse to lower; `invocation: manual` procedure libraries are the executable
  slice. Format reference: `docs/blueprints.md`.

- **Installed config objects now have an authoritative owner.** A new install
  ledger in `state.db` records which installed artifact (kind, id, version,
  digest) owns which contract, named query, procedure, or enum, together with
  the content digest it installed and the phase the install reached
  (`preparing` → `pending_acceptance` → `active`, plus `failed` →
  `rolling_back` → `rolled_back`). Every write is receipted, phase history is
  append-only, illegal transitions are refused with a typed error naming the
  phase the install is actually in, and one live owner per object name is a
  database guarantee. An install holds its names until it reaches
  `rolled_back`, and releases them in that same transaction: a `failed` install
  may already have written objects and still has to roll them back, so freeing
  its names earlier would let a fresh install claim objects the first
  install's rollback is about to remove. An install that mutated nothing pays a
  no-op rollback before its names are reusable.
  Composition ownership previously tracked only the upstream/local split, which
  could not support selective uninstall, dependency-blocked removal, or
  customer-edit-preserving updates. Read-only HTTP routes list installs and
  return one install with its owned objects and phase history; the uninstall
  precondition check reports declared blockers and states, in its own payload,
  the reference sources it cannot see. The write surface stays service-internal
  and there is no MCP tool: the installer that drives the sequence is not built
  yet.

- **Compute slots bind to providers through a ledger in state, not config.** A
  procedure pins a slot's INTERFACE (the contracts in and out); which provider
  fills that slot on a given install is now a receipted deployment record in
  `state.db`, with a monotonic revision per binding and full history retained.
  Binding refuses a provider whose declared contracts are not exactly the slot's,
  a billing mode outside the slot's allowed set (echoing the allowed values), and
  a third-party provider without recorded consent — the consenting actor and
  timestamp land on the binding, not in a config flag, and consent asserted with
  no actor context to attribute it to is refused rather than stamped
  anonymously. An unbindable slot reports every candidate that nearly matched,
  ranked by contract sides matched and then by failure count, naming every reason
  each one failed rather than only the first; the ranking rides on the error as
  structured data as well as in its text. The whole slot interface — both
  contracts, the billing allowlist, and the consent requirement — is pinned onto
  the binding at bind time and never revised: rebinding mints a new revision on
  the same binding, keeps the previous one readable, and is validated against the
  STORED interface, so a rebind that supplies a different one is refused naming
  what the ledger holds instead of redefining the constraints it is being checked
  against. One active binding per slot per install is a database guarantee
  (partial unique index), so two concurrent binds cannot both leave an active row
  behind. Readable over HTTP at `GET /slot-bindings` and
  `GET /slot-bindings/{binding_id}/history`; the bind, rebind, and retire verbs
  are service-level in this release with no CLI or MCP surface yet. Procedure
  runs do not resolve or record bindings yet — wiring slot resolution into run
  start is separate work, so nothing about run behaviour changes in this release.
  Storage migration `0008_binding_ledger`.

## [0.3.2] - 2026-08-06

- **Procedure reads show their run-ledger track record.** List and detail
  surfaces now attach a `track_record` block, so dead procedures are visible
  before an agent chooses one. Its verdict buckets are exhaustive — `succeeded`,
  `failed`, `refused`, `budget_exceeded`, and `in_flight` for started but
  unfinalized runs always sum to `runs` — so a procedure that exhausts its
  budget on every invocation reads differently from one whose invocations are
  still running. The block also carries `last_succeeded_at` and
  `top_refusal_reason`, the most frequent refusal classification. The summary is
  computed once for a whole list page. `linked_outcomes` remains reserved as
  null.

- **Procedure runs now advance the instance read revision.** The run ledger was
  classified audit-only, which was defensible while runs were readable only
  through their own listing. Deriving `track_record` from those rows makes them
  read state, so starting a run and finalizing one each bump `read_revision`,
  refusals included. Without this a procedure page could be read at one
  revision, a run could land, and the next page's continuation token would
  still validate against an unchanged counter — a paginated read spanning two
  states with nothing to detect it, and working-set records reading fresh while
  their buckets were stale. Continuation tokens and working-set freshness now
  react to procedure invocations the same way they react to any other write.

- **Refused procedure runs record why they were refused.** A `refused` run now
  persists a `refusal_reason` classification (`procedure_not_live`,
  `definition_digest_changed`, `tier_not_permitted`, `preflight_refused`,
  `precondition_evaluation_failed`, or `precondition_unsatisfied`) alongside its
  receipt, and procedure reads report the most frequent one. Existing instances
  gain the column on first open through storage migration
  `0005_procedure_refusal_reason`; runs refused before the upgrade keep a null
  reason and are excluded from the most-frequent count rather than being lumped
  into an "unknown" bucket that would outvote every reason observed since.
- **Core boundary traffic is measurable per instance.** HTTP routes, MCP tools,
  and locally invoked CLI service verbs now add call, error, serialized-response-byte,
  and total/maximum duration counters to one aggregate SQLite row per surface,
  without storing per-call events. `cruxible telemetry summary` and
  `GET /api/v1/{instance_id}/telemetry/summary` expose the counters and their
  earliest recorded timestamp at the read-only tier. Which surface a call lands
  under follows the boundary it actually crossed: **against a governed daemon,
  an MCP call reaches core over HTTP and is counted under the HTTP route name,
  so the MCP-tool dimension exists in local (direct-instance) mode only.** A CLI
  command records its emitted bytes and wall time under a `cli:<command>` row,
  while each service verb it invoked keeps its own measured duration. Refusals
  count as that instance's errors — permission-tier, ownership, and direct-write
  denials included; the one exception is a credential scoped to a different
  instance, whose refusal is not the addressed instance's traffic and is not
  counted against it. Recording never touches storage on the request path:
  observations aggregate in memory and a background flusher writes them, never
  waiting on a busy or unavailable store, so the underlying request result and
  timing are unchanged either way. A batch the store refuses is retried on the
  next flush rather than lost, and whatever capture genuinely could not keep is
  published on the summary as `dropped_observations` / `dropped_events` so an
  undercount is visible instead of silent. `cruxible server start` is excluded
  from CLI collection — the daemon's traffic is counted at the HTTP boundary
  that serves it.

- **Procedure proposals catch impossible input contracts before they enter the
  library.** Definition-time authoring lint now blocks a step reference such as
  `$input.transactions_arguments` when `contract_in` does not declare that
  field, naming the step, reference, and the contract's typed required/optional
  fields. A contract that sets `allow_extra` (including the built-in
  `cruxible.JsonObject`) accepts undeclared references, since the payload may
  legitimately carry them. This deliberately changes `propose_procedure`
  behavior: statically-wrong definitions that were previously accepted are now
  refused. The same lint also runs at accept time, so a proposal that was left
  pending before this change can now fail on `resolve --action accept` and must
  be fixed and re-proposed. The existing produced-alias check still blocks
  invalid `returns`. The lint reads only the step fields the runtime resolver
  itself walks, so literal prose that happens to quote a reference — an
  `assert` message telling an operator to supply `$input.foo` — is text, not a
  reference, and no longer blocks a definition that runs correctly.
  Non-blocking proposal warnings flag declared-but-unused inputs, read-implying
  names backed by side-effecting providers, stringified JSON-object step
  inputs, a whole declared string field handed to an `arguments` parameter
  (an opaque bundle the contract cannot validate), a procedure bundling reads
  with side-effecting steps or declaring more than five provider steps, and
  `max_provider_calls` headroom above the expanded provider-call count.
  `get_procedure` now returns `contract_in_schema` — the resolved input field
  shape (per-field defaults, enums, descriptions, and the nested `json_schema`
  a `json` field is validated against), the contract description, the
  `allow_extra` flag, and `input_example`: a worked payload carrying every key
  the caller must supply, which `cruxible procedure show` prints in human mode
  too. Run-time contract refusals are covered by the same typed
  required/optional schema echo, and both surfaces now share one requiredness
  rule — a field carrying a default is optional to supply, because contract
  validation fills the default before it ever checks optionality.

- **A Procedure author can withdraw their own pending proposal.** `withdraw`
  moves a pending definition to the new terminal `withdrawn` status through the
  same receipted transition as accept/reject, at the proposing
  (`governed_write`) tier — withdrawing another actor's pending proposal is a
  review act and is refused below `graph_write` with a typed
  `ProcedureWithdrawalRefusedError` naming the rule. `reject` stays distinct as
  the reviewer's verdict with its required reason; a withdrawal's reason is
  optional, and the terminal status records which of the two happened. A
  withdrawn definition is not live, so its name is immediately free to
  re-propose, and the refusal to supersede a still-pending definition now
  points at the new verb instead of leaving authors to invent renamed variants.
  Available as `cruxible procedure withdraw`, the
  `cruxible_withdraw_procedure` MCP tool, and
  `POST /procedures/{procedure_id}/withdraw`.

- **`cruxible batch-direct-write` shows identity warnings again.** The command
  printed neither a dry-run's nor an applied write's `identity_hint` matches in
  its human output, so a batch duplicating an existing entity's declared
  identity looked clean at the terminal while the `--json`, HTTP and MCP
  results all carried the warning. The command now uses the shared result
  emitter, and the preview surfaces the same warnings the apply would.

- **The empty `server` extra is gone.** `fastapi`/`uvicorn` moved into the base
  dependencies some releases ago, leaving `cruxible[server]` an extra that
  installed nothing; the runtime Dockerfile still asked for it. Nothing about
  what gets installed changes — `pip install cruxible` has shipped the daemon
  either way — but `pip install "cruxible[server]"` now warns that the extra
  does not exist instead of silently resolving. Drop the `[server]` suffix.
  Whether the server stack should move back out of the base install is a 0.4
  packaging decision and is deliberately not attempted here.
- **Rejected writes now teach the caller how to fix them.** Four authoring
  error classes became self-correcting, each measured as wasted retries in an
  agent benchmark run:
  - Datetime rejections (`observed_at` and every other typed temporal field)
    echo the accepted format with a copyable example, on both the HTTP request
    validation path and the runtime API argument checks.
  - Contract rejections naming an unexpected or missing field also list the
    contract's declared fields with type and required/optional (sorted,
    truncated past 40 with a count), so procedure and workflow inputs can be
    fixed in one edit.
  - Dangling-endpoint rejections name the recovery available at the entry
    point that raised them: a batch direct write can carry the entity, an
    attestation cannot.
  - Procedure tier refusals name the provider whose `procedure_access` forced
    the effective tier, and list the `declared_tier` values that clear it.

- **MCP tool descriptions describe the loaded kit.** Query tools name the
  config's named queries; workflow and procedure tools name its registered
  providers and contracts (with a short field preview), so an agent discovers
  the authoring vocabulary from the tool surface instead of prompt
  enumeration. Lists are truncated with a total. Tool schemas do not vary by
  kit. The kit is resolved from local state only — `CRUXIBLE_MCP_KIT_CONFIG`,
  otherwise the sole registered local instance and only in local mode — so
  `tools/list` still answers with no reachable daemon, falling back to the
  static descriptions. A server pointed at a remote daemon describes only what
  `CRUXIBLE_MCP_KIT_CONFIG` names, never a local instance that merely shares
  the host.

- **The hosted runtime image has a repeatable GHCR publish pipeline.** A
  dispatchable workflow builds `deploy/runtime/Dockerfile` from a named
  reviewed commit — refusing to continue unless the checkout is exactly that
  SHA — and pushes it under the immutable tag
  `runtime-<version>-<sha12>` with OCI source, revision, version, and created
  labels. `latest` is never published or moved: an already-published tag is
  reused rather than rebuilt, only an explicit registry "absent" authorizes a
  push, and a tag whose image was built from a different revision fails the
  job. The run summary and job outputs carry the image digest, and a
  post-push job pulls that digest and runs the runtime image suite against
  the published artifact via the new `CRUXIBLE_RUNTIME_IMAGE_REF` test
  override. Deployments pin the digest, not the tag.
## [0.3.1] - 2026-08-05

- **Entity types can declare deterministic identity keys at write time.**
  `identity_hint` returns a structured same-type duplicate warning without
  blocking the write, `unique_by` rejects normalized duplicates while naming
  the existing entity ID (including identity-changing updates), and
  `id_pattern` enforces per-type ID conventions. The shared normalization
  NFC-normalizes, case-folds, trims and collapses whitespace, and deletes
  punctuation; direct add/batch `identity_warnings` surface through both HTTP
  and MCP results. Matching scans same-type entities only and does not merge or
  perform semantic matching.

- **Ontology inspection is authoring-complete.** The canonical ontology view
  now exposes compact config-like entity and relationship property contracts,
  configured write policies, and stored instance counts, so an agent can author
  valid writes from the view alone. The request and response envelope is
  unchanged; CLI and MCP guidance updated.

- **Invalid Procedure definitions return typed validation errors.**
  `propose_procedure` surfaces definition-shape failures as structured 400
  responses with field-path messages on both the HTTP and MCP surfaces,
  instead of opaque server errors.

- **Unknown-provider Procedure errors list the registered providers.** The
  rejection names the valid provider set (sorted, truncated past 40 with a
  count), so an agent can self-correct instead of retrying blind.

- **Procedure runtime reference failures are typed and auditable.** Accepted
  definitions that cannot resolve a step reference now return a structured 400
  `QueryExecutionError` naming the failing step and reference, while atomically
  finalizing the procedure run as `failed` with its failure receipt. Failures
  that escape a step handler without an identified reference are typed the same
  way but name only the step id and kind — no reference is guessed. Procedure
  previews also reject `returns` values that are not produced output aliases.

- **Demo states publish to GHCR as immutable OCI bundles.** The hosted
  runtime image packages ORAS 1.3.2, the state-ref catalog gains the
  `banking-crux-demo` alias, and the publication recipes publish one release
  bundle under a dated immutable tag and retag that exact manifest to
  `latest` via `oras cp`, so the two references can never diverge. Recipes
  document digest-equality verification and the never-republish-a-dated-tag
  rule.

- **Registered source evidence now has compact, server-minted citation
  handles.** Registration, source-artifact list/get responses, and canonical
  `register_source_artifacts` workflow output expose stable revision and chunk
  handles. Relationship and governed-group writes accept `citation_handles`
  beside the unchanged explicit `source_evidence` form; Cruxible resolves them
  to the same full revision-pinned `EvidenceRef` before mutation guards run and
  computes artifact/chunk hashes from the registration. Handles are never
  floating aliases: superseded handles fail as `stale`, and unknown or
  digest-colliding handles fail as `unknown` or `ambiguous` rather than being
  dropped or guessed. Evidence is attached only when a write explicitly passes
  a handle.

## 0.3.0 — 2026-07-29

### Changed (BREAKING)

- **The `wiki-to-state` skill and synthetic wiki-import demo are removed.**
  Their corpus-conversion framing encouraged broad restatement of documents as
  graph state. Markdown remains a supported, content-hashed source-artifact
  format, and `scripts/import_markdown.py` remains as a deterministic batch
  registration helper; only operational claims and procedures that need an
  explicit lifecycle or executable consequence should be promoted from source
  evidence into governed state.

- **Claim feedback now uses `accept` instead of `approve`** across the CLI
  (`feedback record`, `feedback from-query`, and batch item actions), MCP tool
  schemas, HTTP request models, service inputs, and client contracts. During
  0.3, `approve` remains a deprecated input alias: it emits the standard
  structured warning and delegates to `accept`; it is removed in 0.4.0. New
  feedback rows store `accept`, while historical 0.2.x rows containing
  `approve` remain readable. The stored relationship review status remains
  `approved`; this is a public verdict rename, not a storage-status migration.

- **The `flag` feedback action is removed from the live write vocabulary** —
  from the canonical vocabulary on every surface (service, CLI
  `feedback --action`, MCP tool schema, HTTP request models, client contracts).
  As shipped it un-approved an edge to
  `pending` while storing no annotation, destroying the reviewer's actual
  signal at the moment it was given. Historical `flag` rows written by 0.2.x
  instances remain fully readable (the stored-record vocabulary still admits
  them; they render and move nothing). Submitting `flag` now refuses with a
  teaching message. During 0.3 it remains accepted as a deprecated refused
  alias so old callers receive the structured `{surface, replacement,
  removal_version}` warning rather than a schema-level unknown-value error; the
  alias never reaches a mutation and is removed in 0.4.0.
  **Migration:** record a doubt with
  `cruxible attest record --stance contradict` (MCP: `cruxible_attest`) — it stores
  the observation, its evidence refs, and its actor without touching review
  status; adjudicate with `accept`/`reject`/`correct`. Note the tier
  consequence: every remaining feedback action requires `GRAPH_WRITE`, so no
  feedback action completes at the `GOVERNED_WRITE` floor any more.

- **The self-declared `human`/`agent` axis is retired**: `FeedbackRecord.source`,
  `OutcomeRecord.source`, `GroupResolution.resolved_by`,
  `CandidateGroup.proposed_by`, `DecisionRecord.opened_by`, and
  `make_group_proposal`'s `proposed_by` were all caller-supplied, defaulted to
  `"human"`, and were never reconciled with `actor_context.actor_type`. They were
  not inert: the feedback and outcome profiles require a `reason_code` only for
  non-human writers, so an agent could skip the accountability rule written for
  it simply by declaring itself a person. Every one of those declarations now
  carries no signal: the matching `source` / `proposed_by` / `resolved_by` /
  `opened_by` parameters are dropped from the service functions and the runtime
  facade outright, while the MCP tools, the HTTP request models, the CLI
  (`--source`, `--opened-by`), and the client accept them as deprecated inputs
  that are ignored with a warning through 0.3 (removed 0.4.0 — see
  *Deprecated*). Readers derive the value from the actor context.

  The READ-side field names survive as DEPRECATED derived projections (see
  *Deprecated* below): `FeedbackRecord.source`, `OutcomeRecord.source`,
  `GroupResolution.resolved_by`, `CandidateGroup.proposed_by`, and
  `DecisionRecord.opened_by` are re-emitted, computed from
  `derived_actor_kind(actor_context)`. What is gone is the ability to DECLARE
  them. The retired request fields are accepted and ignored with the standard
  `{surface, replacement, removal_version}` warning rather than rejected.
  During 0.3 the removed parameters and hidden CLI flags remain deprecated
  input aliases across Python, CLI, MCP, HTTP, and client surfaces; their values
  are never honored, and the aliases are removed in 0.4.0.

  The `reason_code` requirement now keys off the derived kind and applies to
  everything that is not a resolved human — including `"unknown"`, because an
  unattributed write is absence of evidence, not evidence of a person.
  `RelationshipReviewSource` gains `"unknown"` for the same reason.

  **Migration:** drop the retired arguments from every call; supply an
  `actor_context` instead (auth-on daemons derive one from the credential;
  auth-off daemons default to the declared local operator). Kits declaring
  `proposed_by` on a `make_group_proposal` step must remove it — the step spec
  forbids extra keys. Persisted rows are unaffected: the SQL columns survive as
  denormalized projections written from the derived value.

  **Contract fields removed:** `FeedbackFromQueryInput.source`; the
  `FeedbackSource`, `GroupProposedBy`, and `GroupResolvedBy` type aliases.
  `StateHealthGroupsSection.auto_resolved_count` is superseded by
  `withdrawn_count` but stays on the contract as a deprecated always-0
  projection.

- **`auto_resolved` is retired as a group status**: it was a dead-end label. No
  code path transitioned a group out of it, no edges were created, no resolution
  row existed, and because `find_pending_group` and the pending unique index both
  key on `pending_review`, an auto-resolved group was invisible to the next
  proposal of the same signature — which therefore inserted a DUPLICATE pending
  row instead of rewriting it (`wi-group-auto-resolve-bug`; auto-resolve is
  enabled in shipped kits). Auto-resolution now runs the real approve transition:
  same receipt, same edge provenance, same resolution row as a reviewer-driven
  approve, marked `resolution_source="auto_resolved"`. `propose_group` returns
  `status="resolved"` with a `resolution_id`.

  Applying edges is `GRAPH_WRITE` while proposing is `GOVERNED_WRITE`, so a
  proposer below that tier does not escalate itself: the group stays in
  `pending_review` and the result carries `auto_resolve_deferred_reason`. The
  same happens if the approve itself is refused (a member fails validation, a
  guard rejects it) — the proposal does not fail, and the reason travels on the
  result.

  **Contract change:** `GroupStatus` gains `withdrawn`; `GroupResolution` gains
  `resolution_source`. `auto_resolved` stays in `GroupStatus` as a DEPRECATED
  read-only member (see *Deprecated*) — shipped 0.2.x kits wrote such rows and
  they must still load. They are NOT migrated to `withdrawn`: nobody withdrew
  them, and minting that act would fabricate a governance event that never
  happened. They are terminal, and `resolve_group` refuses them.

- **An empty-delta re-propose withdraws its pending group instead of deleting
  it**: under the default `pending_refresh_mode="replace"`, a re-propose that
  produced no delta used to DELETE the pending group and every one of its
  members, erasing governance history and leaving any receipt naming that
  `group_id` joined to nothing. The group is now marked `withdrawn` with its
  members intact; `withdrawn` sits outside the pending unique index, so the
  signature is free for a later proposal. The receipt operation type is
  `group_withdraw` (was `group_clear`; the old literal stays readable, see
  *Deprecated*). `propose_group` also accepts an optional
  `expected_pending_version`, the same optimistic guard `resolve_group`
  requires — now carried on the HTTP request model, the MCP tool, and
  `cruxible group propose --expected-pending-version`, not only the client.

- **Approve no longer moves trust**: a new approval CARRIES the signature's trust
  posture — status, reason, and the actor who set it — forward verbatim. It used
  to launder a reviewer's receipted `invalidated` into `watch`, twice (once when
  the resolution was created, once again at confirmation), discarding the
  judgement without a receipt, an actor, or a reason. Under
  `auto_resolve_requires_prior_trust: trusted_or_watch` that also silently
  re-armed auto-resolution for the very thesis a reviewer had just invalidated;
  under the `trusted_only` default it merely lost the reason. Trust changes only
  through the receipted `update_trust_status` verb.
  `GroupStore.confirm_resolution` no longer takes a `trust_status` override.

- **Config mutations and snapshot creation move up a tier**: `add_constraint` and
  `add_decision_policy` are ACTIVE CONFIG — once saved they adjudicate every
  later query and workflow, which is the authority `reload_config` carries — so
  both require `ADMIN`. `create_snapshot` MOVES the instance head, invalidating
  every outstanding state-pull apply guarded on the previous one, so it requires
  `GRAPH_WRITE`. All three now mint receipts (`config_add_constraint` and
  `config_add_decision_policy` carry pre/post config digests; `snapshot_create`
  names the head it moved from and to) and thread the resolved actor, which the
  facades previously computed and discarded.

- **Feedback adjudication requires `graph_write`**: `feedback accept`,
  `reject`, and `correct` decide a claim's fate — they make a non-live
  edge live, or retract one — so they now require `GRAPH_WRITE` even
  though the `cruxible_feedback` / `_batch` / `_from_query` tools
  themselves stay at `GOVERNED_WRITE`. Previously a single
  `governed_write` actor could stage an edge (attestation on an absent
  claim, or a `pending` write) and then accept its own proposal, reaching
  a live approved claim on a `proposal_only` type with no reviewer above
  it. The tools stay callable at `governed_write` so canonical actions reach a
  receipted `PermissionDeniedError` (HTTP 403) naming the required tier, but no
  feedback action completes at that floor. The former `flag` exception is now
  only a deprecated compatibility input; it refuses with the structured
  replacement warning on every write tier.

  **Migration:** any caller that accepts/rejects/corrects with a
  `governed_write` credential must present a `graph_write` one. This also
  overrides a type's config-declared `write_tier: governed_write` for
  those three actions — a type owner may lower who *direct-writes* their
  type, not who *adjudicates* claims on it. Auth-off local use is
  unaffected (the local default is `admin`).

- **Group resolution is gated at the service seam too**: the exported
  `service_resolve_group` now re-asserts `GRAPH_WRITE` inside its own
  mutation-receipt scope, so a direct library caller cannot reach the
  transition (and, with `stamp_existing`, bless a pending edge) below the
  tier the `cruxible_resolve_group` tool has always required. No change for
  MCP/HTTP callers; the refusal is a receipted `PermissionDeniedError`
  (HTTP 403).

- **`CRUXIBLE_REFUSE_DIRECT_WRITES` now spans the feedback channel**: while
  the kill-switch is set, the feedback actions that move an edge *into*
  accepted state (`accept` / `correct`) are refused alongside the direct
  write verbs, so freezing live writes can no longer be walked around via
  feedback. `reject` stays available because it moves an edge *out* of live
  state. The deprecated `flag` compatibility input refuses independently and
  never reaches this kill-switch.

- **Four MCP tools now return an object-rooted `{"result": ...}` envelope**:
  `cruxible_query`, `cruxible_query_inline`, `cruxible_list_queries`, and
  `cruxible_inspect_entity` each return a UNION of contract models, which
  derives to an `anyOf` at the schema ROOT. The MCP specification requires a
  tool's `outputSchema` to be object-rooted, and strict clients reject an
  `anyOf` root outright. The previous schema papered over this by pinning
  `"type": "object"` beside the `anyOf` root — a lie about a schema that had no
  `properties` and validated as an alternation. The union now sits under a
  required `result` property, which is both the correct shape and the shape
  FastMCP already generated for `cruxible_state_diff`, so all five union tools
  share one convention.

  The envelope applies to **both** halves of the tool response: the
  `structuredContent` object and the JSON in the text content block. Payloads
  inside `result` are byte-identical to what the handler produced before — only
  the nesting changed.

  **Migration:** read `payload["result"]` where you previously read `payload`.

### Added

- **Attestations: an observation channel separate from adjudication.**
  `cruxible attest record|list|queue|resolve` (MCP `cruxible_attest*`, HTTP
  routes, client methods) records one actor's dated, immutable observation
  about one claim tuple — stance `support`/`contradict`/`unsure`, with
  evidence refs and a note — without touching the claim's review status.
  `support` on an absent tuple creates a pending claim when both endpoints
  exist; `contradict`/`unsure` refuse to conjure the claims they dispute.
  `attest queue` surfaces live claims with open current-content
  contradictions; `attest resolve` appends a reviewer disposition
  (`upheld`/`corrected`/`invalidated`) while the original observation stays
  intact. Writes take an optional `idempotency_key` (actor + tuple scoped)
  for safe retries. This is the replacement the removed `flag` action points
  at: doubt becomes recorded signal instead of destroyed review state.

- **Resolution contracts: commit to the outcome before the acceptance.**
  `cruxible outcome open|resolve|dispose|list|due` (plus MCP/HTTP/client
  surfaces). `outcome open` declares, on a not-yet-accepted subject, what
  result would count — a free-text criterion, a check time, an expiry, and a
  pinned measurement (a named query frozen at definition digest AND execution
  options, or a set of attestations). Contracts are activated only by a
  `requires_resolution_contract`-guarded acceptance; a contract nothing
  activates expires unanswered. `outcome resolve` records
  `satisfied`/`contradicted`/`indeterminate` under evidence-clock discipline
  (the resolving receipt's or attestation's own timestamps settle timing,
  never the caller's word; a drifted measurement query leaves only
  `indeterminate`). One standing resolution per contract; `outcome dispose`
  upholds or overturns, an overturn re-opening exactly one further answer.
  `outcome due` is the attention surface (`due`/`overdue`/`contradicted`).
  The legacy `outcome record`/`outcome profile` functions are deprecated
  toward this (see *Deprecated*).

- **The loop surfaces are public HTTP contract.** All 22 previously hidden
  routes — procedures, attestations, outcome contracts, lifecycle verbs, and
  state diff — are exposed and pinned in the `http_surface` snapshot, and
  `cruxible-client` 0.3.0 ships a method for every one of them with its core
  pin aligned. What shipped as internal loop machinery during 0.2 is now
  surface area with compatibility obligations.

- **`cruxible kit repin`: first-class acceptance of intentional kit edits.**
  Editing a materialized kit used to strand the instance behind a digest
  mismatch unless `CRUXIBLE_KIT_DEV_RESOLVE=1` waved every check through.
  `kit repin` recomputes and re-records the runtime digest for a deliberate
  edit, making "I meant to change this kit" a receipted acceptance rather
  than an environment variable; the env override remains for CI.

- **Structured deprecation mechanics.** A dependency-free registry now owns the
  common `{surface, replacement, removal_version}` warning shape. CLI aliases
  emit one stderr line, MCP results use an existing `warnings` field or an
  additive `deprecation_warnings` key, and HTTP responses carry a `Deprecation`
  header (plus a body entry only for contracts that already expose `warnings`).
  `DEPRECATIONS.md` is the removal schedule, guarded against registry drift.

- **An agent-local working set: opt-in, non-authoritative read cache.**
  With capture enabled (`--ws` on supported `--json` reads, or
  `CRUXIBLE_WORKING_SET=1`), every entity and edge a read returns is also
  appended, in the compact profile, to a per-instance JSONL file under
  `~/.cruxible/working-set` — so re-finding a fact costs a grep instead of a
  re-query. Records carry the `read_revision` and config digest they were
  captured at; `cruxible ws status|verify|refresh|clear|path` manage the
  cache with honest freshness classification (fresh/stale/unknown — missing
  coordinates are never fresh). Credential-scoped instance keys keep
  different bearers' caches separate, the whole path chain is
  symlink-refusing and permission-tightened, and no write path or other
  command ever reads the cache. An opt-in prototype: records are hints to
  re-verify, never proof.

- **Working-set capture fidelity + control-plane catalog.** The agent-local
  working set gains persisted activation (`cruxible ws enable|disable` in
  the CLI context; precedence `--ws` > `CRUXIBLE_WORKING_SET` > persisted),
  a deterministic digest-stamped `catalog.jsonl` (`ws catalog`, regenerated
  by `ws refresh`) indexing entity types, relationship types, named queries,
  and state-held governed procedures, projection-preserving capture
  (explicitly projected scalar fields survive, bounded at 64 keys; edge
  corroboration retained), and one `config_status` read per process instead
  of per capture. Supersede merges props ONLY when both records carry the
  same concrete revision and config digest — cross-coordinate supersede
  replaces wholesale so stale values are never stamped fresh. **Upgrade
  note for 0.3 pre-release dev builds only:** working-set records captured
  on a build between the initial fidelity merge and the same-coordinate
  guard could carry merged stale fields that verify fresh; run
  `cruxible ws clear` once per affected context (the verb operates on the
  current context only; no released version wrote such files).

- **The KEV kits adopt the 0.3 mechanics.** `kev-triage` gains a
  `TriageDecision` type carrying the `outcome_tracking` convention and the
  first shipped `requires_resolution_contract` mutation guard: accepting a
  decision that tracks its outcome refuses until a resolution contract has
  committed, in advance, to what result would count. `not_applicable`
  remains an explicit opt-out, and `outcome_tracking` is frozen at proposal
  time so the accepting write cannot flip it past the guard. Two named
  queries land with it — `exposed_services` (from one CVE, traverse
  product → host → service: candidate reachability/blast radius as an
  auditable PATH — posture edges decorate the rows but do not filter them,
  so the posture-filtered work queues remain
  `asset_vulnerability_postures_requiring_action` and `owner_patch_queue`)
  and `open_triage_queue` (decisions still awaiting a reviewer, the read
  that pairs with the contract queues). `kev-reference` now registers the CISA
  KEV feed snapshot as the revisioned source artifact `cisa_kev_catalog`
  and pins every reference claim's evidence to `cisa_kev_catalog@{revision}`
  with a heading-path locator, so "which settled decisions cite evidence
  that has since changed" is a lookup rather than an investigation.

- **`register_source_artifacts` reports the revisions it wrote.** The step
  output gains `revisions` (`{artifact_id: artifact_revision_id}`), which is
  what lets a later step in the same workflow stamp `{id}@{ordinal}` onto
  the evidence refs it mints. Without it a workflow could only cite the
  LOGICAL artifact id, and an unpinned ref dereferences against whatever
  revision happens to be current — the exact silent staleness the revision
  pin exists to prevent. `source_artifact_evidence_ref` gains matching
  `artifact_revision_id` and `heading_path` arguments so kits spell the pin
  and the revision-stable locator the same way.

- **Opening a resolution contract requires an outcome guard on the
  subject's type**: `outcome open` (and its MCP/HTTP equivalents) refuses
  unless the config declares a mutation guard whose condition is
  `requires_resolution_contract` (compact sugar
  `require: {resolution_contract: true}`) and whose `entity_type` is the
  subject's. A contract opened on an uncovered type was provably inert —
  activation intents are minted only inside guard evaluation, so it could
  never be activated by an acceptance, appeared in no attention queue,
  and silently expired unanswered. The receipted refusal names the guard
  to declare. Coverage is checked at the type level only (a guard's
  `where` clause reads the candidate at write time and cannot be
  evaluated at open, so a guard scoped to a subset of the type counts as
  coverage), and idempotent replays of contracts opened while a guard
  existed stay replayable if the config later drops the guard.

- **`config validate` lints outcome-guard coverage**: validation now emits
  a WARNING when an entity type declares an `outcome_tracking` property
  and no `requires_resolution_contract` guard covers that type — the
  config says the type's decisions are outcome-tracked while nothing
  enforces it. A warning rather than an error: a config with no contracts
  at all is fine, and the adoption property may legitimately land a
  release before the guard. `outcome_tracking` is the adoption
  convention this release introduces (the guard teaching messages spell
  it); a kit expressing the same adoption choice under another property
  name is out of the lint's reach by design, where the guard's `where`
  clause is the source of truth.

- **Claims have a stable identity (`claim_id`)**: edges now carry a minted,
  opaque, immutable `claim_id` that survives pull-apply, snapshot/clone,
  publish→pull, and backup/restore. `edge_key` is demoted to what it always
  was — a per-load key, the wire disambiguator, and the ordering token — and
  is no longer identity. `claim_id` is exposed additively on edge payloads and
  accepted as an optional target disambiguator on attestation and feedback
  targets, where it takes precedence over `edge_key`; supplying both with
  disagreeing values is refused rather than silently resolved.

  **Upgrade notes.** A storage migration (`0004_claim_identity`) rebuilds
  `graph_relationships` around the new key on first open; it is atomic and
  lock-serialized, so a concurrent reader sees the old schema or the new one,
  never a half-upgraded table.

  **One-time working-set duplicate.** The working set is a persistent JSONL
  cache that used to dedupe on `edge_key`. It now dedupes on `claim_id` when
  present. Entries cached BEFORE the upgrade carry no `claim_id`, so an edge
  that is re-added after the upgrade can appear twice in the working set until
  its next refresh — once under the old `edge_key` identity and once under its
  claim identity. This is cosmetic, affects only the local cache (never graph
  state, receipts, or query results), and self-heals on the next working-set
  refresh; `cruxible ws refresh` clears it immediately.

  **Backup format 2.** New backups write their manifest as
  `backup-manifest-v2.json` rather than `manifest.json`, so a pre-identity
  Cruxible refuses the artifact at verification (as a missing required file)
  instead of installing a state database it cannot read. Backups written by
  earlier versions still restore normally.

  **Repairing a damaged upstream.** Re-applying the release an overlay already
  tracks is now refused as a no-op, so the documented repair for a locally
  damaged materialized upstream moved behind an explicit flag:
  `cruxible state pull-preview --repair` then
  `cruxible state pull-apply --repair --apply-digest ...`. Repair preserves
  claim ids.

- **Compact query payloads shed derivable include bytes.** Under
  `profile="compact"` only: configured-but-empty include aliases are omitted
  from query rows, retained include envelopes drop fields derivable from the
  map key, item list, or defaults (`exists`, null `limit`, false
  `truncated` — cardinality and counts stay explicit), and the graph layout
  interns repeated non-empty include maps into a top-level `include_sets`
  table that result refs index by integer. A deterministic five-result
  equivalent shrank 77.8% (7,895 → 1,751 bytes). Standard and full profiles
  are byte-identical to 0.2.x; the only shared-contract change widens graph
  `includes` to `dict | int`, which still accepts every prior payload.

### Security

- **MCP `tools/call` bypassed tool curation AND permission mode at the protocol
  seam.** The advertised surface was filtered only where in-process callers
  looked: `tools/list` returned the curated catalog, but the low-level
  `tools/call` handler dispatched straight into the FastMCP tool manager. Any
  client that knew a tool name — the names are public — could invoke a tool the
  server had excluded, regardless of `CRUXIBLE_MCP_PROFILE`,
  `CRUXIBLE_MCP_TOOLS`, or `CRUXIBLE_MODE`, because
  `advertised_tool_names()` is the only place either filter is applied to the
  tool surface.

  **Precise escalation.** LOCAL execution was still refused in depth:
  `runtime.api` calls `check_permission` inside every gated operation, so a
  local-mode call landed on that floor and a read-only server stayed read-only.
  The REMOTE dispatch path had no equivalent floor. All 92
  `_dispatch_remote_or_local` call sites forward to the HTTP client without any
  local permission check — there is not a single `check_permission` call in
  `mcp/handlers.py` or `mcp/tools.py`, and the remote branch never enters
  `runtime.api`, which is where the mode is enforced. The daemon authorizes
  what its own credential permits. So an MCP server started at
  `CRUXIBLE_MODE=read_only` and pointed at a `graph_write` or `admin` daemon
  could execute writes over the wire; the client-side mode that was supposed to
  hold that line was never consulted.

  The gate now sits on `ToolManager.call_tool`, the single chokepoint both the
  protocol handler and `FastMCP.call_tool()` reach, so the two seams cannot
  drift apart. Refusals name the tool, the reason (profile / allowlist /
  permission mode), and the environment variable that widens the surface.
  Regression coverage drives a real `ClientSession` over the wire rather than
  calling `list_tools()` in process, which is precisely why the original bug
  went unseen. Because the gate wraps private FastMCP internals,
  `validate_runtime_tools()` now pins those seams' signatures and asserts the
  wrappers are installed, so an `mcp` package bump fails at startup with a
  named reason instead of silently un-wrapping a security gate.

### Fixed (governance)

- **A withdrawn group can no longer be resurrected.** `resolve_group` accepted
  any status that was not `resolved`, and withdrawing PRESERVES the proposal and
  its members (that is the point of withdrawing rather than deleting) — so the
  preserved proposal stayed approvable by id afterwards, including once a fresh
  pending group for the same signature existed and had been reviewed on its own
  terms. Resolve now takes an allowlist: `pending_review` only, plus `applying`
  for an approve retry. Every other status is terminal.

- **Overlapping pending groups all see a direct-write conflict.**
  `find_pending_groups_for_tuples` collapsed same-tuple matches to the newest
  group, so a newer (or decoy) group absorbed the whole interaction: it alone was
  annotated with the conflict record and had its `pending_version` bumped, while
  an older group claiming the same edge stayed at the version its reviewer had
  read. That reviewer's `expected_pending_version` guard then never tripped and
  their approve went through against state that had already moved. Every live
  group claiming the tuple is now returned, annotated, and bumped.

- **Governed write-verb names are refused at the public direct-write seam.**
  `provenance_source` is caller-supplied on `add_relationships` /
  `batch_direct_write`, and the chokepoint EXEMPTS `workflow_apply` and
  `group_resolve` from the `proposal_only` refusal — so naming one let a bare
  direct write create brand-new `proposal_only` relationships and write
  `proposal_only` entities with no proposal, no workflow, and no reviewer in the
  act. (The content-binding refusal shipped earlier in this batch only covered
  rewrites of an already-approved EDGE.) Those names are now reserved: the public
  entries raise a receipted `GovernedSourceSpoofRefusedError` (HTTP 403). The
  genuine governed paths are untouched — group resolution and workflow apply call
  `apply_entity` / `apply_relationship` directly and never route through these
  entries.

  **Migration:** a caller passing `provenance_source="workflow_apply"` or
  `"group_resolve"` to a direct-write verb must pick a source that honestly
  describes the write, or go through `group propose` / the canonical workflow.

- **`workflow_apply` marks group-approval drift too, and the marker now reports
  CURRENT divergence.** A canonical workflow apply is a legitimate governed write
  and is not refused when it changes a group-approved edge — but it never routed
  through the direct-write group-interaction detection, so it overwrote approved
  content leaving no trace on the edge at all. Detection and stamping moved to a
  shared `graph/group_drift.py` that both write paths use.

  RULING (maintainer, 2026-07-25) on the marker's semantics, applied to both sites:
  `group_approval_drift` reflects divergence RIGHT NOW. It is recomputed against
  the approved content on every write and DROPPED when the content fully matches
  the approval again; a partial revert lists only the properties that still
  diverge. The approved baseline is still carried forward across writes (so the
  record says what the GROUP approved, not what the edge said last time). The
  previous accumulate-only behavior left a permanent stain: an edge that had been
  edited and then exactly restored still read as drifted forever. History of each
  excursion lives in receipts, not in live state.

- **Re-approving an edge makes the newly blessed content the drift baseline.**
  The third write path for the marker is `resolve_group --stamp-existing`, which
  blesses a surviving edge with the approving group's review and provenance. It
  copied the assertion with only `review` replaced, so a marker raised under
  group A survived group B's approval verbatim: the edge reported drift against
  a group that no longer owned it, over content B had just signed off on. The
  marker is now cleared on re-approval, which is the same ruling as above —
  divergence is measured against the NEWEST approval. (Approval never applies a
  proposed property set over a surviving edge; a member whose tuple is already
  live is skipped, so the blessed baseline is always the edge's current content.)

- **Decision-record terminal transitions are race-safe, and the raw setter is
  private.** `update_record`'s "is it still open?" check lived only in a
  preceding SELECT, so two writers on separate connections could both read
  `open` and both UPDATE — SQLite serializes writers, not read-then-write pairs.
  The loser silently overwrote the winner's terminal state, leaving a record
  whose status contradicted its own event log. The predicate now lives in the
  UPDATE (`AND status = 'open'`) with a rowcount refusal. The method is also
  renamed `_close_record` and removed from `DecisionStoreProtocol`: it was public,
  so any holder of a store handle could flip a record's status with no matching
  terminal event. `finalize_record` / `abandon_record` are the only paths.

- **Evidence refs pin the artifact revision they were made against.**
  `EvidenceRef` retained only the LOGICAL `artifact_id`, and dereference always
  resolved to the CURRENT revision — so a citation made against revision 1
  silently returned revision 2's text once the document was re-registered, even
  though revision 1's chunks, manifest, and archived bytes were all still stored.
  `EvidenceRef` and `SourceEvidenceInput` gain an optional `artifact_revision_id`
  (`{source_artifact_id}@{revision}`), which `resolve_source_evidence_refs` now
  stamps at citation time; `dereference_source_evidence` reads revision-scoped
  when pinned. Additive: old refs carry no revision and still work, falling back
  to the current one — but the result says so via `revision_unpinned` rather than
  letting a caller infer it from a matching hash. Exposed on the HTTP route, the
  MCP tool, the client, and `cruxible source dereference --revision`.

- **Config mutations are undone if their receipt does not commit.**
  `add_constraint` / `add_decision_policy` replaced the YAML immediately, while
  the receipt only became durable when the mutation-receipt boundary committed on
  exit — so a commit failure rolled back SQLite and left the ACTIVE rules changed
  with nothing naming who changed them. The prior bytes (and config provenance)
  are captured and restored on any failure inside the boundary.

- **Source-artifact drift history is no longer erasable by restoring the file.**
  `record_content_drift` cleared both stored fields on a clean read, so an
  artifact that was altered and then put back read as pristine — invisible to
  exactly the reader who needs it, someone auditing whether the evidence behind a
  decision was tampered with. Current drift state still clears (a stale marker on
  a restored file would misreport the evidence base), but a sticky
  `first_drift_observed_hash` / `first_drift_observed_at` pair is written once on
  the first drift and never cleared. Additive columns, migrated in place.

- **Replaying a pinned citation no longer manufactures a tamper record.** A
  revision-pinned dereference of a SUPERSEDED revision under the default
  `manifest_only` retention fell through to the artifact's local path — which now
  holds the NEWER revision's bytes. The hash mismatch was guaranteed and meant
  nothing, but the read reported `drifted` and recorded it, permanently stamping
  the sticky `first_drift_observed_hash` / `_at` pair on a revision nobody had
  touched. `DereferenceStatus` gains `revision_bytes_not_retained` for this case
  and no drift is recorded. Archived revisions are unaffected: their bytes are
  retained and still replay as `available`.

  **Migration:** a caller switching on `status` should treat
  `revision_bytes_not_retained` as "cannot serve this revision's bytes" (like
  `unavailable`), NOT as evidence of tampering. Register with
  `source_retention="archive"` when pinned citations must stay replayable.

### Documented

- **Under auth-on, every credentialed actor derives to `agent`** (maintainer
  ruling, 2026-07-25). A runtime credential is a `service_account`, so there is no way to
  be a human on an auth-on daemon today — and that is not an exemption: an actor
  deriving to `agent` owes a `reason_code` wherever a feedback or outcome profile
  requires one of non-human writers. Human-typed credentials (established at mint
  time, not declared per request) are the future path; the retired self-declared
  `human`/`agent` axis is not reopened. Recorded in
  `docs/runtime-auth-and-agent-roles.md`.

### Deprecated

Deprecate-then-remove applies to every shipped surface: these all still work,
each is annotated `Deprecated:` at its definition, and all are scheduled for
removal in the release after 0.3.

- **`GroupStatus` keeps `auto_resolved` as a read-only member.** Nothing writes
  it any more, but shipped 0.2.x kits (auto-resolve is enabled in them) persisted
  rows with it. Dropping the literal made `_row_to_group` raise on every
  list/get that touched one, so a single legacy row bricked group reads for the
  whole instance immediately after upgrading. Legacy rows are terminal and
  filterable (`cruxible group list --status auto_resolved`) so an operator can
  find them; nothing transitions them and nothing recreates them.
- **`OperationType` keeps `group_clear`.** Renamed to `group_withdraw`, never
  written again, but 0.2.x receipt stores hold rows carrying the old value and
  `get_receipt` raised on every one of them. A rename must not make an audit
  record unreadable.
- **Derived actor-kind projections re-emitted under the old field names.**
  `FeedbackRecord.source`, `OutcomeRecord.source`, `GroupResolution.resolved_by`,
  `CandidateGroup.proposed_by`, and `DecisionRecord.opened_by` return as
  computed, read-only values from `derived_actor_kind(actor_context)` — exactly
  what the matching SQL columns already store. Declaring them is gone; reading
  them is not. Read `actor_context` instead.
- **Retired declared-actor REQUEST fields are accepted and ignored.** Sending
  `source` / `proposed_by` / `resolved_by` / `opened_by` to a mutating surface
  emits the standard structured deprecation warning instead of rejecting or
  silently dropping the value. It is never honored — the kind is derived from
  `actor_context`.
- **`StateHealthGroupsSection.auto_resolved_count` returns, always 0.** An honest
  zero: no path can grow that bucket any more. Read `withdrawn_count`.

### Fixed

- **Two KEV goldens were permanently unstable.** The golden
  cross-section's generated-id normalizer matched a 12-hex-character
  suffix, but claim ids mint 16, so raw `CLM-<uuid4>` values passed
  straight through into byte-compared golden files
  (`asset_exposure_workflow.json`,
  `exposure_reconciliation_workflow.json`). Those two files could not
  match on any re-run, and the resulting churn read as real drift on
  every regeneration. The pattern now accepts a 12-16 character suffix
  and claim ids tokenize as `<CLAIM_N>`. Test-support only; no runtime
  behaviour changes.

- **The MCP tool listing no longer depends on the daemon.** A missing or
  invalid transport (`CRUXIBLE_REQUIRE_SERVER` set with neither
  `CRUXIBLE_SERVER_URL` nor `CRUXIBLE_SERVER_SOCKET`, or both set at once)
  aborted `create_server()`, so the MCP process died before it could answer
  `tools/list` and agent hosts saw an empty surface or hung waiting for one.
  The listing is now static — built from local metadata at construction, never
  touching a call path — and the transport failure is carried to the call that
  actually needs the daemon. Those refusals teach: what to set, how to start a
  daemon, and that a static listing is not evidence the daemon is reachable.

- **Acceptance binds content**: a group approval accepts an edge's PROPERTIES,
  not merely its existence. A later direct write that changes a group-approved
  edge's content is now refused on `proposal_only` types (with a message naming
  the approving group and pointing at the proposal rail) and stamped with a
  receipted drift marker on ordinary types, where facts legitimately change. A
  content-identical write is neither refused nor marked.

- **Direct-write conflict records are append-only and attributed**: a second
  conflict on the same tuple used to REPLACE the first, destroying the earlier
  `detected_at` and `receipt_id` — the record of how many times live state moved
  under a proposal. Records now append and carry the acting actor context. More
  importantly, `update_group_analysis_state` now bumps `pending_version`, so the
  reviewer's `expected_pending_version` guard actually trips: a resolve issued
  against the pre-conflict view used to sail straight through the one mechanism
  that says "the group changed during your review".

- **Provenance backfill no longer claims the toucher's channel**: touching an
  edge that carried NO provenance used to stamp the touching channel as the
  edge's ORIGIN, asserting a provenance the edge never had and turning "we do
  not know where this came from" into a confident, false claim. Such edges are
  now marked `source="unknown_backfilled"` with the touching channel recorded
  separately as `touched_by`.

- **Decision records are append-only and receipted**: `save_record` was a
  full-row upsert, so a finalized record could be silently rewritten back to
  `open`; and because `append_event` refuses once a record is closed while
  finalize/abandon transitioned FIRST, the terminal event for the closing act
  itself could never be recorded. Records are now insert-only with an explicit
  reopen refusal, the terminal event is emitted before the status guard, create/
  finalize/abandon mint receipts, and a failed event append is surfaced on the
  result instead of being swallowed into a log line.

- **Execution traces and source artifacts are insert-only**: a duplicate
  `trace_id` used to silently REPLACE the evidence that a prior provider
  execution happened; traces now refuse it and carry an `actor_context`.
  Registering a source artifact under an existing id used to rewrite the
  manifest that prior evidence refs were pinned against — it now writes a new
  revision with a supersedes pointer, closes the duplicate-check TOCTOU by
  holding the guard inside the write boundary, mints a receipt, and PERSISTS
  detected content drift instead of recomputing and forgetting it on every read.

- **Pending proposals are no longer clobbered**: a plain non-pending write
  onto a tuple whose edge is still `pending` used to resolve as an update
  and silently replace the proposal's properties in place while a reviewer
  was adjudicating it. It is now refused at the single relationship
  chokepoint with `PendingEdgeWriteRefusedError`
  (`pending_edge_write_refused`, HTTP **409**), so every write path —
  single, batch, typed lifecycle write, and canonical workflow apply —
  inherits it. The message names both exits: withdraw/re-propose through
  the pending path, or resolve the proposal through the review machinery
  first. Pending-onto-pending is unchanged (still the create-only rule),
  and post-acceptance updates work exactly as before.

  **Preview boundary:** `dry_run` previews raise these refusals with
  identical semantics but are excluded from the receipt guarantee — receipts
  record what happened, not what was previewed, and a preview persists
  nothing. This is the existing dry-run convention, unchanged here.

## 0.2.8 — 2026-07-21

### Added

- **Gate evaluation receipts**: every `gate check` mints a daemon-side
  `gate_evaluation` receipt inside one write transaction — gate, kind,
  candidates, per-candidate outcomes with satisfying entity IDs, verdict,
  and the observed `(instance_id, read_revision)`; refused evaluations
  (exit-2 paths) are receipted with the reason. Evaluation observes a
  single revision (concurrent mutations wait rather than splitting the
  verdict). New server-side check endpoint
  (`POST /api/v1/{instance_id}/gates/{name}/check`) plus a typed client
  method; CLI candidate sourcing, output, and exit codes unchanged.
- **Loud write targets**: every mutating CLI verb prints a one-line
  stderr target (`instance @ transport`) with provenance markers for
  remembered-context vs explicit flags. JSON stdout stays clean; reads
  stay silent.
- **Write-verb flag consistency**: `relationship add/update` accept
  `--type`; `entity add --id` is optional when the props carry the schema
  primary key (conflicts fail naming both values).
- **`read_revision` on stats**: the stats surface carries the freshness
  counter as a first-class field; property-only updates provably advance
  it.
- **Compact JSON output**: `--json-compact` (or `CRUXIBLE_JSON_COMPACT=1`)
  emits single-line JSON through one central helper; pretty stays the
  default.

### Changed

- **CLI startup is ~8x faster on the read path**: lazy per-command
  loading (`--version`/`--help` ~60ms, previously ~500ms), and
  `cruxible_core.errors` no longer imports the HTTP client stack —
  exception identity across the core/client boundary is preserved via a
  shared dependency-free error base. Import-graph regression tests pin
  the win.

### Fixed

- **Kit bundles ship with every release**: the tag workflow deterministically
  rebuilds bundles, refuses digest drift from the committed manifest, and
  idempotently uploads them to the GitHub release. CI also checks every
  manifest URL once its version tag exists, preventing a published package
  from pointing all built-in kit aliases at missing release assets.

## 0.2.7 — 2026-07-20

### Added

- **Scoped daemon capability ceiling**: `cruxible server start
  --capability-ceiling <tier>` (or `CRUXIBLE_MODE`) fixes an immutable
  per-process permission ceiling using the existing tier hierarchy.
  Anonymous auth-off requests receive exactly the ceiling; bearer and
  relayed tiers are clamped to `min(token tier, ceiling)`, so a
  discovered admin token cannot exceed a lower ceiling. Config reload
  and in-place restart cannot alter it; refusals are typed (operation,
  required tier, ceiling) and identical across HTTP, CLI-against-daemon,
  and MCP; `/health` discloses the ceiling. Defaults unchanged when
  unset. Built for single-container agent deployments where the daemon
  boundary, not client curation, must carry enforcement.
- **Generic gate candidates**: gates of kind `generic` accept arbitrary
  caller-supplied candidate values from newline-delimited stdin or repeatable
  public `--candidate` arguments, enabling state-backed pre-action checks
  outside git. Empty input and terminal stdin fail closed; the hidden
  cross-kind `--value` diagnostic override is unchanged.

### Fixed

- **MCP union output schemas are object-rooted**: the four union-returning
  tools (`query`, `query_inline`, `list_queries`, `inspect_entity`) now
  publish `type: object` at the schema root alongside `anyOf`. Non-conformant
  roots made some MCP clients drop every tool on the server.
- **MCP tools/list no longer blocks on daemon reachability**: the advertised
  catalog is frozen from static metadata at server creation and server-mode
  tool calls run on worker threads, so listing answers immediately even when
  the daemon is down; individual calls fail per-tool with a clean error.
- **Dry-run validation parity**: invalid direct writes now raise the same
  `DataValidationError` in dry-run as in apply (previously dry-run buried
  validation errors in a success envelope with exit 0), across entity,
  relationship, and batch surfaces. Dry-run still mutates nothing and mints
  no receipt.

## 0.2.6 — 2026-07-18

### Added

- **Compact query catalog**: `query list` returns bounded summaries
  (name, mode, entry point, required params, result shape) instead of
  full definitions; `detail=full` preserves the previous payload and
  `query describe` stays the canonical detailed read.
- **Read output profiles**: a shared `compact`/`standard`/`full`
  serializer across query rows, inspect, get, sample, and list.
  `standard` is byte-identical to 0.2.5 and remains the HTTP default;
  MCP read tools default to compact identity cards that always preserve
  lifecycle and review markers (`CRUXIBLE_MCP_READ_PROFILE` overrides).
- **Bounded neighborhood inspection**: `entity inspect` gains multi-hop
  expansion with depth, direction, relationship/target-type filters,
  relationship-state visibility, property projection, and node/edge
  budgets with explicit truncation reasons. Expanded reads default to
  `state=all` per the inspection contract, and `edges_hidden_by_state`
  reports edges an explicit state filter suppressed.
- **Read revision and continuation**: a monotonic `read_revision`
  advances with every state-mutating commit (audit writes excluded) and
  rides every read envelope; list, catalog, and neighborhood reads
  accept opaque continuation tokens that fail with a typed 409 when
  state has moved; receipts pagination uses a keyset cursor. Silent
  truncation is gone: `sample` reports true totals, and empty pages
  with matches report `truncated`.
- **Graph layout for query output**: `layout=graph` returns each unique
  entity and relationship once with ordered result references and a
  compact path index; rows layout is unchanged and remains the default.
- **Agent-local working set (opt-in prototype)**: `--ws` or
  `CRUXIBLE_WORKING_SET=1` captures compact records of everything a
  JSON read returned into a grepable, credential-scoped JSONL cache;
  `cruxible ws path|status|verify|refresh|clear` manage it, `verify`
  checks freshness against the live revision and config digest, and the
  cache is never read by any write path. MCP capture is available via
  `CRUXIBLE_WORKING_SET_DIR` for co-located deployments.

### Changed

- Cold-start agent read cost on the in-repo read benchmark drops 86%
  end to end (methodology and raw results in `benchmarks/read_anchor/`).
- README restructured around a show-first fold; the full governed-domain
  walkthrough moved to `docs/deep-dive.md`.

## 0.2.5 — 2026-07-16

### Fixed

- **Tabular bundle loading tolerates optional columns**: JSON/JSONL
  reference bundles with columns that are null for the first hundred
  rows no longer crash canonical workflow ingest; schema inference now
  scans all rows.

### Changed

- MCP server instructions now document relationship truth-state
  semantics (live / accepted / pending / reviewable) so agents receive
  the review model without reading docs.

## 0.2.4 — 2026-07-16

Config composition lands: instances materialize from chains of config
layers (base kit → domain → overlay) instead of a single vendored file,
and every materialized config carries verifiable provenance.

### Added

- **Recursive N-ary config composition (`extends`)**: a config may extend
  multiple bases and bases may themselves extend, materialized with
  deterministic layering; ambiguous or conflicting layer identities in the
  chain are rejected rather than silently merged.
- **First-class default base kits**: a base kit role with an optional
  `requires_base` contract; `agent-operation` is the public init default,
  with an explicit `--bare` opt-out across CLI, MCP, HTTP, hosted runtime,
  and client surfaces. Base/domain/overlay ordering is validated and the
  composed base identity is reported.
- **Config provenance and `cruxible config status`**: every authored layer
  and its digest is recorded alongside the exact materialized bytes;
  generated active configs are stamped, source drift and hand-edits are
  detected (forged source manifests rejected), governed active configs are
  verified at daemon startup with an explicit recovery override, and
  provenance stays stable across kit repoints and checkout moves.
- **`judgment` proposal-policy preset** (agent-operation kit): planning
  judgments — e.g. work-item dependency edges — require maintainer
  rationale; source evidence is advisory rather than demanded.

### Changed

- **Overlay composition boundary preserved**: uploaded overlays keep their
  layer boundary through composition, so overlay edits cannot rewrite
  base-kit-owned config.

## 0.2.3 — 2026-07-12

Kit versions now track the release train: every bundled kit's manifest
version matches the release that ships it.

### Added

- **Frozen-property mutation guards (`type: frozen`)**: the guard grammar
  could only trigger on transitions *to* named values, so no property could
  be protected from *any* change. A frozen-property condition freezes the
  guarded property outright: updates that change it are refused while the
  entity's **stored, pre-write** state matches an optional `while`
  property=value clause — with no clause the property is immutable after
  create. Creates set the property freely and re-asserting the stored value
  is not a change. Because the clause reads before-state only, a single
  write that both leaves the freeze state and changes the frozen property
  (demote + retarget) is refused by design, and an update whose stored
  state cannot be read — or whose `while` clause value fails schema
  normalization — fails closed. Enforced at the shared guard
  chokepoint every entity write path runs through (`add_entity`,
  `batch_direct_write`, canonical workflow apply). Entity types only in
  v1 — config lint refuses freeze declarations on relationship types.
  Compact grammar: `freeze: <Entity>.<prop>` with an optional `while:`
  mapping. The agent-operation kit closes two holes with it:
  `ReviewRequest.change_head` is frozen while `status=approved` (an
  approved review's pin can no longer be retargeted to an unreviewed SHA
  under the merge-review gate) and `StateNote.kind` is immutable after
  create (a reviewer's rationale note can no longer be re-kinded to
  `scratchpad` to hide it from curated reads).
- **`gates` config view**: `cruxible config views --view gates` renders
  declared repo gates as a generated Markdown block (opt-in; not part of
  `--view all`). The agent-operation README now documents its
  `merge-review` gate with an authored Merge Gate section plus the
  generated block.

### Fixed

- **Kit catalog status is current**: `kits/README.md` now lists
  supply-chain-blast-radius and case-law-monitoring as `ready` — both ship
  working deterministic providers, pinned data, and worked demos, so the
  placeholder-provider caveat no longer applies.
- **kev-triage README no longer misstates the pipeline diagram**: the
  generated workflow-pipeline diagram is an inferred dependency ordering,
  not the onboarding order; the README says so and points at
  `docs/kev-guide.md` for the actual sequence.
- **kev-triage ships least-privilege MCP config**: `.mcp.json` now sets
  `CRUXIBLE_MODE=governed_write` instead of `admin`, with a README note
  that `group resolve` and initial canonical applies need a higher tier.

## 0.2.2 — 2026-07-12

### Added

- **`cruxible gate`: declared merge gates enforced from state**: a `gates:`
  config element declares named, kind-based gates — `{kind, entity_type,
  match_property, condition}`, where `kind` selects a source adapter that
  supplies the candidate values to check. `cruxible gate check <name>`
  evaluates a gate; the only v1 kind, `git-pre-push`, reads git's pre-push
  protocol and requires every parent of every pushed merge commit to be
  pinned by a matching entity in state, refusing the push otherwise (fail
  closed on any error). The agent-operation kit ships a `merge-review` gate
  (ReviewRequest / change_head / approved) so a repo can gate merges on
  approved reviews with a one-line pre-push hook. Doctrine: a *guard* blocks
  a write into state; a *gate* lets the world act only if state agrees.
- **Approval actor separation (`distinct_from_creation_actor`)**: mutation
  guards can now require that the acting actor differ from the actor that
  created the target entity — anchored on the creation receipt's
  server-derived actor identity, never on writable properties. Fail-closed:
  entities with no committed creation receipt or no recorded creation actor
  refuse the guarded transition, and create-with-guarded-value is always
  refused. The agent-operation kit's review-approval guard now combines its
  allow-list with separation, so the actor that files a ReviewRequest can
  no longer approve it. Consequence: importing records in an
  already-approved state is refused — land reviews as `requested` and
  approve under a second credential.

### Security

- **Feedback channel now honors write-tier boundaries**
  (wi-feedback-write-tier-bypass): a `governed_write` feedback `correct`
  could apply arbitrary edge property corrections to relationship types
  whose direct-write surface requires `graph_write`, and `reject`/`flag`
  could move an edge out of live review state with no actor identity under
  server auth. Corrections are now gated at the corrected relationship
  type's config-declared `write_tier` (default `graph_write`) across
  `feedback`, `feedback_batch` (strictest corrected type in a mixed batch),
  and `feedback_from_query` (target resolved from the receipt before the
  check), refusing with the same `PermissionDeniedError` as the direct-write
  facades. Under server auth, **every** feedback action (`approve` /
  `correct` / `reject` / `flag`) now requires a resolved actor identity —
  anonymous retraction ends alongside anonymous promotion. Auth-off local
  behavior is unchanged, as are governed corrections on types that declare
  `write_tier: governed_write`.

## 0.2.1 — 2026-07-11

### Added

- **Config-declared write tiers (`write_tier`)**: entity and relationship
  types may declare `write_tier: governed_write` to open their direct-write
  surface (`add_entity` / `add_relationship` / `batch_direct_write`) to
  `governed_write` actors. Undeclared types keep requiring `graph_write`;
  mixed payloads are gated at the strictest touched type; mutation guards
  and `write_policy` run unchanged after the tier check. Config lint
  rejects non-write tiers (`read_only`, `admin`) and tier declarations on
  `proposal_only`/`mint_only` types. See "Config-Declared Write Tiers"
  in the config reference.
- **agent-operation kit: scratchpad notes + Decision acceptance guard**:
  `state_note_kind` gains `scratchpad` — an implementer's mid-flight
  working state. StateNote and its attachment edges declare
  `write_tier: governed_write`, so implementer agents can write notes
  without `graph_write`. Curated note reads (`recent_state_notes`,
  `state_notes_for_work_item`, `state_notes_for_review_request`, and the
  bounded note sets of the context queries) exclude scratchpad notes; the
  new `work_item_scratchpad` query replays a work item's scratchpad notes
  in created order for mid-flight pickup. A new
  `decision_acceptance_requires_authorized_actor` mutation guard requires
  the `authorized-reviewer` actor to move a Decision to `accepted` —
  including create-with-accepted (proposed decisions stay writable at the
  normal tier). Trust boundary, on the record: the note surface (all
  kinds, creates and updates) is now governed_write territory — note
  content is governed_write-trust while verdicts and lifecycle stay
  actor-guarded; see the kit README's Note-Surface Trust Boundary.

### Fixed

- **Config reload refuses to strand stored graph records**: reloading a
  config that no longer declares entity or relationship types present in
  the stored graph used to succeed silently and break every read of
  those records. Reload now refuses before any write, listing the
  stranded types with counts; `--allow-orphans` proceeds explicitly and
  the response carries the stranding report. Every successful reload now
  reports its type delta, and a reload with a corrupted current config
  still works as the repair path (delta reported as unknown).
- **Snapshot clones are reachable on auth-enabled daemons**: cloning used
  to mint a new instance with no credentials at all — instance-scoped
  source credentials couldn't reach it and nothing could be claimed or
  recovered. The clone response now returns a one-time ADMIN credential
  for the new instance (same conventions as `credential claim-bootstrap`);
  auth-disabled daemons are unchanged.
- **Heterogeneous query returns are labeled correctly**: queries returning
  `AnyEntity` now project `entity_type` and `entity_id` for every row
  instead of mislabeling rows under the entry point's key, and composed
  configs that select keys from unresolved return types fail config lint
  instead of silently disabling the check.

## 0.2.0 — 2026-07-07

The first broadly usable release: hard state for AI agents — typed, governed,
receipted — with composable starter kits and a complete evidence loop.

### Added

- **Multi-kit compose at init**: `cruxible init --kit <base> --kit <overlay>`
  composes overlay kits over a base state in one instance under a unified
  `kits/<kit_id>/` layout; overlay resolution comes from kit manifests
  (`target_state`), with fail-closed namespacing and merged locks.
- **Evidence guard** (`require_evidence_on_support`): opt-in per signal
  source — a support signal carrying no evidence escalates to review and can
  never auto-resolve. All bundled kits opt in: every support verdict the
  shipped kits emit is evidence-backed by construction.
- **Source artifact loop, end to end**: caller-supplied deterministic ids on
  registration (`--id`, HTTP, MCP); a `register_source_artifacts` workflow
  step (canonical-only, content-is-data, idempotent re-runs); read routes for
  browsing registered documents and their chunks; CI-grade evidence
  discipline (quoted evidence locators are recomputed against pinned source
  texts on every test run).
- **Local admin recovery**: `cruxible credential recover-admin` ends the
  permanent-lockout failure when an admin token is lost — local-only, rooted
  in filesystem ownership, fully audited.
- **Case-law monitoring kit**: real Chevron-cluster corpus (11 public-domain
  opinion texts, digest-pinned, with verbatim-quote evidence locators),
  synthetic law firm, two-act bad-law demo, governed citator treatment edges.
- **Supply-chain blast-radius kit**: real VORON 2.4 BOM traced to pinned
  upstream artifacts, incident cascade with alternate-sourcing-aware
  verdicts, buffer-coverage arithmetic, differential product exposure.
- **LLM wiki import**: `scripts/import_markdown.py` plus a recipe
  (`docs/recipes/llm-wiki-to-instance.md`) — wiki pages register as pinned
  source artifacts, an agent proposes the typed state, every migrated claim
  keeps a citation into the page it came from.
- **Provider SDK**: blessed evidence-locator constructors, artifact JSON
  access, tri-state verdict vocabulary (`cruxible_core.provider.payloads`).
- Generated kit READMEs: provider contracts, schema catalog, overlay-scoped
  views, signal-policy catalog (including the evidence-guard column).
- State health: unevidenced-support counts scoped to guarded sources.
- `docs/state-resolution-and-maintenance.md`: how conflicts resolve, what
  each permission tier can touch, how state ages and gets repaired.

### Changed

- **The package is now `cruxible`** (was `cruxible-core`): `pip install
  cruxible`. The import remains `cruxible_core` for 0.2. Existing 0.1.x
  installs of `cruxible-core` are unaffected; a compatibility stub will
  follow.

- Utility workflow outputs pipe into strict contracts: core strips its own
  `source_metadata` envelope at workflow-input validation (undeclared extras
  are still refused).
- Providers never fetch: live acquisition moved out of kit providers into
  standalone scripts at the artifact seam; all bundled providers are pure
  functions over workflow data.
- Signal-policy config refuses unknown keys — a typo'd enforcement flag is a
  config error, not a silently disabled guard.
- `READ_ONLY` includes browsing registered source documents (list + full
  read), consistent with the existing dereference tier.

### Fixed

- Server-mode `relationship get` no longer drops trust metadata — approved,
  group-provenanced edges rendered as unreviewed/unattributed over HTTP.
- Seed evidence chunk pins recomputed with the artifact parser; drift is now
  a CI failure.

### Security

- Admin recovery reviewed adversarially (uid-rooted, lock-guarded, audited;
  recovery grants nothing filesystem ownership didn't already grant).
- Evidence guard reviewed adversarially, including fabricated-evidence
  attacks; workflow artifact registration is provably preview-safe (nothing
  persists before apply).
