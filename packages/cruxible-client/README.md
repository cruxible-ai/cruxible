# Cruxible Python SDK reference

**Implemented API at Playbill `7e57b184a` · 2026-09-21.** Install
`cruxible-client` for the Python client; install `cruxible` separately for the
daemon/CLI/MCP runtime. This is an API reference, not an implementation plan.
The [SDK v2 proposal](../../docs/sdk-v2-reference.md) defines proposed additions
separately, including contract-derived record constructors and typed query/run
results. None of its new syntax is implied available here.

Coverage: every public `Playbill` operation, returned authoring/run handles,
World access, typed values, source selectors, Procedure composition,
projection/signing helpers, package-root exports, public authoring-module
utilities, and every public `CruxibleClient` method. Wire request/response
model names link to their defining modules; this reference does not flatten
every historical wire schema into a new contract.

## Contents

- [Conventions and lifecycle](#conventions-and-lifecycle)
- [Connection and state context](#connection-and-state-context)
- [Knowledge authoring](#knowledge-authoring)
- [Reads and discovery](#reads-and-discovery)
- [Evidence, predictions, and operational work](#evidence-predictions-and-operational-work)
- [Procedure entry points](#procedure-entry-points)
- [Changesets](#api-changesetdraft)
- [Drafts, intents, proposals, and approvals](#drafts-intents-proposals-and-approvals)
- [World and typed values](#world-and-typed-values)
- [Source selection](#source-selection)
- [Procedure composition and execution](#procedure-composition-and-execution)
- [Projections and workspace](#projections-and-workspace)
- [Signing capabilities](#signing-capabilities)
- [Exported contract constructors](#exported-contract-constructors)
- [Shared authoring inputs](#shared-authoring-inputs)
- [Lower-level HTTP client](#lower-level-http-client)
- [Errors and unsupported surfaces](#errors-and-unsupported-surfaces)
- [Import and module index](#import-and-module-index)
- [Examples](#examples)

## Conventions and lifecycle

Signatures show actual types and defaults. An argument whose type includes
`None` is still required when no default is displayed. `*` begins keyword-only
arguments. Names beginning `api.` refer to `cruxible_client.contracts`.
`ProcedureSequence` is the imported alias of `authoring.procedures.Sequence`.
`Sequence[T]` in other annotations means the normal Python collection protocol.
Field tables preserve annotations: `ClassVar` members are class metadata, not
constructor arguments. `Field(...)` and dataclass factories express validation
constraints or generated defaults; they are not values callers must pass.

| Stage | Meaning | What it does not imply |
|---|---|---|
| Draft | An authored decision, possibly with read/coordinate assertions | No durable intent or acceptance yet |
| Prepare | Creates/updates a durable intent and preflights its candidate | No approval or acceptance |
| Submit | Submits the prepared candidate through governed proposal machinery | No approval or acceptance |
| Review | Obtains one exact candidate’s review | No signature |
| Approve | Signs/submits that exact reviewed candidate with supplied authority | No acceptance |
| Accept | Requests a signed accepted generation under current admission | No implicit refresh of agent-owned prose |
| Run | Requests admitted execution of accepted machinery | No guarantee of success or automatic acceptance of emitted proposals |

Accepted state and observed evidence are separate. A live context normally
resolves current head per accepted read; `at(...)` and typed refs select an
explicit coordinate. `pb.coordinate` is cached, not a freshness check. Methods
documented as using `pb.coordinate` use that exact last-observed/pinned value.
Operational queues, signing custody, and write authority are not rewound by a
historical reading context. World snapshots remain readable after the live
client moves. Borrowed contexts share their original connection’s lifetime.

Canonical values and typed contracts reject unsupported encodings. An object
with a `.value` does not implicitly serialize arbitrary Python. Use the declared
SubjectRef/LiteralValue/ExactContent forms for knowledge; use the declared
Procedure contracts for execution values.

All network operations may raise authentication, authorization, transport, or
typed daemon errors. Local shape/coordinate checks may raise `ValueError`,
`TypeError`, Pydantic validation errors, or SDK-specific errors. A transport
timeout after a write is an uncertain outcome: inspect the retained intent,
proposal, run, or installation before retrying. A `refused` result is not
necessarily a Python exception; inspect typed result status and diagnostics.

## Connection and state context

Import `Playbill` from `cruxible_client`. Use `Playbill.connect(...)` or
the context-manager form. Direct construction is an advanced injected-client
adapter; it does not perform the compatibility/orientation sequence of connect.

```text
Playbill(
    *,
    client: CruxibleClient,
    instance_id: str,
    workspace: Path,
    access_profile: AccessProfile,
    clock: Any,
) -> Playbill
```

All constructor arguments are required. `client` is the injected transport;
`instance_id` selects the daemon instance; `workspace` is expanded and resolved;
`access_profile` supplies the read/disclosure preference; `clock` is the injected
evaluation-time source. The object initially has no observed coordinate and owns
its transport. Prefer `connect()` for ordinary use so context and compatibility
checks run.

| Setting | Default / behavior |
|---|---|
| `CRUXIBLE_SERVER_BEARER_TOKEN` | Used when `token` is omitted; never creates a principal or grants rights. |
| `CRUXIBLE_CLI_CONTEXT_PATH` | Otherwise `~/.cruxible/client-context.json`. |
| `CRUXIBLE_CLIENT_TIMEOUT_S` | Ordinary HTTP read/write timeout: 180 seconds; connect/pool: 5 seconds. |
| `CRUXIBLE_CLIENT_CONNECT_TIMEOUT_S` | Connect-time orientation read/write timeout: 900 seconds, at least the ordinary budget. |
| Default access profile | `sdk-default`, classes `("instance", "public")`, disclose restricted existence `True`. |

Explicit target/instance/workspace arguments participate in the shared context
resolver. Workspace attachment, environment, and remembered context cannot
silently combine an instance with an incompatible transport. Resolve or repair
that mismatch instead of guessing a local instance.

<a id="api-playbill-connect"></a>

### `Playbill.connect`

[Source](src/cruxible_client/authoring/sdk.py#L1473)

```text
connect(
    *,
    context: str | Path | None = None,
    target: str | None = None,
    instance: str | None = None,
    token: SecretStr | None = None,
    workspace: Path | None = None,
    access_profile: AccessProfile | None = None,
    at: AcceptedCoordinate | api.PlaybillAcceptedCoordinate | None = None,
) -> Playbill
```

Opens a transport, checks client/daemon contract compatibility, resolves context, and ordinarily performs orientation. A supplied at skips live orientation and pins that coordinate.

**Conditions and effects:** ValueError/PlaybillContextResolutionError for missing or inconsistent context; IncompatibleDaemonVersion for an incompatible authoring contract; authentication/transport errors.

| Parameter | Default | Meaning |
|---|---|---|
| `context` | `None` | Remembered client-context JSON path; otherwise CRUXIBLE_CLI_CONTEXT_PATH or ~/.cruxible/client-context.json. |
| `target` | `None` | Explicit HTTP(S) endpoint or unix:/absolute/socket. It does not select an arbitrary local instance directory. |
| `instance` | `None` | Daemon instance ID. Omission uses resolved workspace/client context. |
| `token` | `None` | Bearer credential as SecretStr; otherwise CRUXIBLE_SERVER_BEARER_TOKEN. |
| `workspace` | `None` | Client workspace root for source selection, projections, and configured floors. |
| `access_profile` | `None` | Declared access classes and disclosure preference; server authorization remains authoritative. |
| `at` | `None` | Explicit accepted coordinate. Omission follows the live/pinned object semantics stated above. |

<a id="api-playbill-close"></a>

### `Playbill.close`

[Source](src/cruxible_client/authoring/sdk.py#L1586)

```text
close() -> None
```

Closes an owning connection. Closing a context borrowed through at() leaves its owner’s transport open.

**Conditions and effects:** No automatic submission, acceptance, or workspace refresh.

<a id="api-playbill-coordinate"></a>

### `Playbill.coordinate`

[Source](src/cruxible_client/authoring/sdk.py#L1611)

```text
coordinate: AcceptedCoordinate
```

The pinned coordinate, or the live client's last observed coordinate.

This property performs no I/O. Live reads resolve current head in their
own request, so this value is not a freshness check.

<a id="api-playbill-at"></a>

### `Playbill.at`

[Source](src/cruxible_client/authoring/sdk.py#L1621)

```text
at(coordinate: AcceptedCoordinate | api.PlaybillAcceptedCoordinate) -> Playbill
```

Returns a borrowed pinned context without I/O. Accepted reads stay fixed; writes still undergo current admission.

**Conditions and effects:** The original connection must remain open; pinned context does not rewind operational queues or authority.

| Parameter | Default | Meaning |
|---|---|---|
| `coordinate` | Required | Exact accepted coordinate, not a timestamp or an instruction to refresh current head. |

<a id="api-playbill-refresh"></a>

### `Playbill.refresh`

[Source](src/cruxible_client/authoring/sdk.py#L1931)

```text
refresh() -> SearchPage
```

Performs orientation in the current live or pinned context and records the observed coordinate.

**Conditions and effects:** A pinned context stays pinned; refresh does not accept proposals or update a local floor.

<a id="api-playbill-world"></a>

### `Playbill.world`

[Source](src/cruxible_client/authoring/sdk.py#L2067)

```text
world() -> World
```

Reads accepted vocabulary and creates an independent pinned World. Lazy field reads remain at that coordinate as the live client advances.

**Conditions and effects:** Current first Subject access loads the Subject listing; field access is not an automatic scalar resolution.

<a id="api-playbill-block"></a>

### `Playbill.block`

[Source](src/cruxible_client/authoring/sdk.py#L1662)

```text
block: ProjectionBlocks
```

Client-only declaration stamps; prose remains wholly agent-owned.

<a id="api-playbill-enter"></a>

### `Playbill.__enter__`

[Source](src/cruxible_client/authoring/sdk.py#L1590)

```text
__enter__() -> Playbill
```

<a id="api-playbill-exit"></a>

### `Playbill.__exit__`

[Source](src/cruxible_client/authoring/sdk.py#L1593)

```text
__exit__(*_args: object) -> None
```

| Parameter | Default | Meaning |
|---|---|---|
| `_args` | `'<variadic>'` | Value required by the declared type; see operation semantics below. |

## Knowledge authoring

<a id="api-playbill-changes"></a>

### `Playbill.changes`

[Source](src/cruxible_client/authoring/sdk.py#L2097)

```text
changes(*, rationale: str | None=None) -> ChangeSetDraft
```

Returns a changeset builder retaining the last observed coordinate for references and vocabulary.

**Conditions and effects:** No prepare/submit/accept at construction. The set admits or refuses as one governed decision.

| Parameter | Default | Meaning |
|---|---|---|
| `rationale` | `None` | Author-supplied explanation retained with this Claim/change decision. |

<a id="api-playbill-subject"></a>

### `Playbill.subject`

[Source](src/cruxible_client/authoring/sdk.py#L1945)

```text
subject(
    *,
    subject: str | SubjectRef,
    pins: Sequence[ArtifactPin],
    lifecycle: ArtifactLifecycle,
) -> SubjectDraft
```

Builds a SubjectDraft from explicit identity, pins, and lifecycle.

**Conditions and effects:** No proposal until prepare/propose. Wrong ref kind or mismatched coordinate refuses.

| Parameter | Default | Meaning |
|---|---|---|
| `subject` | Required | SubjectRef or accepted canonical Subject address; a Claim is about this Subject. |
| `pins` | Required | Declared ArtifactPin dependencies. Exact pins belong to their artifact contract. |
| `lifecycle` | Required | Explicit artifact lifecycle record, including identity/version continuity. |

<a id="api-playbill-claim-type"></a>

### `Playbill.claim_type`

[Source](src/cruxible_client/authoring/sdk.py#L1975)

```text
claim_type(
    *,
    predicate: str | ClaimTypeRef,
    subject_kinds: Sequence[str],
    object_kind: ClaimObjectKind | str,
    value_schema: dict[str, object] | None,
    object_subject_kinds: Sequence[str],
    cardinality: Cardinality | str,
    permitted_roles: Sequence[ClaimRole | str],
    referent_sensitivity: ReferentSensitivity | str,
    sources: Sequence[str | SourceRef],
    admission_policy: ClaimAdmissionPolicyV1,
    resolution_policy: ClaimResolutionPolicyV1,
    pins: Sequence[ArtifactPin],
    evidence_freshness: Duration | None,
    attestation_consequence_policy: ClaimAttestationConsequencePolicyV1 | None = None,
) -> ClaimTypeDraft
```

Builds a ClaimTypeDraft with explicit object, cardinality, role, evidence, admission, and resolution constraints.

**Conditions and effects:** Validates enum/contract values. Required parameters have no inferred default even when their type permits None.

| Parameter | Default | Meaning |
|---|---|---|
| `predicate` | Required | ClaimType reference or canonical predicate address. |
| `subject_kinds` | Required | Allowed or selected Subject kinds, depending on authoring versus audit. |
| `object_kind` | Required | literal, subject, or exact_content, preferably the ClaimObjectKind enum. |
| `value_schema` | Required | Literal-value schema; relevant to literal objects, not a free-form second ontology. |
| `object_subject_kinds` | Required | Allowed endpoint Subject kinds for a subject-valued ClaimType. |
| `cardinality` | Required | one or many. It constrains the field; it does not hide live competing Claims on reads. |
| `permitted_roles` | Required | Claim roles admitted by this ClaimType. |
| `referent_sensitivity` | Required | Whether dependency sensitivity follows endpoint identity or its shell. |
| `sources` | Required | Logical sources admitted by the ClaimType evidence policy. |
| `admission_policy` | Required | Typed policy governing Claim admission. |
| `resolution_policy` | Required | Typed policy governing Claim resolution; separate from acceptance. |
| `pins` | Required | Declared ArtifactPin dependencies. Exact pins belong to their artifact contract. |
| `evidence_freshness` | Required | Optional freshness duration. None leaves it unspecified. |
| `attestation_consequence_policy` | `None` | Optional policy for consequences of accepted signed attestations. |

<a id="api-playbill-claim"></a>

### `Playbill.claim`

[Source](src/cruxible_client/authoring/sdk.py#L2109)

```text
claim(
    *,
    subject: str | SubjectRef,
    predicate: str | ClaimTypeRef,
    value: CanonicalValue | SubjectRef | LiteralValue | ExactContent,
    role: ClaimRole | str,
    rationale: str,
    supported_by: EvidenceSelection | CaptureRef | None = None,
    copied_from: EvidenceSelection | CaptureRef | None = None,
    self_source: str | None = None,
    qualifier: str | None = None,
    effective_period: EffectivePeriod | None = None,
    revises: str | ClaimRef | None = None,
    dispositions: Mapping[str | ClaimRef, Disposition | str] | None = None,
    subject_definition: SubjectDraft | None = None,
    claim_type_definition: ClaimTypeDraft | None = None,
) -> ClaimDraft
```

Builds a ClaimDraft; may read accepted metadata to interpret objects and evidence, but does not submit.

**Conditions and effects:** Exactly one of supported_by, copied_from, self_source is required. Enforces typed reference/value compatibility, citation roles, independent-source restrictions, and unique normalized dispositions.

| Parameter | Default | Meaning |
|---|---|---|
| `subject` | Required | SubjectRef or accepted canonical Subject address; a Claim is about this Subject. |
| `predicate` | Required | ClaimType reference or canonical predicate address. |
| `value` | Required | Object value under the selected ClaimType; use SubjectRef, LiteralValue, or ExactContent where applicable. |
| `role` | Required | Normative, observation, environment_binding, or derivation role admitted by the ClaimType. |
| `rationale` | Required | Author-supplied explanation retained with this Claim/change decision. |
| `supported_by` | `None` | Independent selected evidence or an evidence-role CaptureRef. Exactly one evidence-source argument is required. |
| `copied_from` | `None` | Selected copied content or a reusable CaptureRef; does not upgrade it to independent evidence. |
| `self_source` | `None` | Explicit self-asserted source text. Exactly one of supported_by, copied_from, self_source. |
| `qualifier` | `None` | Optional qualifier of this Claim statement. |
| `effective_period` | `None` | Optional effective start/end instants, distinct from capture and acceptance times. |
| `revises` | `None` | Existing Claim identity to revise rather than minting a parallel lineage. |
| `dispositions` | `None` | Existing contender Claim IDs mapped to explicit dispositions. Duplicate normalized IDs refuse. |
| `subject_definition` | `None` | Subject draft carried with the Claim, if defining its Subject in the same operation. |
| `claim_type_definition` | `None` | ClaimType draft carried with the Claim, if defining its predicate in the same operation. |

<a id="api-playbill-retire-claim"></a>

### `Playbill.retire_claim`

[Source](src/cruxible_client/authoring/sdk.py#L2500)

```text
retire_claim(
    claim: str | ClaimRef,
    *,
    reason: ClaimRetirementReason,
    mode: Literal['preflight', 'submit'] = 'preflight',
    effective_until: datetime | None = None,
    dependents: Sequence[ClaimRetireDependentV1] = (),
) -> api.PlaybillClaimRetireResponse
```

Preflights or submits an attributed retirement with explicit dependency closure.

**Conditions and effects:** Default mode is preflight. Submission is a governed proposal, not automatic acceptance.

| Parameter | Default | Meaning |
|---|---|---|
| `claim` | Required | Claim ID or typed ClaimRef; typed refs also assert their observed coordinate. |
| `reason` | Required | Attributed reason for refusal, retirement, or operational action as specified by the API. |
| `mode` | `'preflight'` | preflight validates; submit requests the corresponding governed retirement proposal. |
| `effective_until` | `None` | Optional effective-end instant for retirement. |
| `dependents` | `()` | Explicit dependency-closure dispositions required by retirement or ClaimType succession. |

<a id="api-playbill-query-definition"></a>

### `Playbill.query_definition`

[Source](src/cruxible_client/authoring/sdk.py#L2659)

```text
query_definition(
    *,
    definition: QueryDefinitionInput,
    vocabulary: Sequence[ClaimTypeRef] = (),
) -> QueryDraft
```

Builds a QueryDraft with optional coordinate assertions from referenced vocabulary.

**Conditions and effects:** A vocabulary ref not used by the query refuses. Ordinary Claim and artifact-definition queries share this API.

| Parameter | Default | Meaning |
|---|---|---|
| `definition` | Required | Typed definition being authored; use the exact input type shown in this operation’s signature. |
| `vocabulary` | `()` | World ClaimType references used by the query, retaining stale-reference assertions. |

<a id="api-playbill-resume-intent"></a>

### `Playbill.resume_intent`

[Source](src/cruxible_client/authoring/sdk.py#L1848)

```text
resume_intent(intent_id: str) -> Intent
```

Reads the latest durable intent, preflight, and observed proposal into a handle.

**Conditions and effects:** Does not prepare, submit, sign, accept, or restore process-local source locations/review tokens.

| Parameter | Default | Meaning |
|---|---|---|
| `intent_id` | Required | Durable authoring intent identity from this instance. |

<a id="api-playbill-proposal"></a>

### `Playbill.proposal`

[Source](src/cruxible_client/authoring/sdk.py#L1869)

```text
proposal(proposal_id: str) -> Proposal
```

Creates a local handle for an existing proposal ID.

**Conditions and effects:** Does not read, review, create, approve, or accept the proposal.

| Parameter | Default | Meaning |
|---|---|---|
| `proposal_id` | Required | Exact proposal identity, not a branch name or candidate revision inferred from head. |

<a id="api-playbill-accept"></a>

### `Playbill.accept`

[Source](src/cruxible_client/authoring/sdk.py#L1873)

```text
accept(proposal_id: str) -> api.PlaybillActivationReceipt
```

Requests governed acceptance and returns PlaybillActivationReceipt. Updates a live connection’s last observed coordinate.

**Conditions and effects:** Does not sign approval or refresh the client workspace. Use pb.at(receipt.accepted_coordinate) for exact readback.

| Parameter | Default | Meaning |
|---|---|---|
| `proposal_id` | Required | Exact proposal identity, not a branch name or candidate revision inferred from head. |

<a id="api-playbill-activate"></a>

### `Playbill.activate`

[Source](src/cruxible_client/authoring/sdk.py#L1905)

```text
activate(proposal_id: str, *, no_sync: bool=False) -> api.PlaybillWorkspaceActivationResult
```

Requests acceptance, refreshes the configured floor at the accepted coordinate, and normally checks workspace blocks.

**Conditions and effects:** no_sync skips the block check, not floor refresh. Acceptance can succeed even when later workspace maintenance reports a problem.

| Parameter | Default | Meaning |
|---|---|---|
| `proposal_id` | Required | Exact proposal identity, not a branch name or candidate revision inferred from head. |
| `no_sync` | `False` | Skip block checking after workspace activation; does not turn acceptance into a dry run. |

<a id="api-playbill-refresh-workspace"></a>

### `Playbill.refresh_workspace`

[Source](src/cruxible_client/authoring/sdk.py#L1888)

```text
refresh_workspace(
    *,
    at: AcceptedCoordinate | api.PlaybillAcceptedCoordinate,
) -> api.PlaybillFloorRefreshResult
```

Exports/materializes the configured floor at the explicit accepted coordinate and reports written/failed/not_configured.

**Conditions and effects:** Writes client workspace files; does not advance the reading context or check/repin projection blocks.

| Parameter | Default | Meaning |
|---|---|---|
| `at` | Required | Explicit accepted coordinate. Omission follows the live/pinned object semantics stated above. |

<a id="api-playbill-file"></a>

### `Playbill.file`

[Source](src/cruxible_client/authoring/sdk.py#L1942)

```text
file(path: str | Path) -> FileSelector
```

Reads a cataloged workspace file into a FileSelector using .playbill/sources.yaml and applicable local catalog settings.

**Conditions and effects:** Uncataloged/out-of-root selections and malformed catalogs refuse. File bytes are observed now, before prepare.

| Parameter | Default | Meaning |
|---|---|---|
| `path` | Required | Workspace/source path or destination; interpretation is stated by its owning operation. |

## Reads and discovery

<a id="api-playbill-claim-view"></a>

### `Playbill.claim_view`

[Source](src/cruxible_client/authoring/sdk.py#L1667)

```text
claim_view(claim: str | ClaimRef) -> ClaimView
```

Reads and adapts one accepted Claim, including value, revision, verdict, and capture references.

**Conditions and effects:** Missing/redacted/version-incompatible artifacts are daemon refusals; a wrong reference kind refuses locally.

| Parameter | Default | Meaning |
|---|---|---|
| `claim` | Required | Claim ID or typed ClaimRef; typed refs also assert their observed coordinate. |

<a id="api-playbill-claim-views"></a>

### `Playbill.claim_views`

[Source](src/cruxible_client/authoring/sdk.py#L1756)

```text
claim_views(claims: Sequence[str | ClaimRef]) -> tuple[ClaimView, ...]
```

Reads a complete identity batch, at most 256 Claims, preserving input order.

**Conditions and effects:** Mixed coordinates, incomplete/truncated response, or mismatched returned identities refuse.

| Parameter | Default | Meaning |
|---|---|---|
| `claims` | Required | Claim selection; identity batches preserve caller order. For repin, None preserves and an empty sequence removes this backing class. |

<a id="api-playbill-capture"></a>

### `Playbill.capture`

[Source](src/cruxible_client/authoring/sdk.py#L1738)

```text
capture(capture: str | CaptureRef, *, max_bytes: int=4 * 1024 * 1024) -> CaptureView
```

Reads retained capture metadata and available bytes at pb.coordinate. Does not refetch the external source.

**Conditions and effects:** Unavailable evidence is represented in CaptureView.result; .ref/.content refuse if the required material is unavailable.

| Parameter | Default | Meaning |
|---|---|---|
| `capture` | Required | Capture digest or typed CaptureRef to retained evidence; does not initiate acquisition. |
| `max_bytes` | `4 * 1024 * 1024` | Byte bound for this operation, in bytes. It is not permission to exceed effective daemon policy. |

<a id="api-playbill-get"></a>

### `Playbill.get`

[Source](src/cruxible_client/authoring/sdk.py#L2854)

```text
get(ref: str | TypedRef) -> KnowledgeCard
```

Reads an exact supported artifact and wraps it in a KnowledgeCard. Typed Subject/ClaimType/Claim/Procedure/Query/Source refs select their read path; string lookup must resolve exactly.

**Conditions and effects:** Ambiguous/absent matches and unsupported reference kinds refuse; inspect the returned kind before using value.

| Parameter | Default | Meaning |
|---|---|---|
| `ref` | Required | Typed artifact reference or supported identity string; see get/explain for supported kinds. |

<a id="api-playbill-search"></a>

### `Playbill.search`

[Source](src/cruxible_client/authoring/sdk.py#L2956)

```text
search(*, query: str, kinds: Collection[str], statuses: Collection[str]) -> SearchPage
```

Reads one compact search page with explicit kind/status filters.

**Conditions and effects:** Check truncated/cursor. This high-level method has no cursor parameter; use the lower-level search_playbill for continuation.

| Parameter | Default | Meaning |
|---|---|---|
| `query` | Required | Named query/QueryRef for run_query; search text for search. |
| `kinds` | Required | Discovery kind filters; pass an explicit empty collection when no filter is intended. |
| `statuses` | Required | Discovery status filters; pass an explicit empty collection when no filter is intended. |

<a id="api-playbill-list"></a>

### `Playbill.list`

[Source](src/cruxible_client/authoring/sdk.py#L2965)

```text
list(*, kinds: Collection[str], statuses: Collection[str]) -> SearchPage
```

Reads one compact listing page with explicit kind/status filters.

**Conditions and effects:** Check truncated/cursor; high-level list has no continuation argument.

| Parameter | Default | Meaning |
|---|---|---|
| `kinds` | Required | Discovery kind filters; pass an explicit empty collection when no filter is intended. |
| `statuses` | Required | Discovery status filters; pass an explicit empty collection when no filter is intended. |

<a id="api-playbill-orient"></a>

### `Playbill.orient`

[Source](src/cruxible_client/authoring/sdk.py#L2973)

```text
orient() -> SearchPage
```

Reads a compact orientation page for Claims, demands, and Procedures in the context’s state.

**Conditions and effects:** A discovery result is not the entire evidence/history body.

<a id="api-playbill-explain"></a>

### `Playbill.explain`

[Source](src/cruxible_client/authoring/sdk.py#L3017)

```text
explain(ref: str | TypedRef) -> object
```

Expands Claim or Subject governance/provenance context.

**Conditions and effects:** Only ClaimRef/SubjectRef or a recognized Claim ID string is accepted here; other kinds raise ReferenceKindError.

| Parameter | Default | Meaning |
|---|---|---|
| `ref` | Required | Typed artifact reference or supported identity string; see get/explain for supported kinds. |

<a id="api-playbill-run-query"></a>

### `Playbill.run_query`

[Source](src/cruxible_client/authoring/sdk.py#L2808)

```text
run_query(
    query: str | QueryRef,
    *,
    parameters: Mapping[str, object] | None = None,
    budgets: QueryBudgetsV1 | None = None,
) -> api.PlaybillQueryRun
```

Runs a named accepted query in the live/pinned/reference context with explicit evaluation time and returns result plus receipt.

**Conditions and effects:** The current wrapper contains result/receipt dictionaries. Check verdict and truncation; artifact_definitions is a checked typed property for artifact queries only.

| Parameter | Default | Meaning |
|---|---|---|
| `query` | Required | Named query/QueryRef for run_query; search text for search. |
| `parameters` | `None` | Invocation/query parameters in the declared canonical contract. |
| `budgets` | `None` | Operation-specific bounds; the signature distinguishes QueryBudgetsV1 from Line budget mappings. |

<a id="api-playbill-since"></a>

### `Playbill.since`

[Source](src/cruxible_client/authoring/sdk.py#L3131)

```text
since(
    generation: int,
    *,
    max_rows: int = 100,
    max_bytes: int = 65536,
    cursor: api.PlaybillSinceCursor | Mapping[str, object] | None = None,
) -> api.PlaybillSinceResult
```

Reads accepted history changes with row/byte bounds and snapshot-bearing continuation.

**Conditions and effects:** Reuse the returned cursor unchanged for the same selection. Does not synthesize changes from current files.

| Parameter | Default | Meaning |
|---|---|---|
| `generation` | Required | Starting generation sequence for accepted-history changes. |
| `max_rows` | `100` | Maximum returned rows for a bounded read. |
| `max_bytes` | `65536` | Byte bound for this operation, in bytes. It is not permission to exceed effective daemon policy. |
| `cursor` | `None` | Opaque continuation returned by the same operation; retains its selection/snapshot. |

## Evidence, predictions, and operational work

<a id="api-playbill-resolution-contracts"></a>

### `Playbill.resolution_contracts`

[Source](src/cruxible_client/authoring/sdk.py#L1790)

```text
resolution_contracts(hypothesis: ClaimVersionReferenceV1) -> api.ResolutionContractsResultV1
```

Reads accepted tests of this exact hypothesis version, including retired contracts, at pb.coordinate.

**Conditions and effects:** A history/snapshot read does not establish current execution authority.

| Parameter | Default | Meaning |
|---|---|---|
| `hypothesis` | Required | Exact accepted Claim version whose independent resolution contracts are requested. |

<a id="api-playbill-predict"></a>

### `Playbill.predict`

[Source](src/cruxible_client/authoring/sdk.py#L1799)

```text
predict(contract: ResolutionContractV1) -> Prediction
```

Creates a governed proposal for an independent resolution contract over an already accepted hypothesis. Returns proposal/intent identities.

**Conditions and effects:** Does not approve or accept the proposal.

| Parameter | Default | Meaning |
|---|---|---|
| `contract` | Required | Typed contract or exact contract reference named by the signature. |

<a id="api-playbill-settle"></a>

### `Playbill.settle`

[Source](src/cruxible_client/authoring/sdk.py#L1813)

```text
settle(
    contract: ResolutionContractReferenceV1,
    *,
    observation: ClaimVersionReferenceV1,
    trigger_event: TriggerEventReferenceV1 | None = None,
    terminal_run_id: str | None = None,
    terminal_record_digest: str | None = None,
) -> PredictionSettlement
```

Evaluates and records settlement using the exact contract and accepted observation version. Returns the mechanical Boolean outcome and relation.

**Conditions and effects:** terminal_run_id and terminal_record_digest must be supplied together. Missing/non-Boolean outcome refuses; procedure success alone is not settlement.

| Parameter | Default | Meaning |
|---|---|---|
| `contract` | Required | Typed contract or exact contract reference named by the signature. |
| `observation` | Required | Exact accepted observation Claim version used as settlement evidence. |
| `trigger_event` | `None` | Retained trigger-event reference, when the contract/run requires event binding. |
| `terminal_run_id` | `None` | Run whose terminal evidence supports settlement; supplied together with terminal_record_digest. |
| `terminal_record_digest` | `None` | Exact retained terminal record; supplied together with terminal_run_id. |

<a id="api-playbill-attest"></a>

### `Playbill.attest`

[Source](src/cruxible_client/authoring/sdk.py#L3053)

```text
attest(
    claim: ClaimRef | str,
    *,
    stance: ClaimStance,
    signer: ClaimAttestationV2Signer,
    note: str | None = None,
    valid_until: datetime | None = None,
) -> ClaimAttestationAppendResultV1
```

Prepares the exact examined-existing Claim statement, signs locally, and appends it through the attestation service.

**Conditions and effects:** Does not change the Claim’s statement or sign a proposal approval. Checks signer/key/ref bindings and current service authority.

| Parameter | Default | Meaning |
|---|---|---|
| `claim` | Required | Claim ID or typed ClaimRef; typed refs also assert their observed coordinate. |
| `stance` | Required | Signed attestation stance admitted by ClaimStance. |
| `signer` | Required | Caller-provisioned signing capability of the protocol required by this method. |
| `note` | `None` | Optional note carried by the prepared attestation request. |
| `valid_until` | `None` | Optional attestation expiry instant. |

<a id="api-playbill-attest-new-capture"></a>

### `Playbill.attest_new_capture`

[Source](src/cruxible_client/authoring/sdk.py#L3084)

```text
attest_new_capture(
    request: PreparedClaimAttestationRequestV1,
    *,
    signer: ClaimAttestationV2Signer,
) -> ClaimAttestationAppendResultV1
```

Signs and appends an already-staged new-capture attestation request.

**Conditions and effects:** attestation_basis must be new_capture; capture registration/acquisition is not invented by this method.

| Parameter | Default | Meaning |
|---|---|---|
| `request` | Required | Typed request specified by the signature; new-capture attestation requires that basis. |
| `signer` | Required | Caller-provisioned signing capability of the protocol required by this method. |

<a id="api-playbill-next"></a>

### `Playbill.next`

[Source](src/cruxible_client/authoring/sdk.py#L3096)

```text
next(*, expiring_within: Duration) -> NextPage
```

Scans the attached workspace and reads actionable work with explicit access profile, evaluation time, and expiry horizon.

**Conditions and effects:** Inspect observed_domains/unobserved_domains; an empty page does not imply every possible domain was observed.

| Parameter | Default | Meaning |
|---|---|---|
| `expiring_within` | Required | Duration defining the next-work expiry horizon. |

<a id="api-playbill-audit"></a>

### `Playbill.audit`

[Source](src/cruxible_client/authoring/sdk.py#L3171)

```text
audit(
    *,
    claim_type_identities: tuple[str, ...] = (),
    subject_kinds: tuple[str, ...] = (),
    max_rows: int = 100,
    max_bytes: int = 65536,
    cursor: api.PlaybillAuditCursor | Mapping[str, object] | None = None,
) -> api.PlaybillAuditResult
```

Reads a bounded ranking of visible Claim-verification work.

**Conditions and effects:** Does not alter accepted Claims or attest to correctness.

| Parameter | Default | Meaning |
|---|---|---|
| `claim_type_identities` | `()` | Optional audit filter by accepted ClaimType identities. |
| `subject_kinds` | `()` | Allowed or selected Subject kinds, depending on authoring versus audit. |
| `max_rows` | `100` | Maximum returned rows for a bounded read. |
| `max_bytes` | `65536` | Byte bound for this operation, in bytes. It is not permission to exceed effective daemon policy. |
| `cursor` | `None` | Opaque continuation returned by the same operation; retains its selection/snapshot. |

<a id="api-playbill-curation-list"></a>

### `Playbill.curation_list`

[Source](src/cruxible_client/authoring/sdk.py#L3153)

```text
curation_list() -> api.PlaybillCurationListResult
```

Performs an attributed workspace scan and reads current operational curation work.

**Conditions and effects:** Operational state stays live even through a pinned accepted-reading context.

<a id="api-playbill-curation-overrule"></a>

### `Playbill.curation_overrule`

[Source](src/cruxible_client/authoring/sdk.py#L3196)

```text
curation_overrule(
    *,
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    attribution_refs: tuple[str, ...] = (),
) -> api.PlaybillCurationActionResult
```

Records that the detector pattern is mechanically inapplicable.

**Conditions and effects:** Requires matching latest-event digest and attribution; does not revise accepted knowledge.

| Parameter | Default | Meaning |
|---|---|---|
| `item_id` | Required | Operational curation item identity. |
| `expected_latest_event_digest` | Required | Optimistic-concurrency assertion on the item’s latest event. |
| `reason` | Required | Attributed reason for refusal, retirement, or operational action as specified by the API. |
| `attribution_refs` | `()` | Retained references supporting attribution/reason for this operational action. |

<a id="api-playbill-curation-accept-fixed"></a>

### `Playbill.curation_accept_fixed`

[Source](src/cruxible_client/authoring/sdk.py#L3214)

```text
curation_accept_fixed(
    *,
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    accepted_proposal_id: str,
    accepted_changeset_digest: str,
    attribution_refs: tuple[str, ...] = (),
) -> api.PlaybillCurationActionResult
```

Records linkage to an exact already-accepted resolving ChangeSet.

**Conditions and effects:** Requires proposal/digest identity and matching latest-event digest; does not accept that proposal itself.

| Parameter | Default | Meaning |
|---|---|---|
| `item_id` | Required | Operational curation item identity. |
| `expected_latest_event_digest` | Required | Optimistic-concurrency assertion on the item’s latest event. |
| `reason` | Required | Attributed reason for refusal, retirement, or operational action as specified by the API. |
| `accepted_proposal_id` | Required | Already-accepted resolving proposal identity. |
| `accepted_changeset_digest` | Required | Exact already-accepted resolving ChangeSet digest. |
| `attribution_refs` | `()` | Retained references supporting attribution/reason for this operational action. |

<a id="api-playbill-curation-suppress"></a>

### `Playbill.curation_suppress`

[Source](src/cruxible_client/authoring/sdk.py#L3236)

```text
curation_suppress(
    *,
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    scope: Literal['item', 'pattern', 'instance'],
    until_generation: int | None = None,
    attribution_refs: tuple[str, ...] = (),
) -> api.PlaybillCurationActionResult
```

Records operational suppression of matching open work for the requested scope and optional generation boundary.

**Conditions and effects:** Does not resolve the item, stop detection, or make its underlying Claims correct.

| Parameter | Default | Meaning |
|---|---|---|
| `item_id` | Required | Operational curation item identity. |
| `expected_latest_event_digest` | Required | Optimistic-concurrency assertion on the item’s latest event. |
| `reason` | Required | Attributed reason for refusal, retirement, or operational action as specified by the API. |
| `scope` | Required | Suppression scope: item, pattern, or instance. |
| `until_generation` | `None` | Optional suppression generation boundary. |
| `attribution_refs` | `()` | Retained references supporting attribution/reason for this operational action. |

## Procedure entry points

<a id="api-playbill-provider-binding"></a>

### `Playbill.provider_binding`

[Source](src/cruxible_client/authoring/sdk.py#L2702)

```text
provider_binding(interface: str, *, provider: str | None=None) -> ProviderBinding
```

Discovers one accepted interface and selects one registered implementation.

**Conditions and effects:** No matching/unique interface, absent provider, or ambiguous implementation refuses. Does not install or authorize execution.

| Parameter | Default | Meaning |
|---|---|---|
| `interface` | Required | Accepted ProviderInterface name or qualified identity. |
| `provider` | `None` | Explicit accepted Provider selection. Omit only when discovery is unambiguous. |

<a id="api-playbill-procedure"></a>

### `Playbill.procedure`

[Source](src/cruxible_client/authoring/sdk.py#L2725)

```text
procedure(*, definition: ProcedureInput | ProcedureSequence) -> ProcedureDraft
```

Builds a ProcedureDraft from ProcedureInput or Sequence. Sequence is structurally built first.

**Conditions and effects:** Rejects unsupported SDK node kinds; accepts source/capture/proposal in graph v4/v5 and call in v5. Direct run-lane readiness is a separate check.

| Parameter | Default | Meaning |
|---|---|---|
| `definition` | Required | Typed definition being authored; use the exact input type shown in this operation’s signature. |

<a id="api-playbill-accepted-procedure"></a>

### `Playbill.accepted_procedure`

[Source](src/cruxible_client/authoring/sdk.py#L2827)

```text
accepted_procedure(procedure: str | ProcedureRef) -> Procedure
```

Returns an accepted Procedure handle; a typed ProcedureRef retains its coordinate.

**Conditions and effects:** Does not execute; a name on a live context follows live reads. Use a pinned context or ref for a fixed selection.

| Parameter | Default | Meaning |
|---|---|---|
| `procedure` | Required | Procedure name or typed reference; Line authoring also accepts a name defined earlier in the same changeset. |

<a id="api-playbill-run-line"></a>

### `Playbill.run_line`

[Source](src/cruxible_client/authoring/sdk.py#L2834)

```text
run_line(
    line_identity_digest: str,
    *,
    occurrence_id: str | None = None,
    resolution_contract: ResolutionContractReferenceV1 | None = None,
    trigger_event: TriggerEventReferenceV1 | None = None,
) -> ProcedureRun
```

Requests one daemon-derived occurrence of an accepted Line and returns a ProcedureRun.

**Conditions and effects:** May invoke providers, register captures, or submit proposals under admitted authority; it is not a preview or permission grant.

| Parameter | Default | Meaning |
|---|---|---|
| `line_identity_digest` | Required | Identity digest of the accepted Line to invoke, not its display name. |
| `occurrence_id` | `None` | Optional retained occurrence identity; the daemon validates/derives its binding. |
| `resolution_contract` | `None` | Exact independent resolution-contract reference bound to this run. |
| `trigger_event` | `None` | Retained trigger-event reference, when the contract/run requires event binding. |

<a id="api-changesetdraft"></a>

## `ChangeSetDraft`

Import: `cruxible_client.authoring.sdk.ChangeSetDraft`. [Source](src/cruxible_client/authoring/sdk.py#L663)

Obtained from `pb.changes(rationale=...)`. Methods stage members; `prepare()`
preflights the whole set. `claim`, `procedure`, `query_definition`, `line`, policy,
contract, and attestation methods return this builder for chaining unless their
signature returns a pending ref. The pending Subject/ClaimType references may
be used by sibling Claims without pretending they already exist in accepted state.

| Field | Type | Default / construction |
|---|---|---|
| `rationale` | `str \| None` | `None` |

### Member semantics

| Member | Return and effect | Additional requirements |
|---|---|---|
| `claim` | Stages one Claim; returns builder | Same value/evidence rules as `Playbill.claim`. |
| `subject` | Stages a definition; returns PendingSubjectRef | Sibling Claims may use it. |
| `claim_type` | Stages a definition; returns PendingClaimTypeRef | Use succeed_claim_type for an accepted predecessor. |
| `succeed_claim_type` | Stages a full successor and closure dispositions | Exact reverse-pin closure; sibling reauthors retain Claim identity/subject/predicate. |
| `retire` | Stages an attributed Claim retirement | Explicit required dependents. |
| `signed_attestation` | Stages supplied signed bytes | Does not re-sign or accept them. |
| `attestation` | Reads/prepares the exact Claim, signs locally, then stages | Explicit signer; no implicit approval of the containing changeset. |
| `capture_contract`, `resolution_contract`, `acquisition_policy` | Stage typed definitions | Existing artifact contracts and normal admission apply. |
| `procedure`, `query_definition`, `procedure_mandate` | Stage the shared authoring inputs | Same validation as the corresponding individual draft. |
| `line` | Stages a Line naming its Procedure and acquisition policy | Manual trigger by default; omitted budgets inherit Procedure hard caps; admission still checks live authority. |
| `prepare` | Creates/preflights one durable intent | Atomic decision; malformed member refuses the set. |

Definitions and successions precede dependent Claims during lowering. Two
members cannot write the same artifact path. A live Claim cannot be carried
unchanged through an object-kind change. The source of truth remains accepted
artifacts; staging a definition does not mutate accepted state.

<a id="api-changesetdraft-claim"></a>

### `ChangeSetDraft.claim`

[Source](src/cruxible_client/authoring/sdk.py#L676)

```text
claim(
    *,
    subject: str | SubjectRef,
    predicate: str | ClaimTypeRef,
    value: CanonicalValue | SubjectRef | LiteralValue | ExactContent,
    role: ClaimRole | str,
    rationale: str,
    supported_by: EvidenceSelection | CaptureRef | None = None,
    copied_from: EvidenceSelection | CaptureRef | None = None,
    self_source: str | None = None,
    qualifier: str | None = None,
    effective_period: EffectivePeriod | None = None,
    revises: str | ClaimRef | None = None,
    dispositions: Mapping[str | ClaimRef, Disposition | str] | None = None,
    subject_definition: SubjectDraft | None = None,
    claim_type_definition: ClaimTypeDraft | None = None,
) -> ChangeSetDraft
```

Add one Claim to this changeset; the signature is `Playbill.claim`'s.

| Parameter | Default | Meaning |
|---|---|---|
| `subject` | Required | SubjectRef or accepted canonical Subject address; a Claim is about this Subject. |
| `predicate` | Required | ClaimType reference or canonical predicate address. |
| `value` | Required | Object value under the selected ClaimType; use SubjectRef, LiteralValue, or ExactContent where applicable. |
| `role` | Required | Normative, observation, environment_binding, or derivation role admitted by the ClaimType. |
| `rationale` | Required | Author-supplied explanation retained with this Claim/change decision. |
| `supported_by` | `None` | Independent selected evidence or an evidence-role CaptureRef. Exactly one evidence-source argument is required. |
| `copied_from` | `None` | Selected copied content or a reusable CaptureRef; does not upgrade it to independent evidence. |
| `self_source` | `None` | Explicit self-asserted source text. Exactly one of supported_by, copied_from, self_source. |
| `qualifier` | `None` | Optional qualifier of this Claim statement. |
| `effective_period` | `None` | Optional effective start/end instants, distinct from capture and acceptance times. |
| `revises` | `None` | Existing Claim identity to revise rather than minting a parallel lineage. |
| `dispositions` | `None` | Existing contender Claim IDs mapped to explicit dispositions. Duplicate normalized IDs refuse. |
| `subject_definition` | `None` | Subject draft carried with the Claim, if defining its Subject in the same operation. |
| `claim_type_definition` | `None` | ClaimType draft carried with the Claim, if defining its predicate in the same operation. |

<a id="api-changesetdraft-signed-attestation"></a>

### `ChangeSetDraft.signed_attestation`

[Source](src/cruxible_client/authoring/sdk.py#L725)

```text
signed_attestation(attestation: ClaimAttestationV2) -> ChangeSetDraft
```

Add an already signed statement, without changing its bytes or Claim.

It becomes accepted only when this changeset passes ordinary approval
and activation. The authenticated submitter need not be its signer.

| Parameter | Default | Meaning |
|---|---|---|
| `attestation` | Required | Already signed immutable ClaimAttestationV2 envelope. |

<a id="api-changesetdraft-attestation"></a>

### `ChangeSetDraft.attestation`

[Source](src/cruxible_client/authoring/sdk.py#L744)

```text
attestation(
    claim: ClaimRef | str,
    *,
    stance: ClaimStance,
    signer: ClaimAttestationV2Signer,
    valid_until: datetime | None = None,
) -> ChangeSetDraft
```

Sign an exact Claim and stage it in this governed batch.

| Parameter | Default | Meaning |
|---|---|---|
| `claim` | Required | Claim ID or typed ClaimRef; typed refs also assert their observed coordinate. |
| `stance` | Required | Signed attestation stance admitted by ClaimStance. |
| `signer` | Required | Caller-provisioned signing capability of the protocol required by this method. |
| `valid_until` | `None` | Optional attestation expiry instant. |

<a id="api-changesetdraft-subject"></a>

### `ChangeSetDraft.subject`

[Source](src/cruxible_client/authoring/sdk.py#L792)

```text
subject(definition: SubjectDraft | SubjectShell) -> PendingSubjectRef
```

Define one Subject inside this changeset, and return a ref to it.

A Claim member may still carry its Subject as a dependency draft; this
is for the Subjects a set defines that no single Claim owns.

The ref it returns is usable as `subject=` or `value=` in the same set,
which is what lets one changeset define a Subject and say something
about it without the caller retyping the address as a string.

| Parameter | Default | Meaning |
|---|---|---|
| `definition` | Required | Typed definition being authored; use the exact input type shown in this operation’s signature. |

<a id="api-changesetdraft-capture-contract"></a>

### `ChangeSetDraft.capture_contract`

[Source](src/cruxible_client/authoring/sdk.py#L817)

```text
capture_contract(contract: CaptureContractV1) -> ChangeSetDraft
```

Define one CaptureContract inside this changeset.

| Parameter | Default | Meaning |
|---|---|---|
| `contract` | Required | Typed contract or exact contract reference named by the signature. |

<a id="api-changesetdraft-resolution-contract"></a>

### `ChangeSetDraft.resolution_contract`

[Source](src/cruxible_client/authoring/sdk.py#L830)

```text
resolution_contract(contract: ResolutionContractV1) -> ChangeSetDraft
```

Define one ResolutionContract inside this changeset.

| Parameter | Default | Meaning |
|---|---|---|
| `contract` | Required | Typed contract or exact contract reference named by the signature. |

<a id="api-changesetdraft-acquisition-policy"></a>

### `ChangeSetDraft.acquisition_policy`

[Source](src/cruxible_client/authoring/sdk.py#L843)

```text
acquisition_policy(policy: SourceAcquisitionPolicyV1) -> ChangeSetDraft
```

Define one SourceAcquisitionPolicy inside this changeset.

| Parameter | Default | Meaning |
|---|---|---|
| `policy` | Required | Explicit typed policy for the corresponding operation. |

<a id="api-changesetdraft-procedure"></a>

### `ChangeSetDraft.procedure`

[Source](src/cruxible_client/authoring/sdk.py#L856)

```text
procedure(*, definition: ProcedureInput | ProcedureSequence) -> ChangeSetDraft
```

Compose a Procedure with its Line and mandate in one existing changeset.

| Parameter | Default | Meaning |
|---|---|---|
| `definition` | Required | Typed definition being authored; use the exact input type shown in this operation’s signature. |

<a id="api-changesetdraft-procedure-mandate"></a>

### `ChangeSetDraft.procedure_mandate`

[Source](src/cruxible_client/authoring/sdk.py#L870)

```text
procedure_mandate(definition: ProcedureMandateInputV1) -> ChangeSetDraft
```

Stage a typed mandate through the shared authoring-input lowering.

| Parameter | Default | Meaning |
|---|---|---|
| `definition` | Required | Typed definition being authored; use the exact input type shown in this operation’s signature. |

<a id="api-changesetdraft-line"></a>

### `ChangeSetDraft.line`

[Source](src/cruxible_client/authoring/sdk.py#L884)

```text
line(
    *,
    name: str,
    procedure: str,
    acquisition_policy: str,
    requested_terminal_rung: Literal[1, 2, 3],
    trigger_policy: TriggerPolicyV2 | None = None,
    parameters: CanonicalValue | None = None,
    budgets: Mapping[str, int] | None = None,
    occurrence_epoch: int = 1,
    retire: bool = False,
) -> ChangeSetDraft
```

Define one Line inside this changeset, naming its Procedure and policy.

Lowering resolves both names -- accepted at the base or defined earlier
in this same set -- into the exact pins the LineSpec carries. A Line is
manual unless another trigger policy is given, and inherits the
Procedure's hard caps as its budget unless one is given.

Lowering refuses a Procedure that is not graph-v4/v5 and one whose Source
nodes leave a Provider slot open: the Line pins exactly what the
Procedure names, and an open slot is nothing to pin. A rung-2 Line
also needs a live ProcedureMandate over its target namespace before it
can run; that is checked at admission, not here.

| Parameter | Default | Meaning |
|---|---|---|
| `name` | Required | Definition identity name, not an arbitrary file path. |
| `procedure` | Required | Procedure name or typed reference; Line authoring also accepts a name defined earlier in the same changeset. |
| `acquisition_policy` | Required | Accepted SourceAcquisitionPolicy name for source authority. |
| `requested_terminal_rung` | Required | Requested Line capability rung 1, 2, or 3; effective authority is checked at admission. |
| `trigger_policy` | `None` | Typed Line trigger policy; None authors a manual trigger. |
| `parameters` | `None` | Invocation/query parameters in the declared canonical contract. |
| `budgets` | `None` | Operation-specific bounds; the signature distinguishes QueryBudgetsV1 from Line budget mappings. |
| `occurrence_epoch` | `1` | Positive epoch distinguishing Line occurrence identity. |
| `retire` | `False` | Whether the authored Line is a retirement. |

<a id="api-changesetdraft-query-definition"></a>

### `ChangeSetDraft.query_definition`

[Source](src/cruxible_client/authoring/sdk.py#L931)

```text
query_definition(
    definition: QueryDefinitionInput,
    *,
    vocabulary: Sequence[ClaimTypeRef] = (),
) -> ChangeSetDraft
```

Add a named query after its vocabulary definitions in this changeset.

| Parameter | Default | Meaning |
|---|---|---|
| `definition` | Required | Typed definition being authored; use the exact input type shown in this operation’s signature. |
| `vocabulary` | `()` | World ClaimType references used by the query, retaining stale-reference assertions. |

<a id="api-changesetdraft-claim-type"></a>

### `ChangeSetDraft.claim_type`

[Source](src/cruxible_client/authoring/sdk.py#L950)

```text
claim_type(definition: ClaimTypeDraft | ClaimType) -> PendingClaimTypeRef
```

| Parameter | Default | Meaning |
|---|---|---|
| `definition` | Required | Typed definition being authored; use the exact input type shown in this operation’s signature. |

<a id="api-changesetdraft-retire"></a>

### `ChangeSetDraft.retire`

[Source](src/cruxible_client/authoring/sdk.py#L976)

```text
retire(
    claim: str | ClaimRef,
    *,
    reason: ClaimRetirementReason,
    effective_until: datetime | None = None,
    dependents: Sequence[ClaimRetireDependentV1] = (),
) -> ChangeSetDraft
```

Retire one accepted Claim, and its live closure, inside this changeset.

Takes exactly what `Playbill.retire_claim` takes: the SDK's own rows
and refs spell a Claim identity `Claim:CLM-...`, so a builder that
refused the prefix made one library disagree with itself. The member
carries the one canonical bare spelling, which is what keeps two
spellings of one retirement on one member identity and one digest.

| Parameter | Default | Meaning |
|---|---|---|
| `claim` | Required | Claim ID or typed ClaimRef; typed refs also assert their observed coordinate. |
| `reason` | Required | Attributed reason for refusal, retirement, or operational action as specified by the API. |
| `effective_until` | `None` | Optional effective-end instant for retirement. |
| `dependents` | `()` | Explicit dependency-closure dispositions required by retirement or ClaimType succession. |

<a id="api-changesetdraft-succeed-claim-type"></a>

### `ChangeSetDraft.succeed_claim_type`

[Source](src/cruxible_client/authoring/sdk.py#L1009)

```text
succeed_claim_type(
    successor: ClaimTypeDraft | ClaimType,
    *,
    dependents: Sequence[ClaimTypeSuccessionDependentV1] = (),
) -> ChangeSetDraft
```

Succeed one accepted ClaimType, and settle its closure, in this set.

Vocabulary evolution is one epistemic move -- "I need this distinction,
and here is everything it changes" -- so it lands in the same signed
generation as the Claims that speak the new vocabulary. Write the
dependents with `carry`, `rescind`, `retire` and `re_author`; the
closure must be exact, and preflight names every member of it that is
still missing.

| Parameter | Default | Meaning |
|---|---|---|
| `successor` | Required | Complete successor ClaimType naming its predecessor. |
| `dependents` | `()` | Explicit dependency-closure dispositions required by retirement or ClaimType succession. |

<a id="api-changesetdraft-prepare"></a>

### `ChangeSetDraft.prepare`

[Source](src/cruxible_client/authoring/sdk.py#L1054)

```text
prepare() -> Intent
```

Compile and preflight the whole changeset as one intent.

## Drafts, intents, proposals, and approvals

`ClaimDraft`, `ProcedureDraft`, `QueryDraft`, and `SubjectDraft` inherit
`prepare() -> Intent`. They expose `payload`, `reference_expectations`,
`program_stamp`, and `source_map` for inspection. `prepare()` performs server
compilation/preflight; the program stamp is structured authoring provenance,
not retained executable Python source. `ClaimTypeDraft.propose(...)` and
`SubjectDraft.propose(...)` are direct proposal helpers and submit immediately;
they are not synonyms for local staging.

`Intent` and `Proposal` are obtained from SDK factories. `from_preflight` and
`from_inspection` are advanced response adapters. Cached properties are not
automatic status polling. `Intent.path_to_acceptance` calls status, and
publication properties may read the daemon if their local evidence is missing.
`Intent.rebase()` clears observed preflight/status; prepare again before relying
on a new candidate. `reprepare()` requires the same owning connection.

`Proposal.review()` returns an immutable process-local `ReviewedProposal`.
`approve(signer=..., reviewed=...)` checks exact proposal/candidate/session/instance
binding, obtains current governance/challenge data, signs locally, and submits
public approval. Its `.details` returns a fresh copy. The token is not proof a
human read it. A new process must review again. Wait methods return the last
observed status at timeout; they do not throw a synthetic failure or retry writes.

`Publication` is a retained cleanup surface for existing insertion expectations.
New `publish_to` authoring is removed. `abandon()` releases an existing
expectation; use projection blocks for newly authored views.

<a id="api-claimdraft"></a>

## `ClaimDraft`

Import: `cruxible_client.authoring.sdk.ClaimDraft`. [Source](src/cruxible_client/authoring/sdk.py#L539)

| Field | Type | Default / construction |
|---|---|---|
| `payload` | `ClaimAuthoringPayloadV1 \| ClaimAuthoringPayloadV2 \| ClaimAuthoringPayloadV3 \| ProcedureAuthoringPayloadV1 \| ProcedureAuthoringPayloadV2 \| SubjectAuthoringPayloadV1 \| ChangeSetAuthoringPayloadV1 \| QueryDefinitionAuthoringPayloadV1` | `Required` |
| `reference_expectations` | `tuple[AuthoringReferenceExpectationV1, ...]` | `Required` |
| `program_stamp` | `AuthoringProgramStampV1` | `Required` |
| `source_map` | `DiagnosticSourceMap` | `Required` |

<a id="api-claimdraft-derived-by"></a>

### `ClaimDraft.derived_by`

[Source](src/cruxible_client/authoring/sdk.py#L540)

```text
derived_by(derivation: object) -> ClaimDraft
```

<a id="api-proceduredraft"></a>

## `ProcedureDraft`

Import: `cruxible_client.authoring.sdk.ProcedureDraft`. [Source](src/cruxible_client/authoring/sdk.py#L572)

| Field | Type | Default / construction |
|---|---|---|
| `payload` | `ClaimAuthoringPayloadV1 \| ClaimAuthoringPayloadV2 \| ClaimAuthoringPayloadV3 \| ProcedureAuthoringPayloadV1 \| ProcedureAuthoringPayloadV2 \| SubjectAuthoringPayloadV1 \| ChangeSetAuthoringPayloadV1 \| QueryDefinitionAuthoringPayloadV1` | `Required` |
| `reference_expectations` | `tuple[AuthoringReferenceExpectationV1, ...]` | `Required` |
| `program_stamp` | `AuthoringProgramStampV1` | `Required` |
| `source_map` | `DiagnosticSourceMap` | `Required` |

<a id="api-querydraft"></a>

## `QueryDraft`

Import: `cruxible_client.authoring.sdk.QueryDraft`. [Source](src/cruxible_client/authoring/sdk.py#L577)

| Field | Type | Default / construction |
|---|---|---|
| `payload` | `ClaimAuthoringPayloadV1 \| ClaimAuthoringPayloadV2 \| ClaimAuthoringPayloadV3 \| ProcedureAuthoringPayloadV1 \| ProcedureAuthoringPayloadV2 \| SubjectAuthoringPayloadV1 \| ChangeSetAuthoringPayloadV1 \| QueryDefinitionAuthoringPayloadV1` | `Required` |
| `reference_expectations` | `tuple[AuthoringReferenceExpectationV1, ...]` | `Required` |
| `program_stamp` | `AuthoringProgramStampV1` | `Required` |
| `source_map` | `DiagnosticSourceMap` | `Required` |

<a id="api-subjectdraft"></a>

## `SubjectDraft`

Import: `cruxible_client.authoring.sdk.SubjectDraft`. [Source](src/cruxible_client/authoring/sdk.py#L1111)

| Field | Type | Default / construction |
|---|---|---|
| `payload` | `ClaimAuthoringPayloadV1 \| ClaimAuthoringPayloadV2 \| ClaimAuthoringPayloadV3 \| ProcedureAuthoringPayloadV1 \| ProcedureAuthoringPayloadV2 \| SubjectAuthoringPayloadV1 \| ChangeSetAuthoringPayloadV1 \| QueryDefinitionAuthoringPayloadV1` | `Required` |
| `reference_expectations` | `tuple[AuthoringReferenceExpectationV1, ...]` | `Required` |
| `program_stamp` | `AuthoringProgramStampV1` | `Required` |
| `source_map` | `DiagnosticSourceMap` | `Required` |
| `shell` | `SubjectShell` | `Required` |

<a id="api-subjectdraft-address"></a>

### `SubjectDraft.address`

[Source](src/cruxible_client/authoring/sdk.py#L1115)

```text
address: str
```

<a id="api-subjectdraft-propose"></a>

### `SubjectDraft.propose`

[Source](src/cruxible_client/authoring/sdk.py#L1118)

```text
propose(*, proposal_name: str) -> Proposal
```

<a id="api-claimtypedraft"></a>

## `ClaimTypeDraft`

Import: `cruxible_client.authoring.sdk.ClaimTypeDraft`. [Source](src/cruxible_client/authoring/sdk.py#L491)

| Field | Type | Default / construction |
|---|---|---|
| `definition` | `ClaimType` | `Required` |

<a id="api-claimtypedraft-predicate"></a>

### `ClaimTypeDraft.predicate`

[Source](src/cruxible_client/authoring/sdk.py#L496)

```text
predicate: str
```

<a id="api-claimtypedraft-propose"></a>

### `ClaimTypeDraft.propose`

[Source](src/cruxible_client/authoring/sdk.py#L499)

```text
propose(*, proposal_name: str) -> Proposal
```

<a id="api-intent"></a>

## `Intent`

Import: `cruxible_client.authoring.sdk.Intent`. [Source](src/cruxible_client/authoring/sdk.py#L1127)

<a id="api-intent-from-preflight"></a>

### `Intent.from_preflight`

[Source](src/cruxible_client/authoring/sdk.py#L1144)

```text
from_preflight(
    playbill: Playbill,
    draft: _IntentDraft,
    result: api.PlaybillAuthoringPreflightResult,
) -> Intent
```

<a id="api-intent-intent-id"></a>

### `Intent.intent_id`

[Source](src/cruxible_client/authoring/sdk.py#L1159)

```text
intent_id: str
```

<a id="api-intent-revision"></a>

### `Intent.revision`

[Source](src/cruxible_client/authoring/sdk.py#L1166)

```text
revision: int
```

<a id="api-intent-refused"></a>

### `Intent.refused`

[Source](src/cruxible_client/authoring/sdk.py#L1173)

```text
refused: bool
```

<a id="api-intent-lint"></a>

### `Intent.lint`

[Source](src/cruxible_client/authoring/sdk.py#L1177)

```text
lint: api.PlaybillClaimTypeProposalLint | None
```

<a id="api-intent-warnings"></a>

### `Intent.warnings`

[Source](src/cruxible_client/authoring/sdk.py#L1181)

```text
warnings: tuple[dict[str, Any], ...]
```

<a id="api-intent-diagnostics"></a>

### `Intent.diagnostics`

[Source](src/cruxible_client/authoring/sdk.py#L1186)

```text
diagnostics: tuple[Diagnostic, ...]
```

<a id="api-intent-path-to-acceptance"></a>

### `Intent.path_to_acceptance`

[Source](src/cruxible_client/authoring/sdk.py#L1215)

```text
path_to_acceptance: tuple[dict[str, object], ...]
```

<a id="api-intent-proposal"></a>

### `Intent.proposal`

[Source](src/cruxible_client/authoring/sdk.py#L1220)

```text
proposal: Proposal | None
```

Last observed proposal identity, without a server read.

Populated by resume_intent(), submit() or status(); None means no proposal was observed
for this local intent revision. Call status() for fresh server state.
This handle is not review, approval, or proof of activation eligibility.

<a id="api-intent-publication"></a>

### `Intent.publication`

[Source](src/cruxible_client/authoring/sdk.py#L1234)

```text
publication: Publication | None
```

The one publication a singular Claim intent owns, if it has one.

<a id="api-intent-publications"></a>

### `Intent.publications`

[Source](src/cruxible_client/authoring/sdk.py#L1246)

```text
publications: tuple[Publication, ...]
```

Every publication this intent owns, one per publishing Claim member.

<a id="api-intent-prepare"></a>

### `Intent.prepare`

[Source](src/cruxible_client/authoring/sdk.py#L1264)

```text
prepare() -> Intent
```

<a id="api-intent-reprepare"></a>

### `Intent.reprepare`

[Source](src/cruxible_client/authoring/sdk.py#L1275)

```text
reprepare(*, draft: ClaimDraft | ProcedureDraft | SubjectDraft) -> Intent
```

<a id="api-intent-submit"></a>

### `Intent.submit`

[Source](src/cruxible_client/authoring/sdk.py#L1295)

```text
submit() -> Intent
```

<a id="api-intent-status"></a>

### `Intent.status`

[Source](src/cruxible_client/authoring/sdk.py#L1304)

```text
status() -> api.PlaybillCandidateStatus
```

<a id="api-intent-rebase"></a>

### `Intent.rebase`

[Source](src/cruxible_client/authoring/sdk.py#L1311)

```text
rebase() -> Intent
```

<a id="api-intent-wait-for-acceptance"></a>

### `Intent.wait_for_acceptance`

[Source](src/cruxible_client/authoring/sdk.py#L1320)

```text
wait_for_acceptance(*, timeout: Duration, poll_interval: Duration) -> api.PlaybillCandidateStatus
```

<a id="api-proposal"></a>

## `Proposal`

Import: `cruxible_client.authoring.sdk.Proposal`. [Source](src/cruxible_client/authoring/sdk.py#L1332)

<a id="api-proposal-from-inspection"></a>

### `Proposal.from_inspection`

[Source](src/cruxible_client/authoring/sdk.py#L1345)

```text
from_inspection(playbill: Playbill, inspection: api.PlaybillProposalInspection) -> Proposal
```

<a id="api-proposal-review"></a>

### `Proposal.review`

[Source](src/cruxible_client/authoring/sdk.py#L1355)

```text
review() -> ReviewedProposal
```

Fetch an immutable full review; inspect its details before approving.

<a id="api-proposal-approve"></a>

### `Proposal.approve`

[Source](src/cruxible_client/authoring/sdk.py#L1359)

```text
approve(*, signer: ApprovalSigner, reviewed: ReviewedProposal) -> api.PlaybillApprovalReceipt
```

Sign this exact review with caller-configured custody; never activate.

<a id="api-proposal-warnings"></a>

### `Proposal.warnings`

[Source](src/cruxible_client/authoring/sdk.py#L1366)

```text
warnings: tuple[dict[str, Any], ...]
```

<a id="api-proposal-status"></a>

### `Proposal.status`

[Source](src/cruxible_client/authoring/sdk.py#L1369)

```text
status() -> api.PlaybillProposalListEntry
```

<a id="api-proposal-wait-for-acceptance"></a>

### `Proposal.wait_for_acceptance`

[Source](src/cruxible_client/authoring/sdk.py#L1377)

```text
wait_for_acceptance(*, timeout: Duration, poll_interval: Duration) -> api.PlaybillProposalListEntry
```

<a id="api-publication"></a>

## `Publication`

Import: `cruxible_client.authoring.sdk.Publication`. [Source](src/cruxible_client/authoring/sdk.py#L1393)

<a id="api-publication-state"></a>

### `Publication.state`

[Source](src/cruxible_client/authoring/sdk.py#L1407)

```text
state: str
```

<a id="api-publication-expectation-id"></a>

### `Publication.expectation_id`

[Source](src/cruxible_client/authoring/sdk.py#L1411)

```text
expectation_id: str
```

<a id="api-publication-status"></a>

### `Publication.status`

[Source](src/cruxible_client/authoring/sdk.py#L1417)

```text
status() -> str
```

<a id="api-publication-abandon"></a>

### `Publication.abandon`

[Source](src/cruxible_client/authoring/sdk.py#L1427)

```text
abandon() -> Publication
```

<a id="api-prediction"></a>

## `Prediction`

Import: `cruxible_client.authoring.sdk.Prediction`. [Source](src/cruxible_client/authoring/sdk.py#L550)

| Field | Type | Default / construction |
|---|---|---|
| `contract_identity` | `str` | `Required` |
| `contract_digest` | `str` | `Required` |
| `intent_id` | `str` | `Required` |
| `proposal_id` | `str` | `Required` |

<a id="api-prediction-proposal"></a>

### `Prediction.proposal`

[Source](src/cruxible_client/authoring/sdk.py#L560)

```text
proposal: Proposal
```

<a id="api-predictionsettlement"></a>

## `PredictionSettlement`

Import: `cruxible_client.authoring.sdk.PredictionSettlement`. [Source](src/cruxible_client/authoring/sdk.py#L565)

| Field | Type | Default / construction |
|---|---|---|
| `prediction_id` | `str` | `Required` |
| `outcome` | `bool` | `Required` |
| `relation` | `dict[str, object]` | `Required` |

<a id="api-claimview"></a>

## `ClaimView`

Import: `cruxible_client.authoring.sdk.ClaimView`. [Source](src/cruxible_client/authoring/sdk.py#L269)

| Field | Type | Default / construction |
|---|---|---|
| `claim_id` | `str` | `Required` |
| `revision` | `int` | `Required` |
| `subject` | `str` | `Required` |
| `predicate` | `str` | `Required` |
| `qualifier` | `str \| None` | `Required` |
| `role` | `str` | `Required` |
| `object_kind` | `str` | `Required` |
| `value` | `object` | `Required` |
| `lifecycle_state` | `str` | `Required` |
| `verdict` | `str` | `Required` |
| `captures` | `tuple[CaptureRef, ...]` | `Required` |

<a id="api-knowledgecard"></a>

## `KnowledgeCard`

Import: `cruxible_client.authoring.sdk.KnowledgeCard`. [Source](src/cruxible_client/authoring/sdk.py#L443)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `RefKind` | `Required` |
| `identity` | `str` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `value` | `object` | `Required` |

<a id="api-knowledgecard-ref"></a>

### `KnowledgeCard.ref`

[Source](src/cruxible_client/authoring/sdk.py#L450)

```text
ref: TypedRef
```

<a id="api-searchpage"></a>

## `SearchPage`

Import: `cruxible_client.authoring.sdk.SearchPage`. [Source](src/cruxible_client/authoring/sdk.py#L466)

| Field | Type | Default / construction |
|---|---|---|
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `evaluation_time` | `str` | `Required` |
| `rows` | `tuple[dict[str, object], ...]` | `Required` |
| `result_digest` | `str` | `Required` |
| `cursor` | `dict[str, object] \| None` | `Required` |
| `truncated` | `bool` | `Required` |
| `orientation` | `dict[str, object] \| None` | `None` |

<a id="api-nextpage"></a>

## `NextPage`

Import: `cruxible_client.authoring.sdk.NextPage`. [Source](src/cruxible_client/authoring/sdk.py#L477)

| Field | Type | Default / construction |
|---|---|---|
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `evaluation_time` | `str` | `Required` |
| `items` | `tuple[dict[str, object], ...]` | `Required` |
| `result_digest` | `str` | `Required` |
| `observed_domains` | `tuple[str, ...]` | `Required` |
| `unobserved_domains` | `tuple[str, ...]` | `Required` |
| `attestation_head_digest` | `str \| None` | `None` |

<a id="api-nextpage-iter"></a>

### `NextPage.__iter__`

[Source](src/cruxible_client/authoring/sdk.py#L486)

```text
__iter__()
```

<a id="api-measurementoutcome"></a>

## `MeasurementOutcome`

Import: `cruxible_client.authoring.sdk.MeasurementOutcome`. [Source](src/cruxible_client/authoring/sdk.py#L3359)

| Field | Type | Default / construction |
|---|---|---|
| `measurement_name` | `str` | `Required` |
| `status` | `str` | `Required` |
| `reading_status` | `str` | `Required` |
| `verdict` | `str \| None` | `Required` |
| `resolution_id` | `str \| None` | `Required` |
| `reading_id` | `str \| None` | `Required` |
| `detail` | `str \| None` | `Required` |
| `raw` | `api.ProcedureMeasurementRowV1` | `Required` |

<a id="api-measurementbatch"></a>

## `MeasurementBatch`

Import: `cruxible_client.authoring.sdk.MeasurementBatch`. [Source](src/cruxible_client/authoring/sdk.py#L3373)

| Field | Type | Default / construction |
|---|---|---|
| `run_id` | `str \| None` | `Required` |
| `activation_coordinate` | `AcceptedCoordinate` | `Required` |
| `observation_coordinate` | `AcceptedCoordinate` | `Required` |
| `observation_time` | `datetime` | `Required` |
| `outcomes` | `tuple[MeasurementOutcome, ...]` | `Required` |
| `raw` | `api.PlaybillProcedureMeasureResultV1` | `Required` |

<a id="api-measurementbatch-getitem"></a>

### `MeasurementBatch.__getitem__`

[Source](src/cruxible_client/authoring/sdk.py#L3383)

```text
__getitem__(measurement_name: str) -> MeasurementOutcome
```

<a id="api-reviewedproposal"></a>

## `ReviewedProposal`

Import: `cruxible_client.authoring.approval.ReviewedProposal`. [Source](src/cruxible_client/authoring/approval.py#L49)

Use `Proposal.review()`; `.details` is a defensive copy of the exact review.

| Field | Type | Default / construction |
|---|---|---|
| `proposal_id` | `str` | `Required` |
| `candidate_digest` | `str` | `Required` |

<a id="api-reviewedproposal-details"></a>

### `ReviewedProposal.details`

[Source](src/cruxible_client/authoring/approval.py#L63)

```text
details: api.PlaybillProposalReview
```

<a id="api-authoring-sdk-carry"></a>

### `authoring.sdk.carry`

[Source](src/cruxible_client/authoring/sdk.py#L581)

```text
carry(claim: str | ClaimRef) -> ClaimTypeSuccessionDependentV1
```

Carry one dependent to the successor by re-pinning it, unchanged.

Available when the dependent still says something true under the successor.
A successor that changes `object_kind` refuses this for a live Claim: its
object no longer says what the ClaimType now means.

| Parameter | Default | Meaning |
|---|---|---|
| `claim` | Required | Claim ID or typed ClaimRef; typed refs also assert their observed coordinate. |

<a id="api-authoring-sdk-rescind"></a>

### `authoring.sdk.rescind`

[Source](src/cruxible_client/authoring/sdk.py#L595)

```text
rescind(claim: str | ClaimRef) -> ClaimTypeSuccessionDependentV1
```

Tombstone one dependent because it should never have been stated.

The tombstone keeps the exact statement it was accepted with, under the
vocabulary it was accepted under -- that is what makes the record readable
after the vocabulary moves, rather than silently rewritten.

| Parameter | Default | Meaning |
|---|---|---|
| `claim` | Required | Claim ID or typed ClaimRef; typed refs also assert their observed coordinate. |

<a id="api-authoring-sdk-retire"></a>

### `authoring.sdk.retire`

[Source](src/cruxible_client/authoring/sdk.py#L610)

```text
retire(
    claim: str | ClaimRef,
    *,
    reason: ClaimRetirementReason,
    effective_until: datetime | None = None,
) -> ClaimTypeSuccessionDependentV1
```

Retire one dependent with an attributed reason as the succession lands.

| Parameter | Default | Meaning |
|---|---|---|
| `claim` | Required | Claim ID or typed ClaimRef; typed refs also assert their observed coordinate. |
| `reason` | Required | Attributed reason for refusal, retirement, or operational action as specified by the API. |
| `effective_until` | `None` | Optional effective-end instant for retirement. |

<a id="api-authoring-sdk-re-author"></a>

### `authoring.sdk.re_author`

[Source](src/cruxible_client/authoring/sdk.py#L626)

```text
re_author(claim: str | ClaimRef, *, with_: str | ClaimRef | None=None) -> ClaimTypeSuccessionDependentV1
```

Say this dependent again, under the successor, as a sibling Claim member.

The sibling revises this same Claim -- a re-authoring keeps the identity,
the subject, the predicate and the exact predecessor digest of what it
re-states -- so `with_` is only ever an explicit spelling of what `claim`
already says, and `re_author(claim)` alone is complete.

| Parameter | Default | Meaning |
|---|---|---|
| `claim` | Required | Claim ID or typed ClaimRef; typed refs also assert their observed coordinate. |
| `with_` | `None` | Value required by the declared type; see operation semantics below. |

## World and typed values

A host `World` is a coordinate-bound read facade over accepted ClaimTypes and
Subjects. `world.<kind>[id]` names a Subject; Subject field access returns tuples
of live Claim contenders. There is no `.one()`, `resolved(...)`, or `state_input`
in this implemented surface. Cardinality-one metadata does not select a scalar.
Missing Subjects raise `AbsentSubject`; ambiguous names and unavailable fields
raise explicit structure/attribute errors. Index/full-name forms disambiguate
Python keyword and member collisions.

| Expression | Result |
|---|---|
| `world.kind("security.asset")` | KindNamespace, including collision-safe lookup |
| `world.security.asset["gateway"]` | WorldSubject at this coordinate |
| `subject["security.asset.release"]` | Live Claim tuple for one full predicate |
| `subject.release` | Same selection if the leaf is unambiguous |
| `subject.claims` | Live Claim tuple across the Subject’s predicates |
| `world.claim_type("security.asset.status")` | WorldClaimType |
| `claim_type("active")` or `claim_type.active` | Predicate-bound LiteralValue, validated locally |
| `claim_type.as_kind` | KindNamespace when a predicate name is also a Subject kind |
| `kind.define("new-id")` | SubjectDraft; does not accept or publish it |
| `world.stub()` | Generated type-stub source for this vocabulary |

`prefetch` installs only complete bounded selections. An exhausted budget,
invalid coordinate, duplicate row, or non-advancing continuation refuses before
a partial cache is installed. Current `ClaimView.value` is annotated `object`;
the constrained vocabulary and daemon checks are stronger than that annotation.
Generated stubs expose declared names and Claim tuples; they do not provide
the proposed typed Procedure field-selection wrapper.

<a id="api-world"></a>

## `World`

Import: `cruxible_client.authoring.world.World`. [Source](src/cruxible_client/authoring/world.py#L497)

<a id="api-world-coordinate"></a>

### `World.coordinate`

[Source](src/cruxible_client/authoring/world.py#L540)

```text
coordinate: AcceptedCoordinate
```

<a id="api-world-kinds"></a>

### `World.kinds`

[Source](src/cruxible_client/authoring/world.py#L544)

```text
kinds: tuple[str, ...]
```

Every accepted Subject kind this world knows, byte-sorted.

<a id="api-world-predicates"></a>

### `World.predicates`

[Source](src/cruxible_client/authoring/world.py#L551)

```text
predicates: tuple[str, ...]
```

Every accepted predicate this world knows, byte-sorted.

<a id="api-world-stub"></a>

### `World.stub`

[Source](src/cruxible_client/authoring/world.py#L563)

```text
stub() -> str
```

Render this world as a `.pyi` module stub.

<a id="api-world-claim-type"></a>

### `World.claim_type`

[Source](src/cruxible_client/authoring/world.py#L621)

```text
claim_type(predicate: str) -> WorldClaimType
```

Read one accepted predicate by its full dotted name.

<a id="api-world-kind"></a>

### `World.kind`

[Source](src/cruxible_client/authoring/world.py#L631)

```text
kind(subject_kind: str) -> KindNamespace
```

Read one accepted Subject kind by its full dotted name.

The escape for a kind whose segments attribute access cannot spell -- a
Python keyword such as `dev.class` -- and for one a predicate of the same
dotted name wins, exactly as `claim_type` is the escape for a predicate.

<a id="api-world-prefetch"></a>

### `World.prefetch`

[Source](src/cruxible_client/authoring/world.py#L785)

```text
prefetch(
    *,
    subjects: Sequence[str | SubjectRef],
    predicates: Sequence[str | ClaimTypeRef] = (),
    page_size: int = 128,
    max_claims: int = 4096,
) -> tuple[ClaimView, ...]
```

Fill selected attribute caches in bounded, coordinate-pinned pages.

Strings are subject kind/id addresses or paths and fully qualified predicates.
Every live contender is retained. If the explicit budget is exceeded,
no partial attribute cache is installed and the caller can narrow the
selection or increase `max_claims`.

<a id="api-worldsubject"></a>

## `WorldSubject`

Import: `cruxible_client.authoring.world.WorldSubject`. [Source](src/cruxible_client/authoring/world.py#L331)

<a id="api-worldsubject-subject-kind"></a>

### `WorldSubject.subject_kind`

[Source](src/cruxible_client/authoring/world.py#L346)

```text
subject_kind: str
```

<a id="api-worldsubject-subject-id"></a>

### `WorldSubject.subject_id`

[Source](src/cruxible_client/authoring/world.py#L350)

```text
subject_id: str
```

<a id="api-worldsubject-claims"></a>

### `WorldSubject.claims`

[Source](src/cruxible_client/authoring/world.py#L354)

```text
claims: tuple[ClaimView, ...]
```

Every live Claim this Subject is the subject of.

<a id="api-worldsubject-explain"></a>

### `WorldSubject.explain`

[Source](src/cruxible_client/authoring/world.py#L359)

```text
explain() -> object
```

Read this Subject's governance and provenance context.

<a id="api-worldsubject-getitem"></a>

### `WorldSubject.__getitem__`

[Source](src/cruxible_client/authoring/world.py#L365)

```text
__getitem__(predicate: str | ClaimTypeRef) -> tuple[ClaimView, ...]
```

Read the live Claims under one predicate, named in full or by leaf.

<a id="api-worldclaimtype"></a>

## `WorldClaimType`

Import: `cruxible_client.authoring.world.WorldClaimType`. [Source](src/cruxible_client/authoring/world.py#L228)

| Field | Type | Default / construction |
|---|---|---|
| `object_kind` | `ClaimObjectKind` | `Required` |
| `cardinality` | `Cardinality` | `Required` |
| `allowed_subject_kinds` | `tuple[str, ...]` | `Required` |
| `allowed_object_subject_kinds` | `tuple[str, ...]` | `Required` |
| `permitted_roles` | `tuple[ClaimRole, ...]` | `Required` |
| `referent_sensitivity` | `ReferentSensitivity` | `Required` |
| `literal_schema` | `dict[str, object] \| None` | `Required` |

<a id="api-worldclaimtype-predicate"></a>

### `WorldClaimType.predicate`

[Source](src/cruxible_client/authoring/world.py#L249)

```text
predicate: str
```

<a id="api-worldclaimtype-members"></a>

### `WorldClaimType.members`

[Source](src/cruxible_client/authoring/world.py#L253)

```text
members: tuple[str, ...]
```

Return the enum members this predicate's literal schema names.

<a id="api-worldclaimtype-as-kind"></a>

### `WorldClaimType.as_kind`

[Source](src/cruxible_client/authoring/world.py#L259)

```text
as_kind: KindNamespace
```

Reach the Subject kind this dotted name also names.

A ClaimType wins attribute access over a Subject kind of the same dotted
name, which would otherwise leave `define()` and `subject_ids`
unreachable. This is that escape.

<a id="api-worldclaimtype-call"></a>

### `WorldClaimType.__call__`

[Source](src/cruxible_client/authoring/world.py#L275)

```text
__call__(value: object) -> LiteralValue
```

Mint one literal object for this predicate, admitted before the wire.

<a id="api-worldclaimtype-getitem"></a>

### `WorldClaimType.__getitem__`

[Source](src/cruxible_client/authoring/world.py#L290)

```text
__getitem__(subject_id: str) -> WorldSubject
```

Read a Subject when this dotted name is also an accepted kind.

<a id="api-kindnamespace"></a>

## `KindNamespace`

Import: `cruxible_client.authoring.world.KindNamespace`. [Source](src/cruxible_client/authoring/world.py#L400)

<a id="api-kindnamespace-subject-kind"></a>

### `KindNamespace.subject_kind`

[Source](src/cruxible_client/authoring/world.py#L417)

```text
subject_kind: str | None
```

Return this namespace's Subject kind, or None if it is only a prefix.

<a id="api-kindnamespace-subject-ids"></a>

### `KindNamespace.subject_ids`

[Source](src/cruxible_client/authoring/world.py#L423)

```text
subject_ids: tuple[str, ...]
```

Return every accepted Subject ID of this kind, loading them on first ask.

<a id="api-kindnamespace-define"></a>

### `KindNamespace.define`

[Source](src/cruxible_client/authoring/world.py#L428)

```text
define(subject_id: str) -> SubjectDraft
```

Draft one new Subject of this kind for a changeset to define.

<a id="api-kindnamespace-getitem"></a>

### `KindNamespace.__getitem__`

[Source](src/cruxible_client/authoring/world.py#L452)

```text
__getitem__(subject_id: str) -> WorldSubject
```

<a id="api-kindnamespace-iter"></a>

### `KindNamespace.__iter__`

[Source](src/cruxible_client/authoring/world.py#L466)

```text
__iter__() -> Iterator[WorldSubject]
```

### Reference and value construction

Reference dataclasses are frozen data carrying address and coordinate, not
authority. Pending refs are meaningful within the changeset that defines them.
CaptureRef additionally preserves citation role; copied/legacy evidence cannot
be upgraded by passing it as independent support. `CaptureView.ref` requires a
verified capture; `.content` requires verified available body bytes. `.text()`
can raise decoding errors; `.json()` can raise JSON decoding errors.

`ExactContent(str)` encodes UTF-8; bytes are retained exactly. It accepts no
invented media-type field. `Duration` uses nonnegative integer microseconds and
rejects booleans. EffectivePeriod normalizes UTC and requires end > start when
both are supplied. AccessProfile requires a canonical nonblank ID and sorted,
unique classes. Enum values below are the actual accepted SDK spellings.

<a id="api-typedref"></a>

## `TypedRef`

Import: `cruxible_client.authoring.sdk_types.TypedRef`. [Source](src/cruxible_client/authoring/sdk_types.py#L34)

<a id="api-typedref-kind"></a>

### `TypedRef.kind`

[Source](src/cruxible_client/authoring/sdk_types.py#L36)

```text
kind: RefKind
```

<a id="api-typedref-address"></a>

### `TypedRef.address`

[Source](src/cruxible_client/authoring/sdk_types.py#L39)

```text
address: str
```

<a id="api-typedref-coordinate"></a>

### `TypedRef.coordinate`

[Source](src/cruxible_client/authoring/sdk_types.py#L42)

```text
coordinate: AcceptedCoordinate
```

<a id="api-subjectref"></a>

## `SubjectRef`

Import: `cruxible_client.authoring.sdk_types.SubjectRef`. [Source](src/cruxible_client/authoring/sdk_types.py#L46)

| Field | Type | Default / construction |
|---|---|---|
| `address` | `str` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `kind` | `ClassVar[RefKind]` | `RefKind.SUBJECT` |

<a id="api-claimtyperef"></a>

## `ClaimTypeRef`

Import: `cruxible_client.authoring.sdk_types.ClaimTypeRef`. [Source](src/cruxible_client/authoring/sdk_types.py#L53)

| Field | Type | Default / construction |
|---|---|---|
| `address` | `str` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `kind` | `ClassVar[RefKind]` | `RefKind.CLAIM_TYPE` |

<a id="api-claimref"></a>

## `ClaimRef`

Import: `cruxible_client.authoring.sdk_types.ClaimRef`. [Source](src/cruxible_client/authoring/sdk_types.py#L72)

| Field | Type | Default / construction |
|---|---|---|
| `address` | `str` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `kind` | `ClassVar[RefKind]` | `RefKind.CLAIM` |

<a id="api-procedureref"></a>

## `ProcedureRef`

Import: `cruxible_client.authoring.sdk_types.ProcedureRef`. [Source](src/cruxible_client/authoring/sdk_types.py#L90)

| Field | Type | Default / construction |
|---|---|---|
| `address` | `str` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `kind` | `ClassVar[RefKind]` | `RefKind.PROCEDURE` |

<a id="api-queryref"></a>

## `QueryRef`

Import: `cruxible_client.authoring.sdk_types.QueryRef`. [Source](src/cruxible_client/authoring/sdk_types.py#L97)

| Field | Type | Default / construction |
|---|---|---|
| `address` | `str` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `kind` | `ClassVar[RefKind]` | `RefKind.QUERY` |

<a id="api-sourceref"></a>

## `SourceRef`

Import: `cruxible_client.authoring.sdk_types.SourceRef`. [Source](src/cruxible_client/authoring/sdk_types.py#L104)

| Field | Type | Default / construction |
|---|---|---|
| `address` | `str` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `kind` | `ClassVar[RefKind]` | `RefKind.SOURCE` |

<a id="api-slotref"></a>

## `SlotRef`

Import: `cruxible_client.authoring.sdk_types.SlotRef`. [Source](src/cruxible_client/authoring/sdk_types.py#L161)

| Field | Type | Default / construction |
|---|---|---|
| `address` | `str` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `kind` | `ClassVar[RefKind]` | `RefKind.SLOT` |

<a id="api-pendingsubjectref"></a>

## `PendingSubjectRef`

Import: `cruxible_client.authoring.sdk_types.PendingSubjectRef`. [Source](src/cruxible_client/authoring/sdk_types.py#L60)

| Field | Type | Default / construction |
|---|---|---|
| `address` | `str` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `kind` | `ClassVar[RefKind]` | `RefKind.SUBJECT` |

<a id="api-pendingclaimtyperef"></a>

## `PendingClaimTypeRef`

Import: `cruxible_client.authoring.sdk_types.PendingClaimTypeRef`. [Source](src/cruxible_client/authoring/sdk_types.py#L79)

| Field | Type | Default / construction |
|---|---|---|
| `address` | `str` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `kind` | `ClassVar[RefKind]` | `RefKind.CLAIM_TYPE` |
| `object_kind` | `str` | `Required` |

<a id="api-captureref"></a>

## `CaptureRef`

Import: `cruxible_client.authoring.sdk_types.CaptureRef`. [Source](src/cruxible_client/authoring/sdk_types.py#L111)

| Field | Type | Default / construction |
|---|---|---|
| `capture_digest` | `str` | `Required` |
| `contract_address` | `str` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `citation_role` | `Literal['evidence', 'copy', 'legacy']` | `Required` |

<a id="api-captureview"></a>

## `CaptureView`

Import: `cruxible_client.authoring.sdk_types.CaptureView`. [Source](src/cruxible_client/authoring/sdk_types.py#L121)

| Field | Type | Default / construction |
|---|---|---|
| `result` | `CaptureReadV1` | `Required` |

<a id="api-captureview-ref"></a>

### `CaptureView.ref`

[Source](src/cruxible_client/authoring/sdk_types.py#L127)

```text
ref: CaptureRef
```

<a id="api-captureview-content"></a>

### `CaptureView.content`

[Source](src/cruxible_client/authoring/sdk_types.py#L142)

```text
content: bytes
```

<a id="api-captureview-text"></a>

### `CaptureView.text`

[Source](src/cruxible_client/authoring/sdk_types.py#L153)

```text
text(encoding: str='utf-8') -> str
```

<a id="api-captureview-json"></a>

### `CaptureView.json`

[Source](src/cruxible_client/authoring/sdk_types.py#L156)

```text
json() -> object
```

<a id="api-literalvalue"></a>

## `LiteralValue`

Import: `cruxible_client.authoring.sdk_types.LiteralValue`. [Source](src/cruxible_client/authoring/sdk_types.py#L168)

| Field | Type | Default / construction |
|---|---|---|
| `predicate` | `str` | `Required` |
| `value` | `CanonicalValue` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |

<a id="api-exactcontent"></a>

## `ExactContent`

Import: `cruxible_client.authoring.sdk_types.ExactContent`. [Source](src/cruxible_client/authoring/sdk_types.py#L183)

| Field | Type | Default / construction |
|---|---|---|
| `content` | `bytes` | `Required` |

<a id="api-duration"></a>

## `Duration`

Import: `cruxible_client.authoring.sdk_types.Duration`. [Source](src/cruxible_client/authoring/sdk_types.py#L214)

| Field | Type | Default / construction |
|---|---|---|
| `value` | `int` | `Required` |

<a id="api-duration-days"></a>

### `Duration.days`

[Source](src/cruxible_client/authoring/sdk_types.py#L222)

```text
days(*, count: int) -> Duration
```

<a id="api-duration-hours"></a>

### `Duration.hours`

[Source](src/cruxible_client/authoring/sdk_types.py#L226)

```text
hours(*, count: int) -> Duration
```

<a id="api-duration-microseconds"></a>

### `Duration.microseconds`

[Source](src/cruxible_client/authoring/sdk_types.py#L230)

```text
microseconds(*, count: int) -> Duration
```

<a id="api-duration-model-dump"></a>

### `Duration.model_dump`

[Source](src/cruxible_client/authoring/sdk_types.py#L239)

```text
model_dump() -> dict[str, object]
```

<a id="api-effectiveperiod"></a>

## `EffectivePeriod`

Import: `cruxible_client.authoring.sdk_types.EffectivePeriod`. [Source](src/cruxible_client/authoring/sdk_types.py#L287)

| Field | Type | Default / construction |
|---|---|---|
| `starts_at` | `datetime \| None` | `Required` |
| `ends_at` | `datetime \| None` | `Required` |

<a id="api-accessprofile"></a>

## `AccessProfile`

Import: `cruxible_client.authoring.sdk_types.AccessProfile`. [Source](src/cruxible_client/authoring/sdk_types.py#L305)

| Field | Type | Default / construction |
|---|---|---|
| `profile_id` | `str` | `Required` |
| `permitted_access_classes` | `tuple[str, ...]` | `Required` |
| `disclose_restricted_existence` | `bool` | `Required` |

<a id="api-accessprofile-model-dump"></a>

### `AccessProfile.model_dump`

[Source](src/cruxible_client/authoring/sdk_types.py#L316)

```text
model_dump() -> dict[str, object]
```

<a id="api-refkind"></a>

## `RefKind`

Import: `cruxible_client.authoring.sdk_types.RefKind`. [Source](src/cruxible_client/authoring/sdk_types.py#L23)

| Member | Value |
|---|---|
| `SUBJECT` | `'subject'` |
| `CLAIM_TYPE` | `'claim_type'` |
| `CLAIM` | `'claim'` |
| `PROCEDURE` | `'procedure'` |
| `QUERY` | `'query'` |
| `SOURCE` | `'source'` |
| `SLOT` | `'slot'` |

<a id="api-claimrole"></a>

## `ClaimRole`

Import: `cruxible_client.authoring.sdk_types.ClaimRole`. [Source](src/cruxible_client/authoring/sdk_types.py#L243)

| Member | Value |
|---|---|
| `NORMATIVE` | `'normative'` |
| `OBSERVATION` | `'observation'` |
| `ENVIRONMENT_BINDING` | `'environment_binding'` |
| `DERIVATION` | `'derivation'` |

<a id="api-disposition"></a>

## `Disposition`

Import: `cruxible_client.authoring.sdk_types.Disposition`. [Source](src/cruxible_client/authoring/sdk_types.py#L250)

| Member | Value |
|---|---|
| `NOT_TESTED` | `'not_tested'` |
| `SUPPORT` | `'support'` |
| `CONTRADICT` | `'contradict'` |
| `UNSURE` | `'unsure'` |

<a id="api-audience"></a>

## `Audience`

Import: `cruxible_client.authoring.sdk_types.Audience`. [Source](src/cruxible_client/authoring/sdk_types.py#L257)

| Member | Value |
|---|---|
| `AGENT` | `'agent'` |
| `HUMAN` | `'human'` |
| `BOTH` | `'both'` |

<a id="api-activationpolicy"></a>

## `ActivationPolicy`

Import: `cruxible_client.authoring.sdk_types.ActivationPolicy`. [Source](src/cruxible_client/authoring/sdk_types.py#L263)

| Member | Value |
|---|---|
| `DRAIN` | `'drain'` |
| `ABORT` | `'abort'` |
| `SNAPSHOT` | `'snapshot'` |
| `EPOCH_CHECK` | `'epoch-check'` |

<a id="api-claimobjectkind"></a>

## `ClaimObjectKind`

Import: `cruxible_client.authoring.sdk_types.ClaimObjectKind`. [Source](src/cruxible_client/authoring/sdk_types.py#L270)

| Member | Value |
|---|---|
| `LITERAL` | `'literal'` |
| `SUBJECT` | `'subject'` |
| `EXACT_CONTENT` | `'exact_content'` |

<a id="api-cardinality"></a>

## `Cardinality`

Import: `cruxible_client.authoring.sdk_types.Cardinality`. [Source](src/cruxible_client/authoring/sdk_types.py#L276)

| Member | Value |
|---|---|
| `ONE` | `'one'` |
| `MANY` | `'many'` |

<a id="api-referentsensitivity"></a>

## `ReferentSensitivity`

Import: `cruxible_client.authoring.sdk_types.ReferentSensitivity`. [Source](src/cruxible_client/authoring/sdk_types.py#L281)

| Member | Value |
|---|---|
| `IDENTITY` | `'identity'` |
| `SHELL` | `'shell'` |

<a id="api-diagnostic"></a>

## `Diagnostic`

Import: `cruxible_client.authoring.sdk_types.Diagnostic`. [Source](src/cruxible_client/authoring/sdk_types.py#L341)

| Field | Type | Default / construction |
|---|---|---|
| `code` | `str` | `Required` |
| `stage` | `str` | `Required` |
| `offending_element` | `str` | `Required` |
| `message` | `str` | `Required` |
| `repair` | `tuple[object, ...]` | `Required` |
| `owner` | `str \| None` | `Required` |
| `disposition` | `str \| None` | `Required` |
| `call_site` | `CallSite \| None` | `Required` |

<a id="api-callsite"></a>

## `CallSite`

Import: `cruxible_client.authoring.sdk_types.CallSite`. [Source](src/cruxible_client/authoring/sdk_types.py#L326)

| Field | Type | Default / construction |
|---|---|---|
| `logical_file` | `str` | `Required` |
| `line` | `int` | `Required` |
| `column` | `int \| None` | `Required` |
| `expression` | `str \| None` | `Required` |

<a id="api-sourcemapentry"></a>

## `SourceMapEntry`

Import: `cruxible_client.authoring.sdk_types.SourceMapEntry`. [Source](src/cruxible_client/authoring/sdk_types.py#L334)

| Field | Type | Default / construction |
|---|---|---|
| `builder_path` | `str` | `Required` |
| `emitted_paths` | `tuple[str, ...]` | `Required` |
| `call_site` | `CallSite` | `Required` |

<a id="api-derivationspec"></a>

## `DerivationSpec`

Import: `cruxible_client.authoring.sdk_types.DerivationSpec`. [Source](src/cruxible_client/authoring/sdk_types.py#L353)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |

## Source selection

`pb.file(path)` selects only cataloged local sources. FileSelector retains
the observed bytes; `.anchor(text)` requires exactly one occurrence and records
byte offsets. `.anchor_window(text=..., surrounding_lines=...)` extends that
unique selection by full lines. Missing, empty, or ambiguous anchors refuse.
Selectors do not silently reinterpret character indexes as byte indexes.
Selections overlapping a governed projection cannot be claimed as independent
evidence. `.observation()` produces the typed working observation for normal
authoring; it is not itself acceptance or evidence registration.

<a id="api-fileselector"></a>

## `FileSelector`

Import: `cruxible_client.authoring.selectors.FileSelector`. [Source](src/cruxible_client/authoring/selectors.py#L142)

| Field | Type | Default / construction |
|---|---|---|
| `path` | `Path` | `Required` |
| `source_id` | `str` | `Required` |
| `content` | `bytes` | `Required` |

<a id="api-fileselector-anchor"></a>

### `FileSelector.anchor`

[Source](src/cruxible_client/authoring/selectors.py#L147)

```text
anchor(text: str) -> EvidenceSelection
```

<a id="api-fileselector-anchor-window"></a>

### `FileSelector.anchor_window`

[Source](src/cruxible_client/authoring/selectors.py#L158)

```text
anchor_window(*, text: str, surrounding_lines: int) -> EvidenceSelection
```

<a id="api-evidenceselection"></a>

## `EvidenceSelection`

Import: `cruxible_client.authoring.selectors.EvidenceSelection`. [Source](src/cruxible_client/authoring/selectors.py#L113)

| Field | Type | Default / construction |
|---|---|---|
| `path` | `Path` | `Required` |
| `source_id` | `str` | `Required` |
| `content` | `bytes` | `Required` |
| `anchor_text` | `str` | `Required` |
| `start_byte` | `int` | `Required` |
| `end_byte` | `int` | `Required` |

<a id="api-evidenceselection-observation"></a>

### `EvidenceSelection.observation`

[Source](src/cruxible_client/authoring/selectors.py#L121)

```text
observation() -> WorkingSelectionObservationV1
```

<a id="api-workspacesources"></a>

## `WorkspaceSources`

Import: `cruxible_client.authoring.selectors.WorkspaceSources`. [Source](src/cruxible_client/authoring/selectors.py#L176)

<a id="api-workspacesources-document-entries"></a>

### `WorkspaceSources.document_entries`

[Source](src/cruxible_client/authoring/selectors.py#L208)

```text
document_entries: tuple[SourceCatalogEntry, ...]
```

<a id="api-workspacesources-procedure-projection-entries"></a>

### `WorkspaceSources.procedure_projection_entries`

[Source](src/cruxible_client/authoring/selectors.py#L214)

```text
procedure_projection_entries: tuple[ProcedureProjectionCatalogEntry, ...]
```

<a id="api-workspacesources-select"></a>

### `WorkspaceSources.select`

[Source](src/cruxible_client/authoring/selectors.py#L238)

```text
select(requested: str | Path) -> FileSelector
```

<a id="api-workspacesources-path-for-source"></a>

### `WorkspaceSources.path_for_source`

[Source](src/cruxible_client/authoring/selectors.py#L254)

```text
path_for_source(source_id: str) -> Path
```

<a id="api-workspacesources-path-for-procedure"></a>

### `WorkspaceSources.path_for_procedure`

[Source](src/cruxible_client/authoring/selectors.py#L262)

```text
path_for_procedure(procedure_identity: str) -> Path
```

## Procedure composition and execution

Import steps from `cruxible_client.authoring.procedures`. Sequence is an
immutable blueprint; `.bind(**providers)` returns a new one. Each step’s `name`
is its output alias and provider binding key; `node_id` optionally preserves a
different stable graph identity. Edges use node identities. Steps fall through
in order unless a forward `next`/Guard branch is supplied. Terminals have no
successor. Every consumed alias must exist on every path reaching its consumer.

| Step | Behavior | Special requirement |
|---|---|---|
| StateTap | Named accepted query read bound at admission | No dynamic query depending on a provider output later in the run |
| Source | Acquisition through selected provider/interface | Capture contract and admitted acquisition policy |
| Call | Contracted operation | Bound provider; applicable effect policy and authority |
| Transform | Closed deterministic transformation | Matching declared contracts and typed transform spec |
| Project | Explicit output construction | Fields satisfy output contract |
| Guard | Forward routing or refusal | on_false defaults to `$abort`; on_true defaults to continuation |
| EmitCapture | Register output evidence, end path | Served through accepted Lines |
| ProposeChangeSet | Submit governed candidate templates, end path | Served through Lines with mandate/authority; does not accept |
| Halt | End path without successful return value | No successor |

Contracts are accepted references or carried Contract definitions. Previous()
denotes the preceding value-producing output, or invocation input when first.
Output(alias, path) selects a particular earlier output. Source.request and
Call.input default to Previous(); Project.fields is explicit. Whole-output Call
auto-wiring compares carried schemas; field mappings/adapters are explicit.

Preview is structural only. Its ready_for_prepare does not verify runtime
values, installed deployment availability, effective authority, or all accepted
references. `build()` raises ProcedureCompositionError containing the preview
when diagnostics remain. Unbound providers are errors, not hidden defaults.

| Run surface | Availability |
|---|---|
| Direct graph v3 | StateTap, Transform, Project, Guard, Repeat, Halt |
| Direct graph v4 | Those kinds plus explicitly bound Source |
| Direct graph v5 | Those kinds plus Call |
| Accepted Line | Adds authorized EmitCapture and ProposeChangeSet paths |
| Not served by this SDK authoring API | PostInbox and MandateSettlement |

Bounded Repeat is available in shared ProcedureInput but has no Sequence step
class. Current Sequence has no general value-merge, nested invoke, recursion,
parallel execution, or Python-source compiler. StateTap binds at admission;
Source requests can depend on prior runtime outputs. Graph legality does not
promise availability in every run lane.

`Procedure.run` admits a new invocation. Read its retained status by run ID via
`ProcedureRun.refresh`; do not use fresh invocation as a generic historical
replay method. An admission refusal can have run_id=None. `result` is meaningful
under the returned status; detailed terminal fields are in the typed raw client
response. Measurement verdicts are distinct from execution success. A due
measurement can record a standing resolution/reading; pending/expired status
does not invent one. `readings` is read-only and carries its snapshot in cursors.

<a id="api-sequence"></a>

## `Sequence`

Import: `cruxible_client.authoring.procedures.Sequence`. [Source](src/cruxible_client/authoring/procedures.py#L273)

| Field | Type | Default / construction |
|---|---|---|
| `steps` | `tuple[Step, ...] \| list[Step]` | `Required` |
| `name` | `str` | `Required` |
| `contract_in` | `Contract` | `Required` |
| `contract_out` | `Contract` | `Required` |
| `budget` | `ProcedureBudgetV3` | `Required` |
| `hard_caps` | `ProcedureHardCapsV3` | `Required` |
| `returns` | `str \| None` | `None` |
| `terminal_capability` | `Literal[1, 2, 3]` | `1` |
| `activation_policy` | `Literal['drain', 'abort', 'snapshot', 'epoch-check']` | `'snapshot'` |
| `acquisition_policy` | `str \| None` | `None` |
| `description` | `str \| None` | `None` |

<a id="api-sequence-bind"></a>

### `Sequence.bind`

[Source](src/cruxible_client/authoring/procedures.py#L292)

```text
bind(**providers: ProviderBinding) -> Sequence
```

<a id="api-sequence-preview"></a>

### `Sequence.preview`

[Source](src/cruxible_client/authoring/procedures.py#L514)

```text
preview() -> ProcedurePreview
```

<a id="api-sequence-build"></a>

### `Sequence.build`

[Source](src/cruxible_client/authoring/procedures.py#L517)

```text
build() -> ProcedureInput
```

<a id="api-statetap"></a>

## `StateTap`

Import: `cruxible_client.authoring.procedures.StateTap`. [Source](src/cruxible_client/authoring/procedures.py#L118)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |
| `next` | `str \| None` | `None` |
| `node_id` | `str \| None` | `dataclass_field(default=None, kw_only=True)` |
| `query` | `str` | `''` |
| `parameters` | `object` | `None` |

<a id="api-source"></a>

## `Source`

Import: `cruxible_client.authoring.procedures.Source`. [Source](src/cruxible_client/authoring/procedures.py#L124)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |
| `next` | `str \| None` | `None` |
| `node_id` | `str \| None` | `dataclass_field(default=None, kw_only=True)` |
| `capture_contract` | `str` | `''` |
| `request` | `object` | `Previous()` |
| `provider` | `ProviderBinding \| None` | `None` |

<a id="api-call"></a>

## `Call`

Import: `cruxible_client.authoring.procedures.Call`. [Source](src/cruxible_client/authoring/procedures.py#L131)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |
| `next` | `str \| None` | `None` |
| `node_id` | `str \| None` | `dataclass_field(default=None, kw_only=True)` |
| `contract_in` | `Contract` | `Required` |
| `contract_out` | `Contract` | `Required` |
| `input` | `object` | `Previous()` |
| `provider` | `ProviderBinding \| None` | `None` |
| `effect_policy` | `str \| None` | `None` |

<a id="api-transform"></a>

## `Transform`

Import: `cruxible_client.authoring.procedures.Transform`. [Source](src/cruxible_client/authoring/procedures.py#L140)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |
| `next` | `str \| None` | `None` |
| `node_id` | `str \| None` | `dataclass_field(default=None, kw_only=True)` |
| `transform_kind` | `TransformKindV1` | `Required` |
| `contract_in` | `Contract` | `Required` |
| `contract_out` | `Contract` | `Required` |
| `spec` | `ProcedureTransformSpecV1` | `Required` |

<a id="api-project"></a>

## `Project`

Import: `cruxible_client.authoring.procedures.Project`. [Source](src/cruxible_client/authoring/procedures.py#L148)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |
| `next` | `str \| None` | `None` |
| `node_id` | `str \| None` | `dataclass_field(default=None, kw_only=True)` |
| `fields` | `object` | `Required` |
| `contract_out` | `Contract` | `Required` |

<a id="api-guard"></a>

## `Guard`

Import: `cruxible_client.authoring.procedures.Guard`. [Source](src/cruxible_client/authoring/procedures.py#L154)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |
| `next` | `str \| None` | `None` |
| `node_id` | `str \| None` | `dataclass_field(default=None, kw_only=True)` |
| `predicate` | `GuardPredicateV1` | `Required` |
| `on_true` | `str \| None` | `None` |
| `on_false` | `str` | `'$abort'` |
| `refusal_code` | `str` | `'guard_refused'` |
| `message` | `str` | `'Procedure guard refused.'` |

<a id="api-emitcapture"></a>

## `EmitCapture`

Import: `cruxible_client.authoring.procedures.EmitCapture`. [Source](src/cruxible_client/authoring/procedures.py#L163)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |
| `next` | `str \| None` | `None` |
| `node_id` | `str \| None` | `dataclass_field(default=None, kw_only=True)` |
| `capture_contract` | `str` | `''` |
| `input` | `object` | `Previous()` |

<a id="api-proposechangeset"></a>

## `ProposeChangeSet`

Import: `cruxible_client.authoring.procedures.ProposeChangeSet`. [Source](src/cruxible_client/authoring/procedures.py#L169)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |
| `next` | `str \| None` | `None` |
| `node_id` | `str \| None` | `dataclass_field(default=None, kw_only=True)` |
| `candidate_templates` | `tuple[object, ...]` | `Required` |

<a id="api-halt"></a>

## `Halt`

Import: `cruxible_client.authoring.procedures.Halt`. [Source](src/cruxible_client/authoring/procedures.py#L174)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |
| `next` | `str \| None` | `None` |
| `node_id` | `str \| None` | `dataclass_field(default=None, kw_only=True)` |
| `reason` | `str \| None` | `None` |

<a id="api-output"></a>

## `Output`

Import: `cruxible_client.authoring.procedures.Output`. [Source](src/cruxible_client/authoring/procedures.py#L47)

| Field | Type | Default / construction |
|---|---|---|
| `step` | `str` | `Required` |
| `path` | `str` | `''` |

<a id="api-previous"></a>

## `Previous`

Import: `cruxible_client.authoring.procedures.Previous`. [Source](src/cruxible_client/authoring/procedures.py#L55)

| Field | Type | Default / construction |
|---|---|---|
| `path` | `str` | `''` |

<a id="api-providerbinding"></a>

## `ProviderBinding`

Import: `cruxible_client.authoring.procedures.ProviderBinding`. [Source](src/cruxible_client/authoring/procedures.py#L61)

| Field | Type | Default / construction |
|---|---|---|
| `provider` | `str` | `Required` |
| `interface` | `str` | `Required` |
| `interface_digest` | `str` | `Required` |
| `implementation_digest` | `str` | `Required` |
| `effect_class` | `Literal['none', 'external_read', 'external_mutation'] \| None` | `None` |

<a id="api-providerbinding-from-interface"></a>

### `ProviderBinding.from_interface`

[Source](src/cruxible_client/authoring/procedures.py#L83)

```text
from_interface(entry: PlaybillProviderInterfaceEntry, *, provider: str | None=None) -> ProviderBinding
```

<a id="api-procedurepreview"></a>

## `ProcedurePreview`

Import: `cruxible_client.authoring.procedures.ProcedurePreview`. [Source](src/cruxible_client/authoring/procedures.py#L197)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |
| `ready_for_prepare` | `bool` | `Required` |
| `contracts` | `tuple[CarriedContractInput, ...]` | `Required` |
| `contract_in` | `dict[str, Any]` | `Required` |
| `contract_out` | `dict[str, Any]` | `Required` |
| `terminal_capability` | `Literal[1, 2, 3]` | `Required` |
| `acquisition_policy` | `str \| None` | `Required` |
| `nodes` | `tuple[dict[str, Any], ...]` | `Required` |
| `edges` | `dict[str, dict[str, str]]` | `Field(default_factory=dict)` |
| `providers` | `dict[str, ProviderBinding]` | `Field(default_factory=dict)` |
| `terminals` | `tuple[str, ...]` | `()` |
| `returns` | `str` | `Required` |
| `budget` | `ProcedureBudgetV3` | `Required` |
| `hard_caps` | `ProcedureHardCapsV3` | `Required` |
| `errors` | `tuple[CompositionDiagnostic, ...]` | `()` |
| `pending_checks` | `tuple[str, ...]` | `('Resolve accepted references at the intent base and verify provider interfaces.', 'Validate runtime values against contracts; preview does not execute any path.', 'Check authority, installation availability and effective policy budgets at admission.')` |

<a id="api-compositiondiagnostic"></a>

## `CompositionDiagnostic`

Import: `cruxible_client.authoring.procedures.CompositionDiagnostic`. [Source](src/cruxible_client/authoring/procedures.py#L191)

| Field | Type | Default / construction |
|---|---|---|
| `step` | `str \| None` | `None` |
| `code` | `str` | `Required` |
| `message` | `str` | `Required` |

<a id="api-procedure"></a>

## `Procedure`

Import: `cruxible_client.authoring.sdk.Procedure`. [Source](src/cruxible_client/authoring/sdk.py#L3413)

<a id="api-procedure-ref"></a>

### `Procedure.ref`

[Source](src/cruxible_client/authoring/sdk.py#L3422)

```text
ref: ProcedureRef
```

<a id="api-procedure-readiness"></a>

### `Procedure.readiness`

[Source](src/cruxible_client/authoring/sdk.py#L3426)

```text
readiness() -> api.PlaybillProcedureReadiness
```

<a id="api-procedure-bind"></a>

### `Procedure.bind`

[Source](src/cruxible_client/authoring/sdk.py#L3437)

```text
bind(*, bindings: Mapping[str | SlotRef, TypedRef]) -> api.PlaybillProcedureBindResult
```

<a id="api-procedure-run"></a>

### `Procedure.run`

[Source](src/cruxible_client/authoring/sdk.py#L3468)

```text
run(
    *,
    at: AcceptedCoordinate | None = None,
    resolution_contract: ResolutionContractReferenceV1 | None = None,
    trigger_event: TriggerEventReferenceV1 | None = None,
    **inputs: CanonicalValue,
) -> ProcedureRun
```

<a id="api-procedure-measure"></a>

### `Procedure.measure`

[Source](src/cruxible_client/authoring/sdk.py#L3490)

```text
measure(
    *,
    run: ProcedureRun | str | None = None,
    measurements: Sequence[str] = (),
    at: AcceptedCoordinate | None = None,
) -> MeasurementBatch
```

Evaluate this Procedure's due measurements, crediting `run` if given.

Pending measurements are reported, not evaluated; a standing resolution
is returned rather than re-derived; calling again with the same run
replays the same reading.

<a id="api-procedure-readings"></a>

### `Procedure.readings`

[Source](src/cruxible_client/authoring/sdk.py#L3525)

```text
readings(
    *,
    run: ProcedureRun | str | None = None,
    measurements: Sequence[str] = (),
    limit: int = 50,
    cursor: str | None = None,
    at: AcceptedCoordinate | None = None,
) -> api.PlaybillProcedureReadingsResultV1
```

Inspect measurement standing and retained readings. Never writes.

A page's `cursor` continues that page's selection: the observation
instant and coordinate the first page was answered at travel inside
it, so passing the cursor back with the same `run`/`measurements`
pages the same selection even though this call stamps a fresh clock.

<a id="api-procedurerun"></a>

## `ProcedureRun`

Import: `cruxible_client.authoring.sdk.ProcedureRun`. [Source](src/cruxible_client/authoring/sdk.py#L3562)

<a id="api-procedurerun-run-id"></a>

### `ProcedureRun.run_id`

[Source](src/cruxible_client/authoring/sdk.py#L3568)

```text
run_id: str | None
```

<a id="api-procedurerun-status"></a>

### `ProcedureRun.status`

[Source](src/cruxible_client/authoring/sdk.py#L3572)

```text
status: str
```

<a id="api-procedurerun-result"></a>

### `ProcedureRun.result`

[Source](src/cruxible_client/authoring/sdk.py#L3576)

```text
result: CanonicalValue
```

<a id="api-procedurerun-receipt"></a>

### `ProcedureRun.receipt`

[Source](src/cruxible_client/authoring/sdk.py#L3580)

```text
receipt: str | None
```

<a id="api-procedurerun-coordinate"></a>

### `ProcedureRun.coordinate`

[Source](src/cruxible_client/authoring/sdk.py#L3584)

```text
coordinate: AcceptedCoordinate
```

<a id="api-procedurerun-track-record"></a>

### `ProcedureRun.track_record`

[Source](src/cruxible_client/authoring/sdk.py#L3588)

```text
track_record: object
```

<a id="api-procedurerun-refresh"></a>

### `ProcedureRun.refresh`

[Source](src/cruxible_client/authoring/sdk.py#L3606)

```text
refresh() -> ProcedureRun
```

<a id="api-procedurerun-measure"></a>

### `ProcedureRun.measure`

[Source](src/cruxible_client/authoring/sdk.py#L3614)

```text
measure(*, measurements: Sequence[str]=()) -> MeasurementBatch
```

Credit this run's exact grain with every due measurement's standing answer.

## Projections and workspace

Access ProjectionBlocks through `pb.block`. Repin reads accepted backing
state and updates local declarations/manifests; it replaces prose only when
explicit body bytes are supplied. For claims/queries/artifacts, None preserves
the backing class and an empty sequence removes it. Query-only and artifact
backings are valid. `currency_policy` distinguishes `warn` from `require_current`.
Compact markers are the default; their local manifests are part of the view.

Sync checks declared block backings and reports drift. It does not regenerate
the author’s prose or silently repin. `check=True` raises when the configured
currency policy makes drift a failure; `detach` is an explicit local mutation.

ProjectionPackage carries page bytes and every referenced exact manifest.
Construction/read/from_bytes validate the package and reject missing, unrelated,
corrupt, or out-of-root material. `.install` writes local derived files; it is
not governed retention. `.retention_value()` returns ExactContent to author in
a normal reviewed Claim if ledger retention is desired. Package construction
and installation do not grant evidence independence to a projection.

<a id="api-projectionblocks"></a>

## `ProjectionBlocks`

Import: `cruxible_client.authoring.sdk.ProjectionBlocks`. [Source](src/cruxible_client/authoring/sdk.py#L3269)

<a id="api-projectionblocks-repin"></a>

### `ProjectionBlocks.repin`

[Source](src/cruxible_client/authoring/sdk.py#L3273)

```text
repin(
    source: str | SourceRef,
    block_id: str,
    *,
    claims: Sequence[str | ClaimRef] | None = None,
    queries: Sequence[str | QueryRef | tuple[str | QueryRef, Mapping[str, CanonicalValue]]] | None = None,
    artifacts: Sequence[ArtifactIdentity] | None = None,
    currency_policy: ProjectionCurrencyPolicy | None = None,
    backing_digest: str | None = None,
    evaluation_time: datetime,
    body: str | bytes | None = None,
    compact: bool = True,
) -> ProjectionBlockStampV2
```

Refresh backing pins and optionally replace this block's authored body.

Compact markers are the default: digest references with local manifests. Subsequent
repins preserve that format. `package()` exports the complete view for
transfer or an explicit governed archival Claim.

<a id="api-projectionblocks-package"></a>

### `ProjectionBlocks.package`

[Source](src/cruxible_client/authoring/sdk.py#L3328)

```text
package(source: str | SourceRef) -> ProjectionPackage
```

Export the page and exact manifests for transfer or governed retention.

<a id="api-projectionblocks-sync"></a>

### `ProjectionBlocks.sync`

[Source](src/cruxible_client/authoring/sdk.py#L3338)

```text
sync(
    *paths: str | Path,
    all: bool = False,
    check: bool = False,
    detach: Sequence[str | Path] = (),
) -> api.PlaybillBlockSyncResultV1
```

Check every block; policy controls whether drift fails the check.

<a id="api-projectionpackage"></a>

## `ProjectionPackage`

Import: `cruxible_client.authoring.projection_package.ProjectionPackage`. [Source](src/cruxible_client/authoring/projection_package.py#L89)

| Field | Type | Default / construction |
|---|---|---|
| `content` | `bytes` | `Required` |
| `manifests` | `Mapping[str, bytes]` | `Required` |

<a id="api-projectionpackage-read"></a>

### `ProjectionPackage.read`

[Source](src/cruxible_client/authoring/projection_package.py#L110)

```text
read(workspace: str | Path, path: str | Path) -> ProjectionPackage
```

<a id="api-projectionpackage-to-bytes"></a>

### `ProjectionPackage.to_bytes`

[Source](src/cruxible_client/authoring/projection_package.py#L118)

```text
to_bytes() -> bytes
```

<a id="api-projectionpackage-from-bytes"></a>

### `ProjectionPackage.from_bytes`

[Source](src/cruxible_client/authoring/projection_package.py#L133)

```text
from_bytes(content: bytes) -> ProjectionPackage
```

<a id="api-projectionpackage-retention-value"></a>

### `ProjectionPackage.retention_value`

[Source](src/cruxible_client/authoring/projection_package.py#L156)

```text
retention_value() -> ExactContent
```

Stage this in a reviewed exact-content Claim to obtain ledger retention.

<a id="api-projectionpackage-install"></a>

### `ProjectionPackage.install`

[Source](src/cruxible_client/authoring/projection_package.py#L160)

```text
install(workspace: str | Path, path: str | Path) -> Path
```

## Signing capabilities

ApprovalSigner and ClaimAttestationV2Signer are separate protocols.
Approval signs a reviewed candidate; Claim attestation signs a statement about
an exact Claim version. Local signers check key identity/custody and reread the
key for signing without sending private bytes to the daemon. An agent supplies
an operator-provisioned signer; connecting does not mint signing authority.
`LocalEd25519ClaimAttestationSigner.open` also needs signing_key_id and enforces
local directory/file permissions. An invalid or changed key refuses.

`prepare_claim_attestation` signs after resolving the exact prepared statement;
`append_prepared_claim_attestation` also submits it. The explicit environment
helper uses CRUXIBLE_PRINCIPAL_KEY_PATH and resolves the authenticated actor;
this is not a search through arbitrary private-key locations.

<a id="api-approvalsigner"></a>

## `ApprovalSigner`

Import: `cruxible_client.authoring.signing.ApprovalSigner`. [Source](src/cruxible_client/authoring/signing.py#L45)

<a id="api-approvalsigner-signer-id"></a>

### `ApprovalSigner.signer_id`

[Source](src/cruxible_client/authoring/signing.py#L49)

```text
signer_id: str
```

<a id="api-approvalsigner-public-key"></a>

### `ApprovalSigner.public_key`

[Source](src/cruxible_client/authoring/signing.py#L52)

```text
public_key: str
```

<a id="api-approvalsigner-sign"></a>

### `ApprovalSigner.sign`

[Source](src/cruxible_client/authoring/signing.py#L54)

```text
sign(statement: ApprovalStatement) -> ApprovalAttestation
```

<a id="api-localed25519approvalsigner"></a>

## `LocalEd25519ApprovalSigner`

Import: `cruxible_client.authoring.signing.LocalEd25519ApprovalSigner`. [Source](src/cruxible_client/authoring/signing.py#L58)

| Field | Type | Default / construction |
|---|---|---|
| `signer_id` | `str` | `Required` |
| `private_key_path` | `Path` | `Required` |
| `public_key` | `str` | `Required` |

<a id="api-localed25519approvalsigner-open"></a>

### `LocalEd25519ApprovalSigner.open`

[Source](src/cruxible_client/authoring/signing.py#L66)

```text
open(
    *,
    signer_id: str,
    private_key_path: Path,
    expected_public_key: str,
    forbidden_roots: Sequence[Path],
) -> LocalEd25519ApprovalSigner
```

Validate custody and key identity without retaining private bytes.

<a id="api-localed25519approvalsigner-sign"></a>

### `LocalEd25519ApprovalSigner.sign`

[Source](src/cruxible_client/authoring/signing.py#L89)

```text
sign(statement: ApprovalStatement) -> ApprovalAttestation
```

<a id="api-claimattestationv2signer"></a>

## `ClaimAttestationV2Signer`

Import: `cruxible_client.authoring.attestations.ClaimAttestationV2Signer`. [Source](src/cruxible_client/authoring/attestations.py#L46)

<a id="api-claimattestationv2signer-signer"></a>

### `ClaimAttestationV2Signer.signer`

[Source](src/cruxible_client/authoring/attestations.py#L48)

```text
signer: str
```

<a id="api-claimattestationv2signer-signing-key-id"></a>

### `ClaimAttestationV2Signer.signing_key_id`

[Source](src/cruxible_client/authoring/attestations.py#L51)

```text
signing_key_id: str
```

<a id="api-claimattestationv2signer-sign-claim-attestation-v2"></a>

### `ClaimAttestationV2Signer.sign_claim_attestation_v2`

[Source](src/cruxible_client/authoring/attestations.py#L53)

```text
sign_claim_attestation_v2(statement: ClaimAttestationStatementV2) -> ClaimAttestationV2
```

<a id="api-localed25519claimattestationsigner"></a>

## `LocalEd25519ClaimAttestationSigner`

Import: `cruxible_client.authoring.attestations.LocalEd25519ClaimAttestationSigner`. [Source](src/cruxible_client/authoring/attestations.py#L113)

| Field | Type | Default / construction |
|---|---|---|
| `signer` | `str` | `Required` |
| `signing_key_id` | `str` | `Required` |
| `private_key_path` | `Path` | `Required` |
| `public_key` | `str` | `Required` |

<a id="api-localed25519claimattestationsigner-open"></a>

### `LocalEd25519ClaimAttestationSigner.open`

[Source](src/cruxible_client/authoring/attestations.py#L120)

```text
open(
    *,
    signer: str,
    signing_key_id: str,
    private_key_path: Path,
    expected_public_key: str,
    forbidden_roots: Sequence[Path],
) -> 'LocalEd25519ClaimAttestationSigner'
```

<a id="api-localed25519claimattestationsigner-sign-claim-attestation-v2"></a>

### `LocalEd25519ClaimAttestationSigner.sign_claim_attestation_v2`

[Source](src/cruxible_client/authoring/attestations.py#L143)

```text
sign_claim_attestation_v2(statement: ClaimAttestationStatementV2) -> ClaimAttestationV2
```

## Exported contract constructors

These models are directly exported from `cruxible_client`. Construct them with
keyword fields or validate mappings through `model_validate(...)`; serialize
with `model_dump(...)` / `model_dump_json(...)`, and inspect the full validation
schema with `model_json_schema()`. Validation occurs at construction, independently
of later authoring/admission checks. Constructing a model never accepts an artifact.

Fields/defaults below come from the current model definitions. Nested discriminator
unions and historical graph grammars remain defined by their linked schemas.
A `Field(...)` without a default is required even when it also declares bounds.


<a id="api-artifactidentity"></a>

### `ArtifactIdentity`

Import: `cruxible_client.ArtifactIdentity`. [Source](src/cruxible_client/contracts/artifacts.py#L30)

Kind and name must already be NFC-normalized and canonical. `qualified` returns `kind:name`; it performs no lookup.

| Field | Type | Default / validation |
|---|---|---|
| `kind` | `str` | `Required` |
| `name` | `str` | `Required` |

<a id="api-artifactlifecycle"></a>

### `ArtifactLifecycle`

Import: `cruxible_client.ArtifactLifecycle`. [Source](src/cruxible_client/contracts/artifacts.py#L86)

An optional predecessor is a tagged artifact digest. Lifecycle metadata does not itself retire an artifact; it is admitted with the governed definition.

| Field | Type | Default / validation |
|---|---|---|
| `state` | `Literal['live', 'retired']` | `'live'` |
| `predecessor_digest` | `str \| None` | `None` |

<a id="api-artifactpin"></a>

### `ArtifactPin`

Import: `cruxible_client.ArtifactPin`. [Source](src/cruxible_client/contracts/artifacts.py#L66)

`role` must be canonical. `artifact_digest` is a tagged artifact digest; the identity alone does not pin a version.

| Field | Type | Default / validation |
|---|---|---|
| `role` | `str` | `Required` |
| `target` | `ArtifactIdentity` | `Required` |
| `artifact_digest` | `str` | `Required` |

<a id="api-claimadmissionpolicyv1"></a>

### `ClaimAdmissionPolicyV1`

Import: `cruxible_client.ClaimAdmissionPolicyV1`. [Source](src/cruxible_client/contracts/policies.py#L137)

Requirement groups must be sorted and unique by requirement ID, with IDs unique across kinds. Unknown freeze transition exceptions are refused.

| Field | Type | Default / validation |
|---|---|---|
| `tag` | `Literal['playbill-claim-admission-policy-v1']` | `'playbill-claim-admission-policy-v1'` |
| `corroboration_requirements` | `tuple[CorroborationRequirementV1, ...]` | `()` |
| `freeze_requirements` | `tuple[FreezeRequirementV1, ...]` | `()` |

<a id="api-claimresolutionpolicyv1"></a>

### `ClaimResolutionPolicyV1`

Import: `cruxible_client.ClaimResolutionPolicyV1`. [Source](src/cruxible_client/contracts/policies.py#L162)

Eligible verdicts are nonempty, sorted, and unique; basis kinds are sorted and unique. Many-cardinality requires `selector="all"`; one-cardinality cannot select all contenders.

| Field | Type | Default / validation |
|---|---|---|
| `tag` | `Literal['playbill-claim-resolution-policy-v1']` | `'playbill-claim-resolution-policy-v1'` |
| `cardinality` | `ClaimCardinality` | `Required` |
| `eligible_verdicts` | `tuple[ClaimVerdict, ...]` | `Required` |
| `required_basis_kinds` | `tuple[str, ...]` | `()` |
| `require_current` | `bool` | `True` |
| `selector` | `Literal['all', 'only_contender']` | `Required` |
| `conflict_result` | `Literal['unresolved', 'refuse']` | `'unresolved'` |

<a id="api-canonicaldurationv1"></a>

### `CanonicalDurationV1`

Import: `cruxible_client.CanonicalDurationV1`. [Source](src/cruxible_client/contracts/captures.py#L141)

Duration is represented in integer microseconds, not floating-point seconds. Procedure budgets/caps additionally require their wall-clock duration to be nonzero.

| Field | Type | Default / validation |
|---|---|---|
| `tag` | `Literal['playbill-duration-v1']` | `'playbill-duration-v1'` |
| `microseconds` | `int` | `Field(ge=0)` |

<a id="api-contractschema"></a>

### `ContractSchema`

Import: `cruxible_client.ContractSchema`. [Source](src/cruxible_client/contracts/procedures/contract_schema.py#L96)

Each field must explicitly supply its type, even though `PropertySchema` alone has a type default. Extra fields are disallowed by default.

| Field | Type | Default / validation |
|---|---|---|
| `description` | `str \| None` | `None` |
| `fields` | `dict[str, PropertySchema]` | `Required` |
| `allow_extra` | `bool` | `False` |

<a id="api-procedurebudgetv3"></a>

### `ProcedureBudgetV3`

Import: `cruxible_client.ProcedureBudgetV3`. [Source](src/cruxible_client/contracts/procedures/models.py#L87)

Wall-clock budget must be nonzero. Omitted optional result/item limits do not disable effective daemon policy. Bounds in the field declarations remain enforced.

| Field | Type | Default / validation |
|---|---|---|
| `tag` | `Literal['playbill-procedure-budget-v1']` | `'playbill-procedure-budget-v1'` |
| `wall_clock` | `CanonicalDurationV1` | `Required` |
| `max_provider_calls` | `int` | `Field(ge=0, le=1000000)` |
| `max_capture_bytes` | `int` | `Field(ge=0, le=2 ** 63 - 1)` |
| `max_result_bytes` | `int \| None` | `Field(default=None, ge=1, exclude_if=lambda v: v is None)` |
| `max_items` | `int \| None` | `Field(default=None, ge=1, le=2 ** 31 - 1, exclude_if=lambda value: value is None)` |

<a id="api-proceduredefinitionv3"></a>

### `ProcedureDefinitionV3`

Import: `cruxible_client.ProcedureDefinitionV3`. [Source](src/cruxible_client/contracts/procedures/models.py#L781)

This root export is the frozen graph-v3 model, not an alias for the latest graph version. The current Sequence builder emits graph v5. Use the corresponding versioned contract when authoring a raw definition.

| Field | Type | Default / validation |
|---|---|---|
| `graph_format` | `Literal[3]` | `3` |
| `name` | `str` | `Required` |
| `description` | `str \| None` | `None` |
| `contract_in` | `ProcedurePinBindingV1` | `Required` |
| `contract_out` | `ProcedurePinBindingV1` | `Required` |
| `parameter_contract` | `ProcedurePinBindingV1 \| None` | `None` |
| `nodes` | `tuple[ProcedureNodeV3, ...]` | `Required` |
| `returns` | `str` | `Required` |
| `pin_slots` | `tuple[ProcedurePinSlotV1, ...]` | `()` |
| `measurements` | `tuple[ProcedureMeasurementDeclarationV1, ...]` | `()` |
| `budget` | `ProcedureBudgetV3` | `Required` |
| `hard_caps` | `ProcedureHardCapsV3` | `Required` |
| `terminal_capability` | `Literal[1, 2, 3]` | `Required` |
| `annotations` | `object` | `Field(default_factory=dict)` |

<a id="api-procedurehardcapsv3"></a>

### `ProcedureHardCapsV3`

Import: `cruxible_client.ProcedureHardCapsV3`. [Source](src/cruxible_client/contracts/procedures/models.py#L107)

Maximum wall-clock duration must be nonzero. Caps constrain declared/effective execution; their values do not grant execution authority.

| Field | Type | Default / validation |
|---|---|---|
| `tag` | `Literal['playbill-procedure-hard-caps-v1']` | `'playbill-procedure-hard-caps-v1'` |
| `max_wall_clock` | `CanonicalDurationV1` | `Required` |
| `max_provider_calls` | `int` | `Field(ge=0, le=1000000)` |
| `max_capture_bytes` | `int` | `Field(ge=0, le=2 ** 63 - 1)` |
| `max_result_bytes` | `int \| None` | `Field(default=None, ge=1, exclude_if=lambda v: v is None)` |
| `max_items` | `int` | `Field(ge=1, le=2 ** 31 - 1)` |
| `max_repeat_attempts` | `int` | `Field(ge=1, le=2 ** 31 - 1)` |

<a id="api-procedureownedcontractv1"></a>

### `ProcedureOwnedContractV1`

Import: `cruxible_client.ProcedureOwnedContractV1`. [Source](src/cruxible_client/contracts/procedures/artifacts.py#L138)

An owner-carried input/output contract is part of the governed Procedure envelope; constructing it does not publish a standalone global contract.

| Field | Type | Default / validation |
|---|---|---|
| `tag` | `Literal['playbill-procedure-owned-contract-v1']` | `'playbill-procedure-owned-contract-v1'` |
| `identity` | `ArtifactIdentity` | `Required` |
| `contract_schema` | `ContractSchema` | `Field(alias='schema')` |

<a id="api-procedurepinslotrefv1"></a>

### `ProcedurePinSlotRefV1`

Import: `cruxible_client.ProcedurePinSlotRefV1`. [Source](src/cruxible_client/contracts/procedures/models.py#L74)

References an explicitly declared Procedure slot. It is a definition-time placeholder, not an arbitrary runtime dispatch key.

| Field | Type | Default / validation |
|---|---|---|
| `tag` | `Literal['playbill-procedure-pin-slot-ref-v1']` | `'playbill-procedure-pin-slot-ref-v1'` |
| `slot_name` | `str` | `Required` |

<a id="api-procedurepinslotv1"></a>

### `ProcedurePinSlotV1`

Import: `cruxible_client.ProcedurePinSlotV1`. [Source](src/cruxible_client/contracts/procedures/models.py#L39)

Declares a slot under the frozen Procedure contract. `Procedure.bind(...)` uses the corresponding accepted binding rules.

| Field | Type | Default / validation |
|---|---|---|
| `tag` | `Literal['playbill-procedure-pin-slot-v1']` | `'playbill-procedure-pin-slot-v1'` |
| `slot_name` | `str` | `Required` |
| `pin_role` | `str` | `Required` |
| `artifact_kind` | `str` | `Required` |
| `interface_digest` | `str` | `Required` |

<a id="api-projectnodev3"></a>

### `ProjectNodeV3`

Import: `cruxible_client.ProjectNodeV3`. [Source](src/cruxible_client/contracts/procedures/models.py#L512)

Pure projection under the declared output contract. It does not invoke a provider or mutate accepted state.

| Field | Type | Default / validation |
|---|---|---|
| `kind` | `Literal['project']` | `'project'` |
| `node_id` | `str` | `Required` |
| `fields` | `object` | `Required` |
| `contract_out` | `ProcedurePinBindingV1` | `Required` |
| `as_` | `str` | `Field(alias='as')` |
| `next` | `str \| None` | `None` |

<a id="api-propertyschema"></a>

### `PropertySchema`

Import: `cruxible_client.PropertySchema`. [Source](src/cruxible_client/contracts/procedures/contract_schema.py#L31)

`required` aliases the inverse of `optional`; conflicting explicit values refuse. A primary key cannot be optional or a list. Lists require `item_fields`, which non-list fields forbid. `enum` and `enum_ref` are mutually exclusive; enum values must be nonempty, unique canonical values, and a supplied default must belong to the enum. `enum_ref` requires string type. `json_schema` requires JSON type and a canonical-serializable schema.

| Field | Type | Default / validation |
|---|---|---|
| `type` | `PropertyType` | `'string'` |
| `primary_key` | `bool` | `False` |
| `indexed` | `bool` | `False` |
| `optional` | `bool` | `False` |
| `required` | `bool \| None` | `Field(default=None, exclude=True)` |
| `default` | `Any \| None` | `None` |
| `enum` | `list[Any] \| None` | `None` |
| `enum_ref` | `str \| None` | `None` |
| `description` | `str \| None` | `None` |
| `json_schema` | `dict[str, Any] \| None` | `None` |
| `item_fields` | `dict[str, PropertySchema] \| None` | `Field(default=None, exclude_if=lambda value: value is None)` |

<a id="api-statetapnodev3"></a>

### `StateTapNodeV3`

Import: `cruxible_client.StateTapNodeV3`. [Source](src/cruxible_client/contracts/procedures/models.py#L240)

An admitted query selection. Its parameters cannot silently depend on a later provider output.

| Field | Type | Default / validation |
|---|---|---|
| `kind` | `Literal['state_tap']` | `'state_tap'` |
| `node_id` | `str` | `Required` |
| `query` | `ProcedurePinBindingV1` | `Required` |
| `parameters` | `object` | `Field(default_factory=dict)` |
| `as_` | `str` | `Field(alias='as')` |
| `next` | `str \| None` | `None` |

<a id="api-transformnodev3"></a>

### `TransformNodeV3`

Import: `cruxible_client.TransformNodeV3`. [Source](src/cruxible_client/contracts/procedures/models.py#L480)

A contracted built-in transformation, restricted to the transform kind/specification in the versioned graph contract.

| Field | Type | Default / validation |
|---|---|---|
| `kind` | `Literal['transform']` | `'transform'` |
| `node_id` | `str` | `Required` |
| `transform_kind` | `TransformKindV1` | `Required` |
| `contract_in` | `ProcedurePinBindingV1` | `Required` |
| `contract_out` | `ProcedurePinBindingV1` | `Required` |
| `spec` | `ProcedureTransformSpecV1` | `Required` |
| `as_` | `str` | `Field(alias='as')` |
| `next` | `str \| None` | `None` |

## Shared authoring inputs

`cruxible_client.authoring.inputs` re-exports
`cruxible_client.contracts.authoring.inputs`. These strict typed inputs are
the shared declarative authoring surface; they are not a second orchestration
API. Unknown fields refuse. Accepted references resolve at the intent base;
carried contracts belong to their Procedure; slots are intentionally deferred.
An exact accepted pin is not silently converted to a different version.

The field tables below are the declared constructor fields/defaults. Pydantic
Field(...) entries show actual constraints/factories, not values to copy into
payloads. Model validators impose additional cross-field rules: notably exactly
one exact-content text/base64 representation and the discriminated member kinds.
Use model validation rather than constructing an unvalidated dictionary.

<a id="api-literalobjectinput"></a>

## `LiteralObjectInput`

Import: `cruxible_client.contracts.authoring.inputs.LiteralObjectInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L74)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['literal']` | `Required` |
| `value` | `object` | `Required` |

<a id="api-subjectobjectinput"></a>

## `SubjectObjectInput`

Import: `cruxible_client.contracts.authoring.inputs.SubjectObjectInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L79)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['subject']` | `Required` |
| `subject` | `str` | `Required` |

<a id="api-exactcontentobjectinput"></a>

## `ExactContentObjectInput`

Import: `cruxible_client.contracts.authoring.inputs.ExactContentObjectInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L84)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['exact_content']` | `Required` |
| `text` | `str \| None` | `None` |
| `content_base64` | `str \| None` | `None` |

<a id="api-selfsourceinput"></a>

## `SelfSourceInput`

Import: `cruxible_client.contracts.authoring.inputs.SelfSourceInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L117)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['self_source']` | `Required` |
| `body` | `str` | `Required` |

<a id="api-workingselectioninput"></a>

## `WorkingSelectionInput`

Import: `cruxible_client.contracts.authoring.inputs.WorkingSelectionInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L122)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['working_selection']` | `Required` |
| `source_id` | `str` | `Required` |

<a id="api-existingcaptureinput"></a>

## `ExistingCaptureInput`

Import: `cruxible_client.contracts.authoring.inputs.ExistingCaptureInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L127)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['existing_capture']` | `Required` |
| `capture_digest` | `str` | `Required` |

<a id="api-acceptedreferenceinput"></a>

## `AcceptedReferenceInput`

Import: `cruxible_client.contracts.authoring.inputs.AcceptedReferenceInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L138)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['accepted']` | `Required` |
| `role` | `str` | `Required` |
| `target` | `str` | `Required` |

<a id="api-slotreferenceinput"></a>

## `SlotReferenceInput`

Import: `cruxible_client.contracts.authoring.inputs.SlotReferenceInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L144)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['slot']` | `Required` |
| `slot_name` | `str` | `Required` |

<a id="api-carriedcontractreferenceinput"></a>

## `CarriedContractReferenceInput`

Import: `cruxible_client.contracts.authoring.inputs.CarriedContractReferenceInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L149)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['carried_contract']` | `Required` |
| `name` | `str` | `Required` |
| `role` | `str` | `Required` |

<a id="api-carriedcontractinput"></a>

## `CarriedContractInput`

Import: `cruxible_client.contracts.authoring.inputs.CarriedContractInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L155)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |
| `description` | `str \| None` | `None` |
| `fields` | `dict[str, PropertySchema]` | `Required` |
| `allow_extra` | `bool` | `False` |

<a id="api-claimdispositioninput"></a>

## `ClaimDispositionInput`

Import: `cruxible_client.contracts.authoring.inputs.ClaimDispositionInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L162)

| Field | Type | Default / construction |
|---|---|---|
| `claim_id` | `str` | `Required` |
| `disposition` | `Literal['not_tested', 'support', 'contradict', 'unsure']` | `Required` |

<a id="api-claiminput"></a>

## `ClaimInput`

Import: `cruxible_client.contracts.authoring.inputs.ClaimInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L167)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['claim']` | `Required` |
| `subject` | `str` | `Required` |
| `predicate` | `str` | `Required` |
| `qualifier` | `str \| None` | `None` |
| `object` | `AuthoringObjectInput` | `Required` |
| `role` | `Literal['normative', 'observation', 'environment_binding', 'derivation']` | `Required` |
| `effective_from` | `datetime \| None` | `None` |
| `effective_until` | `datetime \| None` | `None` |
| `rationale` | `str` | `Required` |
| `source` | `AuthoringSourceInput` | `Required` |
| `citation_role` | `Literal['evidence', 'copy'] \| None` | `None` |
| `claim_id` | `str \| None` | `None` |
| `dispositions` | `tuple[ClaimDispositionInput, ...]` | `()` |

<a id="api-procedureinput"></a>

## `ProcedureInput`

Import: `cruxible_client.contracts.authoring.inputs.ProcedureInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L183)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['procedure']` | `Required` |
| `definition` | `dict[str, object]` | `Required` |
| `activation_policy` | `Literal['drain', 'abort', 'snapshot', 'epoch-check']` | `Required` |
| `retire` | `bool` | `False` |
| `contracts` | `tuple[CarriedContractInput, ...]` | `()` |
| `acquisition_policy` | `str \| None` | `None` |

<a id="api-subjectinput"></a>

## `SubjectInput`

Import: `cruxible_client.contracts.authoring.inputs.SubjectInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L202)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['subject']` | `Required` |
| `subject` | `SubjectShell` | `Required` |

<a id="api-querydefinitioninput"></a>

## `QueryDefinitionInput`

Import: `cruxible_client.contracts.authoring.inputs.QueryDefinitionInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L207)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['query_definition']` | `Required` |
| `query_definition` | `QueryDefinitionSpecV1 \| QueryDefinitionV1` | `Required` |

<a id="api-approvalpolicyinput"></a>

## `ApprovalPolicyInput`

Import: `cruxible_client.contracts.authoring.inputs.ApprovalPolicyInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L212)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['approval_policy']` | `Required` |
| `approval_policy` | `ApprovalPolicyV1` | `Required` |

<a id="api-procedureruntimepolicyinput"></a>

## `ProcedureRuntimePolicyInput`

Import: `cruxible_client.contracts.authoring.inputs.ProcedureRuntimePolicyInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L217)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['procedure_runtime_policy']` | `Required` |
| `procedure_runtime_policy` | `ProcedureRuntimePolicyV1` | `Required` |

<a id="api-claimtypeinput"></a>

## `ClaimTypeInput`

Import: `cruxible_client.contracts.authoring.inputs.ClaimTypeInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L222)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['claim_type']` | `Required` |
| `claim_type` | `ClaimType` | `Required` |

<a id="api-claimtypesuccessioninput"></a>

## `ClaimTypeSuccessionInput`

Import: `cruxible_client.contracts.authoring.inputs.ClaimTypeSuccessionInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L227)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['claim_type_succession']` | `Required` |
| `successor` | `ClaimType` | `Required` |
| `dependents` | `tuple[ClaimTypeSuccessionDependentV1, ...]` | `()` |

<a id="api-claimretirementinput"></a>

## `ClaimRetirementInput`

Import: `cruxible_client.contracts.authoring.inputs.ClaimRetirementInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L235)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['claim_retirement']` | `Required` |
| `claim_id` | `str` | `Required` |
| `reason` | `ClaimRetirementReason` | `Required` |
| `effective_until` | `datetime \| None` | `None` |
| `dependents` | `tuple[ClaimRetireDependentV1, ...]` | `()` |

<a id="api-proceduremandateinputv1"></a>

## `ProcedureMandateInputV1`

Import: `cruxible_client.contracts.authoring.inputs.ProcedureMandateInputV1`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L243)

| Field | Type | Default / construction |
|---|---|---|
| `tag` | `Literal['playbill-procedure-mandate-input-v1']` | `'playbill-procedure-mandate-input-v1'` |
| `kind` | `Literal['procedure_mandate']` | `Required` |
| `name` | `str` | `Required` |
| `procedure_name` | `str` | `Required` |
| `rung` | `Literal[2, 3]` | `Required` |
| `authority_ceiling` | `ProcedureHardCapsV3` | `Required` |
| `namespace` | `tuple[str, ...]` | `Required` |
| `valid_from` | `datetime` | `Required` |
| `expires_at` | `datetime` | `Required` |
| `retire` | `bool` | `False` |

<a id="api-changesetinput"></a>

## `ChangeSetInput`

Import: `cruxible_client.contracts.authoring.inputs.ChangeSetInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L271)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['change_set']` | `Required` |
| `members` | `tuple[AuthoringChangeSetMemberInputV1, ...]` | `Field(min_length=1)` |
| `rationale` | `str \| None` | `Field(default=None, max_length=CHANGE_SET_RATIONALE_MAX_LENGTH)` |

<a id="api-authoringinputerror"></a>

## `AuthoringInputError`

Import: `cruxible_client.contracts.authoring.inputs.AuthoringInputError`. [Source](src/cruxible_client/contracts/authoring/inputs.py#L300)

| Field | Type | Default / construction |
|---|---|---|
| `code` | `str` | `Required` |
| `field_path` | `str` | `Required` |
| `message` | `str` | `Required` |
| `repair` | `str` | `Required` |

## Lower-level HTTP client

`CruxibleClient` is synchronous, exported from `cruxible_client`. Construct
with exactly one of base_url or socket_path and an optional explicit bearer
token; unlike Playbill.connect, this constructor does not resolve workspace
context or perform the SDK compatibility/orientation workflow. Use a context
manager or close(). Public methods below retain explicit instance IDs and
typed request/response contracts. A method whose signature takes `request`
expects that model, not an invented SDK payload.

Each method’s complete signature is listed, with its HTTP operation when directly
declared in the client. GET reads are subject to visibility and snapshot rules;
POST can be a read, preflight, append, or governed mutation according to the
operation. HTTP verb alone does not indicate acceptance. Methods never turn a
returned proposal into an approval without a separate explicit operation.
Exceptions are decoded through ErrorResponse/response_to_error. Shape-invalid
responses can raise validation errors; ambiguous transport completion must be
checked before retrying a write.

```text
CruxibleClient(*, base_url: str | None=None, socket_path: str | None=None, token: str | None=None) -> None
```

<a id="api-cruxibleclient-close"></a>

### `CruxibleClient.close`

[Source](src/cruxible_client/transport/http.py#L209)

```text
close() -> None
```

<a id="api-cruxibleclient-enter"></a>

### `CruxibleClient.__enter__`

[Source](src/cruxible_client/transport/http.py#L212)

```text
__enter__() -> CruxibleClient
```

<a id="api-cruxibleclient-exit"></a>

### `CruxibleClient.__exit__`

[Source](src/cruxible_client/transport/http.py#L215)

```text
__exit__(*_args: object) -> None
```

<a id="api-cruxibleclient-version"></a>

### `CruxibleClient.version`

[Source](src/cruxible_client/transport/http.py#L242)

```text
version() -> str
```

<a id="api-cruxibleclient-check-playbill-projection-blocks"></a>

### `CruxibleClient.check_playbill_projection_blocks`

[Source](src/cruxible_client/transport/http.py#L257)

```text
check_playbill_projection_blocks(
    instance_id: str,
    *,
    request: contracts.PlaybillProjectionCheckRequestV1,
) -> contracts.PlaybillProjectionCheckResultV1
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/projections/check'`.

<a id="api-cruxibleclient-read-playbill-block-sync-backing"></a>

### `CruxibleClient.read_playbill_block_sync_backing`

[Source](src/cruxible_client/transport/http.py#L269)

```text
read_playbill_block_sync_backing(
    instance_id: str,
    *,
    request: contracts.PlaybillBlockSyncReadRequestV1,
) -> contracts.PlaybillBlockSyncReadResultV1
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/projections/sync-backing'`.

<a id="api-cruxibleclient-server-info"></a>

### `CruxibleClient.server_info`

[Source](src/cruxible_client/transport/http.py#L281)

```text
server_info() -> contracts.ServerInfoResult
```

HTTP: `GET '/api/v1/server/info'`.

<a id="api-cruxibleclient-server-restart"></a>

### `CruxibleClient.server_restart`

[Source](src/cruxible_client/transport/http.py#L285)

```text
server_restart() -> contracts.ServerRestartResult
```

HTTP: `POST '/api/v1/server/restart'`.

<a id="api-cruxibleclient-server-stop"></a>

### `CruxibleClient.server_stop`

[Source](src/cruxible_client/transport/http.py#L289)

```text
server_stop() -> contracts.ServerStopResult
```

HTTP: `POST '/api/v1/server/stop'`.

<a id="api-cruxibleclient-create-playbill-host"></a>

### `CruxibleClient.create_playbill_host`

[Source](src/cruxible_client/transport/http.py#L293)

```text
create_playbill_host(
    *,
    instance_id: str | None = None,
    workspace_root: str | None = None,
) -> contracts.PlaybillHostResult
```

HTTP: `POST '/api/v1/runtime/instances'`.

<a id="api-cruxibleclient-declare-playbill-block"></a>

### `CruxibleClient.declare_playbill_block`

[Source](src/cruxible_client/transport/http.py#L308)

```text
declare_playbill_block(
    instance_id: str,
    stamp: Mapping[str, Any],
) -> contracts.PlaybillBlockDeclareResultV1
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/blocks/declare'`.

<a id="api-cruxibleclient-depublish-playbill-block"></a>

### `CruxibleClient.depublish_playbill_block`

[Source](src/cruxible_client/transport/http.py#L317)

```text
depublish_playbill_block(
    instance_id: str,
    source_id: str,
    block_id: str,
) -> contracts.PlaybillBlockDepublishResultV1
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/blocks/depublish'`.

<a id="api-cruxibleclient-playbill-host-workspace-detach"></a>

### `CruxibleClient.playbill_host_workspace_detach`

[Source](src/cruxible_client/transport/http.py#L326)

```text
playbill_host_workspace_detach(instance_id: str) -> contracts.PlaybillWorkspaceDetachResultV1
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/workspace-detach'`.

<a id="api-cruxibleclient-playbill-host-workspace-registration"></a>

### `CruxibleClient.playbill_host_workspace_registration`

[Source](src/cruxible_client/transport/http.py#L335)

```text
playbill_host_workspace_registration(instance_id: str) -> contracts.PlaybillHostWorkspaceRegistrationV1
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/workspace-registration'`.

<a id="api-cruxibleclient-show-playbill-host"></a>

### `CruxibleClient.show_playbill_host`

[Source](src/cruxible_client/transport/http.py#L341)

```text
show_playbill_host(instance_id: str) -> contracts.PlaybillHostInspectionV1
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/host'`.

<a id="api-cruxibleclient-claim-runtime-bootstrap"></a>

### `CruxibleClient.claim_runtime_bootstrap`

[Source](src/cruxible_client/transport/http.py#L345)

```text
claim_runtime_bootstrap(
    instance_id: str,
    bootstrap_secret: str,
) -> contracts.RuntimeCredentialBootstrapResult
```

HTTP: `POST f'/api/v1/{instance_id}/runtime/bootstrap/claim'`.

<a id="api-cruxibleclient-create-runtime-credential"></a>

### `CruxibleClient.create_runtime_credential`

[Source](src/cruxible_client/transport/http.py#L354)

```text
create_runtime_credential(
    instance_id: str,
    *,
    label: str,
    permission_mode: contracts.RuntimeCredentialPermissionMode = 'admin',
) -> contracts.RuntimeCredentialResult
```

HTTP: `POST f'/api/v1/{instance_id}/runtime/credentials'`.

<a id="api-cruxibleclient-list-runtime-credentials"></a>

### `CruxibleClient.list_runtime_credentials`

[Source](src/cruxible_client/transport/http.py#L367)

```text
list_runtime_credentials(instance_id: str) -> contracts.RuntimeCredentialListResult
```

HTTP: `GET f'/api/v1/{instance_id}/runtime/credentials'`.

<a id="api-cruxibleclient-revoke-runtime-credential"></a>

### `CruxibleClient.revoke_runtime_credential`

[Source](src/cruxible_client/transport/http.py#L371)

```text
revoke_runtime_credential(instance_id: str, credential_id: str) -> contracts.RuntimeCredentialResult
```

HTTP: `POST f'/api/v1/{instance_id}/runtime/credentials/{credential_id}/revoke'`.

<a id="api-cruxibleclient-rotate-runtime-credential"></a>

### `CruxibleClient.rotate_runtime_credential`

[Source](src/cruxible_client/transport/http.py#L379)

```text
rotate_runtime_credential(instance_id: str, credential_id: str) -> contracts.RuntimeCredentialResult
```

HTTP: `POST f'/api/v1/{instance_id}/runtime/credentials/{credential_id}/rotate'`.

<a id="api-cruxibleclient-init-playbill"></a>

### `CruxibleClient.init_playbill`

[Source](src/cruxible_client/transport/http.py#L399)

```text
init_playbill(
    instance_id: str,
    *,
    principals: Sequence[Mapping[str, Any]],
    operating_profile: Literal['local', 'cloud'] = 'local',
    require_independent_approval: bool = False,
    workspace_root: str | None = None,
    git_object_format: Literal['sha1', 'sha256'] | None = None,
    mirror_url: str | None = None,
) -> contracts.PlaybillInitResult
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/init'`.

<a id="api-cruxibleclient-store-playbill-body"></a>

### `CruxibleClient.store_playbill_body`

[Source](src/cruxible_client/transport/http.py#L427)

```text
store_playbill_body(instance_id: str, content: bytes) -> contracts.PlaybillCasObjectResult
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/bodies'`.

<a id="api-cruxibleclient-decommission-playbill-instance"></a>

### `CruxibleClient.decommission_playbill_instance`

[Source](src/cruxible_client/transport/http.py#L438)

```text
decommission_playbill_instance(
    instance_id: str,
    *,
    reason: str,
) -> contracts.PlaybillInstanceDecommissionResultV1
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/instance/decommission'`.

<a id="api-cruxibleclient-set-playbill-ledger-mirror"></a>

### `CruxibleClient.set_playbill_ledger_mirror`

[Source](src/cruxible_client/transport/http.py#L447)

```text
set_playbill_ledger_mirror(instance_id: str, *, url: str) -> contracts.PlaybillLedgerMirrorV1
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/ledger/mirror'`.

<a id="api-cruxibleclient-publish-playbill-ledger"></a>

### `CruxibleClient.publish_playbill_ledger`

[Source](src/cruxible_client/transport/http.py#L456)

```text
publish_playbill_ledger(instance_id: str, *, timeout: float=60.0) -> contracts.PlaybillLedgerMirrorV1
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/ledger/publish'`.

<a id="api-cruxibleclient-get-playbill-ledger-mirror"></a>

### `CruxibleClient.get_playbill_ledger_mirror`

[Source](src/cruxible_client/transport/http.py#L467)

```text
get_playbill_ledger_mirror(instance_id: str) -> contracts.PlaybillLedgerMirrorV1
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/ledger/mirror'`.

<a id="api-cruxibleclient-list-playbill-provider-packages"></a>

### `CruxibleClient.list_playbill_provider_packages`

[Source](src/cruxible_client/transport/http.py#L471)

```text
list_playbill_provider_packages(instance_id: str) -> PlaybillProviderCatalogV1
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/providers'`.

<a id="api-cruxibleclient-install-playbill-provider"></a>

### `CruxibleClient.install_playbill_provider`

[Source](src/cruxible_client/transport/http.py#L475)

```text
install_playbill_provider(
    instance_id: str,
    request: PlaybillProviderInstallRequestV1,
) -> PlaybillProviderInstallResultV1
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/providers/install'`.

<a id="api-cruxibleclient-propose-playbill-document"></a>

### `CruxibleClient.propose_playbill_document`

[Source](src/cruxible_client/transport/http.py#L487)

```text
propose_playbill_document(
    instance_id: str,
    *,
    shell: Mapping[str, Any],
    proposal_name: str,
    source_compilation_digest: str | None = None,
    base: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
) -> contracts.PlaybillProposalInspection
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/documents/proposals'`.

<a id="api-cruxibleclient-propose-playbill-compiler-upgrade"></a>

### `CruxibleClient.propose_playbill_compiler_upgrade`

[Source](src/cruxible_client/transport/http.py#L510)

```text
propose_playbill_compiler_upgrade(
    instance_id: str,
    *,
    target: CompilerCoordinate,
    base: AcceptedCoordinate | contracts.PlaybillAcceptedCoordinate,
    proposal_name: str,
) -> contracts.PlaybillProposalInspection
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/compiler/proposals'`.

<a id="api-cruxibleclient-propose-playbill-principal-change"></a>

### `CruxibleClient.propose_playbill_principal_change`

[Source](src/cruxible_client/transport/http.py#L528)

```text
propose_playbill_principal_change(
    instance_id: str,
    *,
    principal: Mapping[str, Any],
    proposal_name: str,
    base: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
) -> contracts.PlaybillProposalInspection
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/principals/proposals'`.

<a id="api-cruxibleclient-list-playbill-principals"></a>

### `CruxibleClient.list_playbill_principals`

[Source](src/cruxible_client/transport/http.py#L549)

```text
list_playbill_principals(instance_id: str) -> contracts.PlaybillPrincipalList
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/principals'`.

<a id="api-cruxibleclient-playbill-whoami"></a>

### `CruxibleClient.playbill_whoami`

[Source](src/cruxible_client/transport/http.py#L553)

```text
playbill_whoami(instance_id: str) -> contracts.PlaybillWhoAmI
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/whoami'`.

<a id="api-cruxibleclient-list-playbill-proposals"></a>

### `CruxibleClient.list_playbill_proposals`

[Source](src/cruxible_client/transport/http.py#L557)

```text
list_playbill_proposals(
    instance_id: str,
    *,
    status: Literal['open', 'settled', 'incomplete'] | None = None,
) -> contracts.PlaybillProposalList
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/proposals'`.

<a id="api-cruxibleclient-resolve-playbill-proposal-selector"></a>

### `CruxibleClient.resolve_playbill_proposal_selector`

[Source](src/cruxible_client/transport/http.py#L570)

```text
resolve_playbill_proposal_selector(
    instance_id: str,
    selector: str,
) -> contracts.PlaybillProposalSelectorResultV1
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/proposal-selector'`.

<a id="api-cruxibleclient-readmit-playbill-proposal"></a>

### `CruxibleClient.readmit_playbill_proposal`

[Source](src/cruxible_client/transport/http.py#L581)

```text
readmit_playbill_proposal(instance_id: str, proposal_id: str) -> contracts.PlaybillProposalReadmitResult
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/proposals/{proposal_id}/readmit'`.

<a id="api-cruxibleclient-withdraw-playbill-proposal"></a>

### `CruxibleClient.withdraw_playbill_proposal`

[Source](src/cruxible_client/transport/http.py#L592)

```text
withdraw_playbill_proposal(
    instance_id: str,
    proposal_id: str,
    *,
    reason: str,
) -> contracts.PlaybillProposalWithdrawResult
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/proposals/{proposal_id}/withdraw'`.

<a id="api-cruxibleclient-inspect-playbill-proposal"></a>

### `CruxibleClient.inspect_playbill_proposal`

[Source](src/cruxible_client/transport/http.py#L605)

```text
inspect_playbill_proposal(instance_id: str, proposal_id: str) -> contracts.PlaybillProposalInspection
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/proposals/{proposal_id}'`.

<a id="api-cruxibleclient-inspect-playbill-refusal"></a>

### `CruxibleClient.inspect_playbill_refusal`

[Source](src/cruxible_client/transport/http.py#L611)

```text
inspect_playbill_refusal(instance_id: str, proposal_id: str) -> contracts.PlaybillRefusalInspection
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/proposals/{proposal_id}/refusal'`.

<a id="api-cruxibleclient-review-playbill-proposal"></a>

### `CruxibleClient.review_playbill_proposal`

[Source](src/cruxible_client/transport/http.py#L619)

```text
review_playbill_proposal(
    instance_id: str,
    proposal_id: str,
    *,
    include_body: bool = False,
    workspace_observation: Mapping[str, Any] | None = None,
) -> contracts.PlaybillProposalReview
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/proposals/{proposal_id}/review'`.

<a id="api-cruxibleclient-prepare-playbill-approval"></a>

### `CruxibleClient.prepare_playbill_approval`

[Source](src/cruxible_client/transport/http.py#L638)

```text
prepare_playbill_approval(
    instance_id: str,
    proposal_id: str,
    *,
    signer_id: str,
    include_body: bool = False,
) -> contracts.PlaybillApprovalChallenge
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/proposals/{proposal_id}/approval-challenge'`.

<a id="api-cruxibleclient-submit-playbill-approval"></a>

### `CruxibleClient.submit_playbill_approval`

[Source](src/cruxible_client/transport/http.py#L652)

```text
submit_playbill_approval(
    instance_id: str,
    proposal_id: str,
    *,
    attestation: Mapping[str, Any],
) -> contracts.PlaybillApprovalReceipt
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/proposals/{proposal_id}/approvals'`.

<a id="api-cruxibleclient-approve-playbill-proposal"></a>

### `CruxibleClient.approve_playbill_proposal`

[Source](src/cruxible_client/transport/http.py#L665)

```text
approve_playbill_proposal(
    instance_id: str,
    proposal_id: str,
    *,
    signer_id: str,
    signer: Callable[[dict[str, Any]], Mapping[str, Any]],
    include_body: bool = False,
) -> contracts.PlaybillApprovalReceipt
```

<a id="api-cruxibleclient-activate-playbill-proposal"></a>

### `CruxibleClient.activate_playbill_proposal`

[Source](src/cruxible_client/transport/http.py#L686)

```text
activate_playbill_proposal(instance_id: str, proposal_id: str) -> contracts.PlaybillActivationReceipt
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/proposals/{proposal_id}/activate'`.

<a id="api-cruxibleclient-list-playbill-documents"></a>

### `CruxibleClient.list_playbill_documents`

[Source](src/cruxible_client/transport/http.py#L694)

```text
list_playbill_documents(
    instance_id: str,
    *,
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
) -> contracts.PlaybillDocumentList
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/documents'`.

<a id="api-cruxibleclient-get-playbill-document"></a>

### `CruxibleClient.get_playbill_document`

[Source](src/cruxible_client/transport/http.py#L706)

```text
get_playbill_document(
    instance_id: str,
    identity: str,
    *,
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
) -> contracts.PlaybillDocumentView
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/documents/{identity}'`.

<a id="api-cruxibleclient-read-playbill-capture"></a>

### `CruxibleClient.read_playbill_capture`

[Source](src/cruxible_client/transport/http.py#L719)

```text
read_playbill_capture(instance_id: str, request: CaptureReadRequestV1) -> CaptureReadV1
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/captures/read'`.

<a id="api-cruxibleclient-dereference-playbill-document"></a>

### `CruxibleClient.dereference_playbill_document`

[Source](src/cruxible_client/transport/http.py#L728)

```text
dereference_playbill_document(
    instance_id: str,
    identity: str,
    *,
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
) -> contracts.PlaybillBodyRead
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/documents/{identity}/body'`.

<a id="api-cruxibleclient-playbill-document-history"></a>

### `CruxibleClient.playbill_document_history`

[Source](src/cruxible_client/transport/http.py#L741)

```text
playbill_document_history(instance_id: str, identity: str) -> contracts.PlaybillDocumentHistory
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/documents/{identity}/history'`.

<a id="api-cruxibleclient-explain-playbill-subject"></a>

### `CruxibleClient.explain_playbill_subject`

[Source](src/cruxible_client/transport/http.py#L747)

```text
explain_playbill_subject(
    instance_id: str,
    *,
    subject: Mapping[str, Any],
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any],
    detail: Literal['summary', 'evidence', 'proof'] = 'summary',
    include_body: bool = False,
) -> contracts.PlaybillExplainResult | contracts.PlaybillExplainUnsupportedDetail
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/explain'`.

<a id="api-cruxibleclient-playbill-source-context"></a>

### `CruxibleClient.playbill_source_context`

[Source](src/cruxible_client/transport/http.py#L771)

```text
playbill_source_context(instance_id: str) -> contracts.PlaybillSourceContext
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/sources/context'`.

<a id="api-cruxibleclient-check-playbill-source-bundle"></a>

### `CruxibleClient.check_playbill_source_bundle`

[Source](src/cruxible_client/transport/http.py#L775)

```text
check_playbill_source_bundle(
    instance_id: str,
    *,
    bundle: Mapping[str, Any],
) -> contracts.PlaybillSourceCheckResult
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/sources/check'`.

<a id="api-cruxibleclient-propose-playbill-source-bundle"></a>

### `CruxibleClient.propose_playbill_source_bundle`

[Source](src/cruxible_client/transport/http.py#L784)

```text
propose_playbill_source_bundle(
    instance_id: str,
    *,
    bundle: Mapping[str, Any],
    source_name: str,
    proposal_name: str,
) -> contracts.PlaybillProposalInspection
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/sources/proposals'`.

<a id="api-cruxibleclient-propose-playbill-subject"></a>

### `CruxibleClient.propose_playbill_subject`

[Source](src/cruxible_client/transport/http.py#L823)

```text
propose_playbill_subject(
    instance_id: str,
    *,
    shell: Mapping[str, Any],
    proposal_name: str,
    base: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
) -> contracts.PlaybillProposalInspection
```

<a id="api-cruxibleclient-list-playbill-subjects"></a>

### `CruxibleClient.list_playbill_subjects`

[Source](src/cruxible_client/transport/http.py#L836)

```text
list_playbill_subjects(
    instance_id: str,
    *,
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
) -> contracts.PlaybillSubjectList
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/subjects'`.

<a id="api-cruxibleclient-get-playbill-subject"></a>

### `CruxibleClient.get_playbill_subject`

[Source](src/cruxible_client/transport/http.py#L848)

```text
get_playbill_subject(
    instance_id: str,
    subject_kind: str,
    subject_id: str,
    *,
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
) -> contracts.PlaybillSubjectView
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/subjects/{subject_kind}/{subject_id}'`.

<a id="api-cruxibleclient-playbill-subject-history"></a>

### `CruxibleClient.playbill_subject_history`

[Source](src/cruxible_client/transport/http.py#L862)

```text
playbill_subject_history(
    instance_id: str,
    subject_kind: str,
    subject_id: str,
) -> contracts.PlaybillSubjectHistory
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/subjects/{subject_kind}/{subject_id}/history'`.

<a id="api-cruxibleclient-propose-playbill-claim-type"></a>

### `CruxibleClient.propose_playbill_claim_type`

[Source](src/cruxible_client/transport/http.py#L870)

```text
propose_playbill_claim_type(
    instance_id: str,
    *,
    claim_type: Mapping[str, Any],
    proposal_name: str,
    base: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
) -> contracts.PlaybillProposalInspection
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/claim-types/proposals'`.

<a id="api-cruxibleclient-propose-playbill-claim-type-input"></a>

### `CruxibleClient.propose_playbill_claim_type_input`

[Source](src/cruxible_client/transport/http.py#L886)

```text
propose_playbill_claim_type_input(
    instance_id: str,
    *,
    input: Mapping[str, Any],
    proposal_name: str,
) -> contracts.PlaybillClaimTypeInputProposalResult
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/claim-types/proposals'`.

<a id="api-cruxibleclient-migrate-playbill-claim-type"></a>

### `CruxibleClient.migrate_playbill_claim_type`

[Source](src/cruxible_client/transport/http.py#L903)

```text
migrate_playbill_claim_type(
    instance_id: str,
    *,
    request: Mapping[str, Any],
) -> contracts.PlaybillClaimTypeMigrationResponse
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/claim-types/migrations'`.

<a id="api-cruxibleclient-list-playbill-claim-types"></a>

### `CruxibleClient.list_playbill_claim_types`

[Source](src/cruxible_client/transport/http.py#L916)

```text
list_playbill_claim_types(
    instance_id: str,
    *,
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
) -> contracts.PlaybillClaimTypeList
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/claim-types'`.

<a id="api-cruxibleclient-get-playbill-claim-type"></a>

### `CruxibleClient.get_playbill_claim_type`

[Source](src/cruxible_client/transport/http.py#L928)

```text
get_playbill_claim_type(
    instance_id: str,
    predicate: str,
    *,
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
) -> contracts.PlaybillClaimTypeView
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/claim-types/{predicate}'`.

<a id="api-cruxibleclient-retire-playbill-claim"></a>

### `CruxibleClient.retire_playbill_claim`

[Source](src/cruxible_client/transport/http.py#L941)

```text
retire_playbill_claim(
    instance_id: str,
    claim_id: str,
    *,
    request: Mapping[str, Any],
) -> contracts.PlaybillClaimRetireResponse
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/claims/{claim_id}/retire'`.

<a id="api-cruxibleclient-append-playbill-claim-attestation"></a>

### `CruxibleClient.append_playbill_claim_attestation`

[Source](src/cruxible_client/transport/http.py#L956)

```text
append_playbill_claim_attestation(
    instance_id: str,
    *,
    request: ClaimAttestationAppendRequestV1,
) -> ClaimAttestationAppendResultV1
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/claim-attestations'`.

<a id="api-cruxibleclient-recover-playbill-claim-attestations"></a>

### `CruxibleClient.recover_playbill_claim_attestations`

[Source](src/cruxible_client/transport/http.py#L969)

```text
recover_playbill_claim_attestations(instance_id: str) -> None
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/claim-attestations/recover'`.

<a id="api-cruxibleclient-resolution-contracts"></a>

### `CruxibleClient.resolution_contracts`

[Source](src/cruxible_client/transport/http.py#L975)

```text
resolution_contracts(
    instance_id: str,
    *,
    request: contracts.ResolutionContractsRequestV1,
) -> contracts.ResolutionContractsResultV1
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/resolution-contracts/query'`.

<a id="api-cruxibleclient-predict-playbill"></a>

### `CruxibleClient.predict_playbill`

[Source](src/cruxible_client/transport/http.py#L984)

```text
predict_playbill(
    instance_id: str,
    *,
    request: contracts.PlaybillPredictRequestV2,
) -> contracts.PlaybillPredictResultV2
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/predictions'`.

<a id="api-cruxibleclient-settle-playbill-prediction"></a>

### `CruxibleClient.settle_playbill_prediction`

[Source](src/cruxible_client/transport/http.py#L996)

```text
settle_playbill_prediction(
    instance_id: str,
    prediction_id: str,
    *,
    request: contracts.PlaybillSettleRequestV2,
) -> contracts.PlaybillSettleResultV2
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/predictions/{prediction_id}/settlements'`.

<a id="api-cruxibleclient-create-playbill-authoring-intent"></a>

### `CruxibleClient.create_playbill_authoring_intent`

[Source](src/cruxible_client/transport/http.py#L1009)

```text
create_playbill_authoring_intent(
    instance_id: str,
    *,
    payload: Mapping[str, Any],
    reference_expectations: Sequence[Mapping[str, Any]] | None = None,
    program_stamp: Mapping[str, Any] | None = None,
) -> contracts.PlaybillAuthoringIntentView
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/authoring/intents'`.

<a id="api-cruxibleclient-create-playbill-authoring-input"></a>

### `CruxibleClient.create_playbill_authoring_input`

[Source](src/cruxible_client/transport/http.py#L1047)

```text
create_playbill_authoring_input(
    instance_id: str,
    *,
    input: Mapping[str, Any],
) -> contracts.PlaybillAuthoringIntentView
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/authoring/intents'`.

<a id="api-cruxibleclient-get-playbill-authoring-intent"></a>

### `CruxibleClient.get_playbill_authoring_intent`

[Source](src/cruxible_client/transport/http.py#L1062)

```text
get_playbill_authoring_intent(instance_id: str, intent_id: str) -> contracts.PlaybillAuthoringIntentView
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/authoring/intents/{intent_id}'`.

<a id="api-cruxibleclient-resume-playbill-authoring-intent"></a>

### `CruxibleClient.resume_playbill_authoring_intent`

[Source](src/cruxible_client/transport/http.py#L1070)

```text
resume_playbill_authoring_intent(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillAuthoringIntentView
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/authoring/intents/{intent_id}/resume'`.

<a id="api-cruxibleclient-list-pending-playbill-authoring-intents"></a>

### `CruxibleClient.list_pending_playbill_authoring_intents`

[Source](src/cruxible_client/transport/http.py#L1080)

```text
list_pending_playbill_authoring_intents(instance_id: str) -> contracts.PlaybillAuthoringIntentList
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/authoring/intents'`.

<a id="api-cruxibleclient-compile-playbill-authoring"></a>

### `CruxibleClient.compile_playbill_authoring`

[Source](src/cruxible_client/transport/http.py#L1087)

```text
compile_playbill_authoring(
    instance_id: str,
    *,
    payload: Mapping[str, Any],
    intent_id: str | None = None,
    reference_expectations: Sequence[Mapping[str, Any]] | None = None,
    program_stamp: Mapping[str, Any] | None = None,
) -> contracts.PlaybillAuthoringPreflightResult
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/authoring/compile'`.

<a id="api-cruxibleclient-compile-playbill-authoring-input"></a>

### `CruxibleClient.compile_playbill_authoring_input`

[Source](src/cruxible_client/transport/http.py#L1130)

```text
compile_playbill_authoring_input(
    instance_id: str,
    *,
    input: Mapping[str, Any],
    intent_id: str | None = None,
) -> contracts.PlaybillAuthoringPreflightResult
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/authoring/compile'`.

<a id="api-cruxibleclient-preflight-playbill-authoring-intent"></a>

### `CruxibleClient.preflight_playbill_authoring_intent`

[Source](src/cruxible_client/transport/http.py#L1147)

```text
preflight_playbill_authoring_intent(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillAuthoringPreflightResult
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/authoring/intents/{intent_id}/preflight'`.

<a id="api-cruxibleclient-rebase-playbill-authoring-intent"></a>

### `CruxibleClient.rebase_playbill_authoring_intent`

[Source](src/cruxible_client/transport/http.py#L1158)

```text
rebase_playbill_authoring_intent(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillAuthoringIntentView
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/authoring/intents/{intent_id}/rebase'`.

<a id="api-cruxibleclient-submit-playbill-authoring-intent"></a>

### `CruxibleClient.submit_playbill_authoring_intent`

[Source](src/cruxible_client/transport/http.py#L1169)

```text
submit_playbill_authoring_intent(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillAuthoringSubmitResult
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/authoring/intents/{intent_id}/submit'`.

<a id="api-cruxibleclient-playbill-authoring-intent-status"></a>

### `CruxibleClient.playbill_authoring_intent_status`

[Source](src/cruxible_client/transport/http.py#L1180)

```text
playbill_authoring_intent_status(instance_id: str, intent_id: str) -> contracts.PlaybillCandidateStatus
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/authoring/intents/{intent_id}/status'`.

<a id="api-cruxibleclient-abandon-playbill-authoring-insertion"></a>

### `CruxibleClient.abandon_playbill_authoring_insertion`

[Source](src/cruxible_client/transport/http.py#L1190)

```text
abandon_playbill_authoring_insertion(
    instance_id: str,
    intent_id: str,
    *,
    expectation_id: str | None = None,
) -> contracts.PlaybillInsertionAbandonResult
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/authoring/intents/{intent_id}/insertion/abandon'`.

<a id="api-cruxibleclient-list-playbill-claims"></a>

### `CruxibleClient.list_playbill_claims`

[Source](src/cruxible_client/transport/http.py#L1206)

```text
list_playbill_claims(
    instance_id: str,
    *,
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    subject_path: str | None = None,
    predicate: str | None = None,
    include_retired: bool = False,
) -> contracts.PlaybillClaimList
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/claims'`.

<a id="api-cruxibleclient-read-playbill-claim-batch"></a>

### `CruxibleClient.read_playbill_claim_batch`

[Source](src/cruxible_client/transport/http.py#L1229)

```text
read_playbill_claim_batch(
    instance_id: str,
    *,
    request: ClaimReadBatchRequestV1,
) -> ClaimReadBatchResultV1
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/claims/read-batch'`.

<a id="api-cruxibleclient-get-playbill-claim-backings"></a>

### `CruxibleClient.get_playbill_claim_backings`

[Source](src/cruxible_client/transport/http.py#L1241)

```text
get_playbill_claim_backings(
    instance_id: str,
    *,
    claim_ids: Sequence[str],
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any],
) -> ClaimBackingsResultV1
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/claims/backings'`.

<a id="api-cruxibleclient-get-playbill-claim"></a>

### `CruxibleClient.get_playbill_claim`

[Source](src/cruxible_client/transport/http.py#L1260)

```text
get_playbill_claim(
    instance_id: str,
    identity: str,
    *,
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    evaluation_time: str | None = None,
) -> contracts.PlaybillClaimViewV2
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/claims/{identity}'`.

<a id="api-cruxibleclient-playbill-claim-history"></a>

### `CruxibleClient.playbill_claim_history`

[Source](src/cruxible_client/transport/http.py#L1277)

```text
playbill_claim_history(instance_id: str, identity: str) -> contracts.PlaybillClaimHistory
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/claims/{identity}/history'`.

<a id="api-cruxibleclient-explain-playbill-claim"></a>

### `CruxibleClient.explain_playbill_claim`

[Source](src/cruxible_client/transport/http.py#L1283)

```text
explain_playbill_claim(
    instance_id: str,
    identity: str,
    *,
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    evaluation_time: str | None = None,
) -> contracts.PlaybillClaimExplanationV2 | contracts.PlaybillClaimExplanationV3
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/claims/{identity}/explanation'`.

<a id="api-cruxibleclient-propose-playbill-query-definition"></a>

### `CruxibleClient.propose_playbill_query_definition`

[Source](src/cruxible_client/transport/http.py#L1303)

```text
propose_playbill_query_definition(
    instance_id: str,
    *,
    query: Mapping[str, Any],
    proposal_name: str,
    base: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
) -> contracts.PlaybillProposalInspection
```

<a id="api-cruxibleclient-list-playbill-query-definitions"></a>

### `CruxibleClient.list_playbill_query_definitions`

[Source](src/cruxible_client/transport/http.py#L1316)

```text
list_playbill_query_definitions(
    instance_id: str,
    *,
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
) -> contracts.PlaybillQueryDefinitionList
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/queries'`.

<a id="api-cruxibleclient-list-playbill-policies-in-force"></a>

### `CruxibleClient.list_playbill_policies_in_force`

[Source](src/cruxible_client/transport/http.py#L1328)

```text
list_playbill_policies_in_force(instance_id: str) -> contracts.PlaybillPolicyInForceList
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/policies'`.

<a id="api-cruxibleclient-get-playbill-query-definition"></a>

### `CruxibleClient.get_playbill_query_definition`

[Source](src/cruxible_client/transport/http.py#L1335)

```text
get_playbill_query_definition(
    instance_id: str,
    name: str,
    *,
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
) -> contracts.PlaybillQueryDefinitionView
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/queries/{name}'`.

<a id="api-cruxibleclient-run-playbill-query"></a>

### `CruxibleClient.run_playbill_query`

[Source](src/cruxible_client/transport/http.py#L1348)

```text
run_playbill_query(
    instance_id: str,
    name: str,
    *,
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    evaluation_time: str | None = None,
    parameters: Mapping[str, Any] | None = None,
    budgets: Mapping[str, Any] | None = None,
) -> contracts.PlaybillQueryRun
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/queries/{name}/run'`.

<a id="api-cruxibleclient-playbill-procedure-readiness"></a>

### `CruxibleClient.playbill_procedure_readiness`

[Source](src/cruxible_client/transport/http.py#L1369)

```text
playbill_procedure_readiness(
    instance_id: str,
    name: str,
    *,
    evaluation_time: str,
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
) -> contracts.PlaybillProcedureReadiness
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/procedures/{name}/readiness'`.

<a id="api-cruxibleclient-bind-playbill-procedure"></a>

### `CruxibleClient.bind_playbill_procedure`

[Source](src/cruxible_client/transport/http.py#L1385)

```text
bind_playbill_procedure(
    instance_id: str,
    name: str,
    *,
    bindings: Sequence[Mapping[str, Any]],
) -> contracts.PlaybillProcedureBindResult
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/procedures/{name}/bind'`.

<a id="api-cruxibleclient-run-playbill-procedure"></a>

### `CruxibleClient.run_playbill_procedure`

[Source](src/cruxible_client/transport/http.py#L1401)

```text
run_playbill_procedure(
    instance_id: str,
    name: str,
    *,
    evaluation_time: str | None,
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    input: Any,
    resolution_contract: contracts.ResolutionContractReferenceV1 | None = None,
    trigger_event: contracts.TriggerEventReferenceV1 | None = None,
) -> contracts.PlaybillProcedureRunState
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/procedures/{name}/runs'`.

<a id="api-cruxibleclient-get-playbill-procedure-run"></a>

### `CruxibleClient.get_playbill_procedure_run`

[Source](src/cruxible_client/transport/http.py#L1433)

```text
get_playbill_procedure_run(instance_id: str, run_id: str) -> contracts.PlaybillProcedureRunState
```

HTTP: `GET f'/api/v1/{instance_id}/playbill/procedure-runs/{run_id}'`.

<a id="api-cruxibleclient-measure-playbill-procedure"></a>

### `CruxibleClient.measure_playbill_procedure`

[Source](src/cruxible_client/transport/http.py#L1441)

```text
measure_playbill_procedure(
    instance_id: str,
    name: str,
    *,
    request: contracts.PlaybillProcedureMeasureRequestV1,
) -> contracts.PlaybillProcedureMeasureResultV1
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/procedures/{name}/measurements'`.

<a id="api-cruxibleclient-list-playbill-procedure-readings"></a>

### `CruxibleClient.list_playbill_procedure_readings`

[Source](src/cruxible_client/transport/http.py#L1454)

```text
list_playbill_procedure_readings(
    instance_id: str,
    name: str,
    *,
    request: contracts.PlaybillProcedureReadingsRequestV1,
) -> contracts.PlaybillProcedureReadingsResultV1
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/procedures/{name}/readings'`.

<a id="api-cruxibleclient-run-playbill-line"></a>

### `CruxibleClient.run_playbill_line`

[Source](src/cruxible_client/transport/http.py#L1467)

```text
run_playbill_line(
    instance_id: str,
    line_identity_digest: str,
    *,
    occurrence_id: str | None,
    evaluation_time: str | None = None,
    resolution_contract: contracts.ResolutionContractReferenceV1 | None = None,
    trigger_event: contracts.TriggerEventReferenceV1 | None = None,
) -> contracts.PlaybillProcedureRunState
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/lines/{line_identity_digest}/runs'`.

<a id="api-cruxibleclient-next-playbill"></a>

### `CruxibleClient.next_playbill`

[Source](src/cruxible_client/transport/http.py#L1498)

```text
next_playbill(
    instance_id: str,
    *,
    evaluation_time: str,
    access_profile: Mapping[str, Any],
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    expiring_within: Mapping[str, Any] | None = None,
    workspace_observation: Mapping[str, Any] | None = None,
    since_result_digest: str | None = None,
    at_attestation_head_digest: str | None = None,
) -> contracts.PlaybillNextResult
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/next'`.

<a id="api-cruxibleclient-since-playbill"></a>

### `CruxibleClient.since_playbill`

[Source](src/cruxible_client/transport/http.py#L1528)

```text
since_playbill(
    instance_id: str,
    *,
    generation: int,
    access_profile: Mapping[str, Any],
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    max_rows: int = 100,
    max_bytes: int = 65536,
    cursor: contracts.PlaybillSinceCursor | Mapping[str, Any] | None = None,
) -> contracts.PlaybillSinceResult
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/since'`.

<a id="api-cruxibleclient-list-playbill-curation"></a>

### `CruxibleClient.list_playbill_curation`

[Source](src/cruxible_client/transport/http.py#L1567)

```text
list_playbill_curation(
    instance_id: str,
    *,
    evaluation_time: str,
    access_profile: Mapping[str, Any],
    workspace_observation: Mapping[str, Any] | None = None,
) -> contracts.PlaybillCurationListResult
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/curation/list'`.

<a id="api-cruxibleclient-audit-playbill"></a>

### `CruxibleClient.audit_playbill`

[Source](src/cruxible_client/transport/http.py#L1588)

```text
audit_playbill(
    instance_id: str,
    *,
    evaluation_time: str,
    access_profile: Mapping[str, Any],
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    claim_type_identities: tuple[str, ...] = (),
    subject_kinds: tuple[str, ...] = (),
    max_rows: int = 100,
    max_bytes: int = 65536,
    cursor: contracts.PlaybillAuditCursor | Mapping[str, Any] | None = None,
) -> contracts.PlaybillAuditResult
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/audit'`.

<a id="api-cruxibleclient-overrule-playbill-curation"></a>

### `CruxibleClient.overrule_playbill_curation`

[Source](src/cruxible_client/transport/http.py#L1636)

```text
overrule_playbill_curation(
    instance_id: str,
    *,
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    attribution_refs: tuple[str, ...] = (),
) -> contracts.PlaybillCurationActionResult
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/curation/overrule'`.

<a id="api-cruxibleclient-accept-fixed-playbill-curation"></a>

### `CruxibleClient.accept_fixed_playbill_curation`

[Source](src/cruxible_client/transport/http.py#L1657)

```text
accept_fixed_playbill_curation(
    instance_id: str,
    *,
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    accepted_proposal_id: str,
    accepted_changeset_digest: str,
    attribution_refs: tuple[str, ...] = (),
) -> contracts.PlaybillCurationActionResult
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/curation/accept-fixed'`.

<a id="api-cruxibleclient-suppress-playbill-curation"></a>

### `CruxibleClient.suppress_playbill_curation`

[Source](src/cruxible_client/transport/http.py#L1682)

```text
suppress_playbill_curation(
    instance_id: str,
    *,
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    scope: Literal['item', 'pattern', 'instance'],
    until_generation: int | None = None,
    attribution_refs: tuple[str, ...] = (),
) -> contracts.PlaybillCurationActionResult
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/curation/suppress'`.

<a id="api-cruxibleclient-discover-playbill"></a>

### `CruxibleClient.discover_playbill`

[Source](src/cruxible_client/transport/http.py#L1707)

```text
discover_playbill(
    instance_id: str,
    *,
    query: str | None = None,
    entrypoint: str | None = None,
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    evaluation_time: str | None = None,
    profile: Literal['interfaces', 'subjects', 'all'] = 'interfaces',
    budget: Mapping[str, Any] | None = None,
) -> contracts.PlaybillDiscoveryResult | contracts.PlaybillInterfaceInventory
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/discover'`.

<a id="api-cruxibleclient-search-playbill"></a>

### `CruxibleClient.search_playbill`

[Source](src/cruxible_client/transport/http.py#L1733)

```text
search_playbill(
    instance_id: str,
    *,
    mode: Literal['search', 'list', 'orient'],
    query: str | None = None,
    kinds: Sequence[str] = ('claim', 'demand', 'procedure'),
    subject: Mapping[str, Any] | None = None,
    statuses: Sequence[str] = (),
    cursor: Mapping[str, Any] | None = None,
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    evaluation_time: str | None = None,
    budgets: Mapping[str, Any] | None = None,
) -> contracts.PlaybillSearchResult
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/search'`.

<a id="api-cruxibleclient-expand-playbill"></a>

### `CruxibleClient.expand_playbill`

[Source](src/cruxible_client/transport/http.py#L1767)

```text
expand_playbill(
    instance_id: str,
    *,
    address: Mapping[str, Any],
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    evaluation_time: str | None = None,
    facets: Sequence[str] = (),
    budget: Mapping[str, Any] | None = None,
) -> contracts.PlaybillContextCapsule
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/expand'`.

<a id="api-cruxibleclient-resolve-playbill-coverage"></a>

### `CruxibleClient.resolve_playbill_coverage`

[Source](src/cruxible_client/transport/http.py#L1788)

```text
resolve_playbill_coverage(
    instance_id: str,
    *,
    observations: Sequence[Mapping[str, Any]],
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    budget: Mapping[str, Any] | None = None,
    scan_budget: Mapping[str, Any] | None = None,
) -> contracts.PlaybillCoverageResult
```

Resolve coverage for a batch of already-observed working sources.

The caller observes its own working set -- binding each path to a
declared logical source and hashing the bytes it actually read -- and
this call carries those observations. Coverage is delivered against
them; the daemon reads no client filesystem.

HTTP: `POST f'/api/v1/{instance_id}/playbill/coverage/resolve'`.

<a id="api-cruxibleclient-export-playbill-floor"></a>

### `CruxibleClient.export_playbill_floor`

[Source](src/cruxible_client/transport/http.py#L1819)

```text
export_playbill_floor(
    instance_id: str,
    *,
    at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    format_version: Literal[2, 3] = 3,
    review_notes_oid: str | None = None,
) -> contracts.PlaybillFloorExport
```

HTTP: `POST f'/api/v1/{instance_id}/playbill/floor/export'`.

## Errors and unsupported surfaces

| Family | Handling |
|---|---|
| PlaybillSdkError | Local SDK refusal, also a ValueError/CoreError; inspect code and error-specific fields. |
| ReferenceKindError, LiteralValueTypeError, ExactContentTypeError | Wrong reference/object relationship; repair the typed authoring input. |
| AbsentSubject, LiteralSchemaError, SourceSelectionError | Missing vocabulary/Subject, invalid constrained value, or invalid source selection. |
| CapabilityNotServed | Explicit unsupported feature with capability, code, and repair. |
| ProcedureCompositionError | Contains `.preview`, including per-step errors and pending checks. |
| ApprovalReviewMismatch | Review/signing context does not match; obtain and inspect a new review rather than silently signing a changed candidate. |
| ProjectionMarkerError, ProjectionRepinError, ProjectionSyncError | Invalid/corrupt declaration, refused repin, or policy-enforced sync failure. |
| IncompatibleDaemonVersion | Client and daemon authoring contract snapshots differ; upgrade deliberately. |
| ServerUnreachableError | Connection failure or timeout; a timed-out mutation may have completed. |
| AuthenticationError / PermissionDeniedError / typed daemon errors | Repair credentials, scope, authority, or the server’s structured refusal. |

`ClaimDraft.derived_by()` always raises the unavailable derivation-carry refusal.
`Publication` only handles already-existing insertion expectations; no new
publish_to writer exists. `DerivationSpec` is a value type, not proof of a served
derivation writer. Root `__all__` still lists `PlaybillInsertionApplication`, but
that attribute is absent at this revision and cannot be imported. This reference
records the mismatch rather than advertising a working API or changing code.

Public wire models remain in `cruxible_client.contracts`; `model_fields`,
`model_json_schema()`, and model validation expose exact types, requiredness,
constraints, and discriminator vocabulary. A model existing in that namespace
does not mean the SDK currently serves its operation. Historical contracts stay
separate from current run readiness.

## Import and module index

The table lists every name in the package-root `__all__` and its defining
module. The following appendix indexes the remaining exported authoring utilities
and shared models. Source links resolve to the exact checked-out revision and
include constructor/validator definitions for request and response contracts.

| Root import | Definition / availability |
|---|---|
| `install_provider_package` | `cruxible_client.provider_installation` · [Source](src/cruxible_client/provider_installation.py) |
| `ApprovalReviewMismatch` | `cruxible_client.authoring.approval` · [Source](src/cruxible_client/authoring/approval.py) |
| `ReviewedProposal` | `cruxible_client.authoring.approval` · [Source](src/cruxible_client/authoring/approval.py) |
| `ApprovalSigner` | `cruxible_client.authoring.signing` · [Source](src/cruxible_client/authoring/signing.py) |
| `LocalEd25519ApprovalSigner` | `cruxible_client.authoring.signing` · [Source](src/cruxible_client/authoring/signing.py) |
| `AbsentSubject` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `AccessProfile` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `ActivationPolicy` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `Audience` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `ArtifactIdentity` | `cruxible_client.contracts.artifacts` · [Source](src/cruxible_client/contracts/artifacts.py) |
| `ArtifactLifecycle` | `cruxible_client.contracts.artifacts` · [Source](src/cruxible_client/contracts/artifacts.py) |
| `ArtifactPin` | `cruxible_client.contracts.artifacts` · [Source](src/cruxible_client/contracts/artifacts.py) |
| `CapabilityNotServed` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `Cardinality` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `CaptureRef` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `CaptureView` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `ClaimObjectKind` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `ClaimAdmissionPolicyV1` | `cruxible_client.contracts.policies` · [Source](src/cruxible_client/contracts/policies.py) |
| `ClaimAttestationV2Signer` | `cruxible_client.authoring.attestations` · [Source](src/cruxible_client/authoring/attestations.py) |
| `ClaimRef` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `ClaimRole` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `ClaimTypeRef` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `ClaimResolutionPolicyV1` | `cruxible_client.contracts.policies` · [Source](src/cruxible_client/contracts/policies.py) |
| `CruxibleClient` | `cruxible_client.transport.http` · [Source](src/cruxible_client/transport/http.py) |
| `Disposition` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `Duration` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `CanonicalDurationV1` | `cruxible_client.contracts.captures` · [Source](src/cruxible_client/contracts/captures.py) |
| `ContractSchema` | `cruxible_client.contracts.procedures.contract_schema` · [Source](src/cruxible_client/contracts/procedures/contract_schema.py) |
| `EffectivePeriod` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `ExactContent` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `ExactContentTypeError` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `KindNamespace` | `cruxible_client.authoring.world` · [Source](src/cruxible_client/authoring/world.py) |
| `LiteralSchemaError` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `LiteralValue` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `LiteralValueTypeError` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `LocalEd25519ClaimAttestationSigner` | `cruxible_client.authoring.attestations` · [Source](src/cruxible_client/authoring/attestations.py) |
| `PendingClaimTypeRef` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `PendingSubjectRef` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `Playbill` | `cruxible_client.authoring.sdk` · [Source](src/cruxible_client/authoring/sdk.py) |
| `ProjectionPackage` | `cruxible_client.authoring.projection_package` · [Source](src/cruxible_client/authoring/projection_package.py) |
| `Prediction` | `cruxible_client.authoring.sdk` · [Source](src/cruxible_client/authoring/sdk.py) |
| `PredictionSettlement` | `cruxible_client.authoring.sdk` · [Source](src/cruxible_client/authoring/sdk.py) |
| `PlaybillInsertionApplication` | Unavailable at this revision; stale export. |
| `PlaybillInsertionApplyError` | `cruxible_client.authoring.insertions` · [Source](src/cruxible_client/authoring/insertions.py) |
| `PlaybillWorkspaceError` | `cruxible_client.authoring.workspace` · [Source](src/cruxible_client/authoring/workspace.py) |
| `activate_with_workspace_refresh` | `cruxible_client.authoring.workspace` · [Source](src/cruxible_client/authoring/workspace.py) |
| `inspect_workspace_floor` | `cruxible_client.authoring.workspace` · [Source](src/cruxible_client/authoring/workspace.py) |
| `observe_playbill_next_workspace` | `cruxible_client.authoring.workspace` · [Source](src/cruxible_client/authoring/workspace.py) |
| `materialize_playbill_floor` | `cruxible_client.authoring.workspace` · [Source](src/cruxible_client/authoring/workspace.py) |
| `ProcedureRef` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `ProcedureBudgetV3` | `cruxible_client.contracts.procedures.models` · [Source](src/cruxible_client/contracts/procedures/models.py) |
| `ProcedureDefinitionV3` | `cruxible_client.contracts.procedures.models` · [Source](src/cruxible_client/contracts/procedures/models.py) |
| `ProcedureHardCapsV3` | `cruxible_client.contracts.procedures.models` · [Source](src/cruxible_client/contracts/procedures/models.py) |
| `ProcedureOwnedContractV1` | `cruxible_client.contracts.procedures.artifacts` · [Source](src/cruxible_client/contracts/procedures/artifacts.py) |
| `ProcedurePinSlotRefV1` | `cruxible_client.contracts.procedures.models` · [Source](src/cruxible_client/contracts/procedures/models.py) |
| `ProcedurePinSlotV1` | `cruxible_client.contracts.procedures.models` · [Source](src/cruxible_client/contracts/procedures/models.py) |
| `ProjectNodeV3` | `cruxible_client.contracts.procedures.models` · [Source](src/cruxible_client/contracts/procedures/models.py) |
| `PropertySchema` | `cruxible_client.contracts.procedures.contract_schema` · [Source](src/cruxible_client/contracts/procedures/contract_schema.py) |
| `QueryRef` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `ReferentSensitivity` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `SlotRef` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `SourceRef` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `StateTapNodeV3` | `cruxible_client.contracts.procedures.models` · [Source](src/cruxible_client/contracts/procedures/models.py) |
| `SubjectRef` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `TypedRef` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `TransformNodeV3` | `cruxible_client.contracts.procedures.models` · [Source](src/cruxible_client/contracts/procedures/models.py) |
| `World` | `cruxible_client.authoring.world` · [Source](src/cruxible_client/authoring/world.py) |
| `WorldClaimType` | `cruxible_client.authoring.world` · [Source](src/cruxible_client/authoring/world.py) |
| `WorldStructureError` | `cruxible_client.authoring.world` · [Source](src/cruxible_client/authoring/world.py) |
| `WorldSubject` | `cruxible_client.authoring.world` · [Source](src/cruxible_client/authoring/world.py) |

### Module `cruxible_client.authoring.attestations`

[Source](src/cruxible_client/authoring/attestations.py)

#### `LocalClaimAttestationKeyUnavailable`

Key generation, custody, or public/private correspondence failed.

#### `PRINCIPAL_KEY_PATH_ENV`

Exported constant or type alias; exact value and admissible members are defined in the linked module.

#### `prepare_claim_attestation`

```text
prepare_claim_attestation(client: 'Any', instance_id: 'str', *, prepared: 'PreparedClaimAttestationRequestV1', signer: 'ClaimAttestationV2Signer') -> 'ClaimAttestationV2'
```

Bind and sign an exact accepted Claim without appending or accepting it.

#### `append_prepared_claim_attestation`

```text
append_prepared_claim_attestation(client: 'Any', instance_id: 'str', *, prepared: 'PreparedClaimAttestationRequestV1', signer: 'ClaimAttestationV2Signer') -> 'ClaimAttestationAppendResultV1'
```

#### `local_attestation_signer_from_environment`

```text
local_attestation_signer_from_environment(client: 'Any', instance_id: 'str', *, workspace_root: 'Path | None' = None) -> 'LocalEd25519ClaimAttestationSigner'
```

Resolve the authenticated actor and its local key without wire disclosure.

### Module `cruxible_client.authoring.bind`

[Source](src/cruxible_client/authoring/bind.py)

#### `AuthoringBindAnchorNotFoundError`

The requested anchor was absent from the selected file.

#### `AuthoringBindAmbiguityError`

The requested anchor identified multiple byte occurrences.

#### `AuthoringBindError`

A local Flow-A bind input could not produce one mechanical observation.

#### `bind_working_selection_input`

```text
bind_working_selection_input(input: 'ClaimInput', *, content: 'bytes', anchor: 'str', window_lines: 'int | None' = None, occurrence: 'int | None' = None) -> 'ClaimAuthoringPayloadV1'
```

Observe local bytes for a decision-only working_selection Claim input.

### Module `cruxible_client.authoring.blocks`

[Source](src/cruxible_client/authoring/blocks.py)

#### `ParsedProjectionBlock`

ParsedProjectionBlock(source_id: 'str', block_id: 'str', stamp: 'ProjectionBlockStamp | None', opening_start: 'int', opening_end: 'int', body_start: 'int', body_end: 'int', closing_end: 'int', body_digest: 'str')

#### `ProjectionIndependentEvidenceForbidden`

A citation's span lies inside a projection block: the client fast path.

Evidence never comes from a projection window, whatever the citation's role
or origin. The daemon refuses the same span with the same code at lowering
and at the citation gate; this raises before the wire so an author learns
it without a round trip.

#### `ProjectionMarkerError`

Base class for Playbill protocol and storage refusals.

#### `ProjectionRepinError`

Base class for Playbill protocol and storage refusals.

#### `ProjectionSyncError`

Base class for Playbill protocol and storage refusals.

#### `assert_independent_projection_evidence`

```text
assert_independent_projection_evidence(*, source_id: 'str', content: 'bytes', start_byte: 'int', end_byte: 'int') -> 'None'
```

Refuse a span that touches any stamped block window in `content`.

Reads the windows with the evidence-side scanner rather than the page
parser: a cited source is evidence, not a projection page, so it is neither
held to the page ceilings nor refused for a marker defect of its own. A
capture with no marker bytes costs one substring search.

#### `frame_projection_block`

```text
frame_projection_block(*, stamp: 'ProjectionBlockStamp', body: 'bytes', compact: 'bool' = False) -> 'bytes'
```

Mechanically frame accepted bytes and prove the one frozen marker grammar.

#### `parse_projection_blocks`

```text
parse_projection_blocks(content: 'bytes', *, source_id: 'str', allow_bootstrap: 'bool' = False, manifests: 'Mapping[str, bytes] | None' = None) -> 'tuple[ParsedProjectionBlock, ...]'
```

Parse one complete source using its known logical source identity.

#### `render_projection_opening`

```text
render_projection_opening(stamp: 'ProjectionBlockStamp') -> 'bytes'
```

#### `repin_projection_block`

```text
repin_projection_block(client: 'CruxibleClient', instance_id: 'str', *, workspace: 'str | Path', source_id: 'str', block_id: 'str', claims: 'Sequence[str] | None' = None, queries: 'Sequence[tuple[str, Mapping[str, object]]] | None' = None, artifacts: 'Sequence[ArtifactIdentity] | None' = None, currency_policy: 'ProjectionCurrencyPolicy | None' = None, backing_digest: 'str | None' = None, evaluation_time: 'datetime', coordinate: 'AcceptedCoordinate | None' = None, body: 'bytes | None' = None, compact: 'bool' = True) -> 'ProjectionBlockStampV2'
```

Repin one block, optionally installing explicitly supplied agent-authored body bytes.

Omitted body preserves prose. Compact markers are the default; their manifests
are retained before the page write. Use a reviewed exact-content package Claim
for ledger recovery.
The whole-file compare-and-swap preserves concurrent author edits.

#### `sync_projection_blocks`

```text
sync_projection_blocks(client: 'CruxibleClient', instance_id: 'str', *, workspace: 'str | Path', paths: 'Sequence[str | Path]' = (), all_sources: 'bool' = False, check: 'bool' = False, detach_paths: 'Sequence[str | Path]' = ()) -> 'PlaybillBlockSyncResultV1'
```

Check all dependencies without authoring prose. Only explicit detach edits files.

### Module `cruxible_client.authoring.context`

[Source](src/cruxible_client/authoring/context.py)

#### `PlaybillContextResolutionError`

A selected workspace or target layer is not a safe context source.

#### `PlaybillWorkspaceBinding`

```text
tag: Literal['playbill-coverage-workspace-config-v1', 'playbill-coverage-workspace-config-v2'] = 'playbill-coverage-workspace-config-v1'
server_url: str | None = None
server_socket: str | None = None
instance_id: str | None = None
```

```text
attached() -> bool
```

The target-bearing subset of a workspace coverage configuration.

#### `ResolvedPlaybillContext`

```text
server_url: str | None
server_socket: str | None
instance_id: str | None
transport_source: TargetSource
instance_source: TargetSource
workspace: Path
workspace_source: WorkspaceSource
workspace_binding_path: Path | None
workspace_attached: bool
warnings: tuple[str, ...] = ()
instance_transport_mismatch: str | None = None
```

Resolved workspace and independently sourced target components.

#### `TargetSource`

Exported constant or type alias; exact value and admissible members are defined in the linked module.

#### `WorkspaceSource`

Exported constant or type alias; exact value and admissible members are defined in the linked module.

#### `resolve_playbill_context`

```text
resolve_playbill_context(*, server_url: 'str | None' = None, server_socket: 'str | None' = None, instance_id: 'str | None' = None, workspace: 'str | Path | None' = None, remembered: 'Mapping[str, object] | None' = None, environ: 'Mapping[str, str] | None' = None, cwd: 'Path | None' = None, no_workspace: 'bool' = False, home: 'Path | None' = None) -> 'ResolvedPlaybillContext'
```

Resolve explicit > environment > workspace > remembered context.

Transport and instance are selected independently. The workspace binding is
eligible only when its coverage config names both one transport and an
instance; incomplete coverage-only configs remain valid but do not retarget
commands.

### Module `cruxible_client.authoring.examples`

[Source](src/cruxible_client/authoring/examples.py)

#### `AUTHORING_EXAMPLE_FACTORIES`

Exported constant or type alias; exact value and admissible members are defined in the linked module.

#### `AUTHORING_EXAMPLE_NAMES`

Exported constant or type alias; exact value and admissible members are defined in the linked module.

#### `AuthoringExampleName`

Exported constant or type alias; exact value and admissible members are defined in the linked module.

#### `authoring_example`

```text
authoring_example(name: 'AuthoringExampleName', *, claim_id: 'str | None' = None, capture_digest: 'str | None' = None) -> 'AuthoringExample'
```

#### `change_set_example`

```text
change_set_example() -> 'ChangeSetInput'
```

Return one changeset carrying a mix of members that admit or refuse together.

One authoring surface is one changeset: a Subject, the ClaimType that
admits the statement, two Claims that read both, and one retirement all
lower once and generate once.

#### `claim_type_succession_example`

```text
claim_type_succession_example() -> 'ChangeSetInput'
```

Return one changeset that evolves a committed vocabulary in one generation.

The succession names the ClaimType it replaces and pins its exact current
digest; every member of that ClaimType's reverse-pin closure is
dispositioned in the same set -- one carried to the successor, one
tombstoned, one re-authored as the sibling Claim member that says it again
under the new vocabulary.

#### `claim_existing_capture_example`

```text
claim_existing_capture_example() -> 'ClaimInput'
```

#### `claim_flow_a_example`

```text
claim_flow_a_example() -> 'ClaimInput'
```

#### `claim_exact_content_example`

```text
claim_exact_content_example() -> 'ClaimInput'
```

A Claim whose object IS the text, not a value that names it.

Rulings and method laws are this shape: the statement is the wording, so the
object carries the body rather than a literal the ClaimType admits. `text`
is the ordinary spelling; `content_base64` is the same object for bytes that
are not text.

#### `claim_self_source_example`

```text
claim_self_source_example() -> 'ClaimInput'
```

#### `claim_subject_relation_example`

```text
claim_subject_relation_example() -> 'ClaimInput'
```

#### `document_example`

```text
document_example() -> 'DocumentShell'
```

#### `approval_policy_example`

```text
approval_policy_example() -> 'ApprovalPolicyInput'
```

#### `procedure_example`

```text
procedure_example() -> 'ProcedureInput'
```

#### `procedure_mandate_example`

```text
procedure_mandate_example() -> 'ProcedureMandateInputV1'
```

#### `query_claims_by_type_example`

```text
query_claims_by_type_example() -> 'QueryDefinitionInput'
```

Return a governed query template for current supported work-item status.

#### `subject_example`

```text
subject_example() -> 'SubjectInput'
```

### Module `cruxible_client.authoring.insertions`

[Source](src/cruxible_client/authoring/insertions.py)

#### `PlaybillInsertionApplyError`

A local source cannot be reconciled with its insertion expectation.

#### `replace_publication_file`

```text
replace_publication_file(path: 'Path', *, expected: 'bytes', replacement: 'bytes') -> 'None'
```

Durably replace one exact preimage without overwriting a concurrent edit.

### Module `cruxible_client.authoring.projection_package`

[Source](src/cruxible_client/authoring/projection_package.py)

#### `load_projection_manifests`

```text
load_projection_manifests(workspace: 'Path', content: 'bytes') -> 'dict[str, bytes]'
```

#### `retain_local_manifests`

```text
retain_local_manifests(workspace: 'Path', manifests: 'Mapping[str, bytes]') -> 'None'
```

### Module `cruxible_client.authoring.seed`

[Source](src/cruxible_client/authoring/seed.py)

#### `SEED_BODY_DIRECTORY`

Exported constant or type alias; exact value and admissible members are defined in the linked module.

#### `SEED_BUNDLE_DIGEST_DOMAIN`

Exported constant or type alias; exact value and admissible members are defined in the linked module.

#### `SEED_GROUP_OPERATION_DIGEST_DOMAIN`

Exported constant or type alias; exact value and admissible members are defined in the linked module.

#### `SEED_ENTRY_DIRECTORIES`

Exported constant or type alias; exact value and admissible members are defined in the linked module.

#### `SEED_GROUP_OPERATIONS`

Exported constant or type alias; exact value and admissible members are defined in the linked module.

#### `SeedBundleEntryV1`

```text
tag: Literal['playbill-seed-bundle-entry-v1'] = 'playbill-seed-bundle-entry-v1'
path: str
kind: SeedEntryKind
identity: str
payload: dict[str, Any]
```

One authoring JSON, named by the identity its grouping turns on.

#### `SeedBundleError`

A bundle could not be read, or could not be legally grouped.

#### `SeedCarriedEntryV1`

```text
tag: Literal['playbill-seed-carried-entry-v1'] = 'playbill-seed-carried-entry-v1'
path: str
kind: SeedEntryKind
identity: str
carried_by: str
```

One bundle entry that needs no proposal because a Claim already carries it.

#### `SeedEntryKind`

Exported constant or type alias; exact value and admissible members are defined in the linked module.

#### `SeedPlanV1`

```text
tag: Literal['playbill-seed-plan-v1'] = 'playbill-seed-plan-v1'
proposal_name: str
body_paths: tuple[str, ...] = ()
groups: tuple[SeedProposalGroupV1, ...] = ()
carried: tuple[SeedCarriedEntryV1, ...] = ()
```

```text
group_ids() -> tuple[str, ...]
```

```text
group(group_id: str) -> SeedProposalGroupV1
```

```text
next_group_id(after: str) -> str | None
```

The whole grouping, before a byte is stored or a proposal is opened.

Deterministic in the bundle's bytes alone: no clock, no accepted state, no
instance. That is what lets `--plan` answer offline and lets a run manifest
pin `plan_digest` as evidence that two arms seeded the same world.

#### `SeedPlanResultV1`

```text
tag: Literal['playbill-seed-plan-result-v1'] = 'playbill-seed-plan-result-v1'
plan: SeedPlanV1
plan_digest: str
rendered: tuple[str, ...]
```

The typed result of offline seed planning: the complete plan, its digest, and
its rendered explanation. It does not indicate that the plan was submitted.

#### `SeedProposalGroupV1`

```text
tag: Literal['playbill-seed-proposal-group-v1'] = 'playbill-seed-proposal-group-v1'
group_id: str
proposal_slug: str
kind: SeedEntryKind
operation: str
entry_paths: tuple[str, ...] = Field(min_length=1)
rationale: str
```

One proposal the plan will submit, and the reason it is exactly one.

#### `plan_seed_bundle`

```text
plan_seed_bundle(files: 'Mapping[str, bytes]', *, proposal_name: 'str') -> 'SeedPlanV1'
```

Group one bundle into the fewest proposals its own closures allow.

#### `plan_seed_directory`

```text
plan_seed_directory(root: 'Path', *, proposal_name: 'str') -> 'SeedPlanResultV1'
```

#### `proposal_slug`

```text
proposal_slug(group_id: 'str') -> 'str'
```

Fold one group id into the seed-plan v1 presentation-slug grammar.

A group id names an artifact and is written for a person to select --
`query_definition:project.work_items`. The v1 plan retains its short slug for
byte compatibility and human display, but proposal refs are now machine-owned
content addresses from :func:`seed_group_proposal_name`.

#### `read_seed_bundle`

```text
read_seed_bundle(files: 'Mapping[str, bytes]') -> 'tuple[SeedBundleEntryV1, ...]'
```

Read a bundle's files into typed, byte-sorted entries.

`files` is keyed by bundle-relative POSIX path. A file outside the known
directories refuses rather than being ignored: silently skipping part of a
bundle would make "this bundle was applied" untrue in a way nobody could see.

#### `read_seed_bundle_files`

```text
read_seed_bundle_files(root: 'Path') -> 'dict[str, bytes]'
```

Read one bundle without following symlinks or escaping its root.

#### `render_seed_plan`

```text
render_seed_plan(plan: 'SeedPlanV1') -> 'tuple[str, ...]'
```

The human rendering: what will be proposed, in order, and what rides along.

#### `seed_group_operation_digest`

```text
seed_group_operation_digest(plan: 'SeedPlanV1', group: 'SeedProposalGroupV1') -> 'Sha256Value'
```

Bind one planned group to its exact, name-independent bundle content.

#### `seed_group_proposal_name`

```text
seed_group_proposal_name(plan: 'SeedPlanV1', group: 'SeedProposalGroupV1') -> 'str'
```

Return the machine-owned proposal-ref leaf for one seed operation.

#### `seed_plan_digest`

```text
seed_plan_digest(plan: 'SeedPlanV1') -> 'Sha256Value'
```

Digest the plan, so a run manifest can pin the world it seeds.

### Module `cruxible_client.authoring.source_map`

[Source](src/cruxible_client/authoring/source_map.py)

#### `DiagnosticSourceMap`

```text
entries: tuple[SourceMapEntry, ...]
```

```text
locate(emitted_path: str) -> CallSite | None
```

DiagnosticSourceMap(entries: 'tuple[SourceMapEntry, ...]')

#### `capture_keyword_sites`

```text
capture_keyword_sites(operation: 'str', *, stacklevel: 'int' = 1) -> 'dict[str, CallSite]'
```

Locate keyword expressions in the smallest call spanning the caller line.

#### `entries_for_keywords`

```text
entries_for_keywords(*, builder: 'str', emitted: 'dict[str, tuple[str, ...]]', sites: 'dict[str, CallSite]') -> 'tuple[SourceMapEntry, ...]'
```

### Module `cruxible_client.authoring.sources`

[Source](src/cruxible_client/authoring/sources.py)

#### `WorkspaceSourceError`

Local catalog paths or aliases are not a valid compilation request.

#### `compile_client_source_context`

```text
compile_client_source_context(client: 'SourceContextClient', instance_id: 'str', *, catalog: 'SourceCatalog', repository_root: 'Path', aliases: 'Mapping[str, Path]') -> 'SourceCompilationBundle'
```

Read local bytes against path-free accepted context from the daemon.

#### `load_source_catalog`

```text
load_source_catalog(portable_path: 'Path', local_path: 'Path | None') -> 'SourceCatalog'
```

#### `mapped_root_aliases`

```text
mapped_root_aliases(values: 'Mapping[str, Path]') -> 'dict[str, Path]'
```

#### `root_aliases`

```text
root_aliases(values: 'Iterable[str]') -> 'dict[str, Path]'
```

### Module `cruxible_client.authoring.workspace`

[Source](src/cruxible_client/authoring/workspace.py)

#### `PlaybillWorkspaceAttachmentError`

Daemon registration and the requested client workspace disagree.

#### `PlaybillWorkspaceError`

A client workspace or exported floor failed deterministic validation.

#### `activate_with_workspace_refresh`

```text
activate_with_workspace_refresh(client: '_FloorClient', instance_id: 'str', proposal_id: 'str', *, workspace: 'str | Path', sync: 'bool' = True) -> 'contracts.PlaybillWorkspaceActivationResult'
```

Activate once, refresh the floor, then independently sync local blocks.

#### `configured_floor_path`

```text
configured_floor_path(workspace: 'str | Path') -> 'str | None'
```

Return the declared v2 floor path, or `None` when absent/unconfigured.

#### `inspect_workspace_floor`

```text
inspect_workspace_floor(workspace: 'str | Path', *, current_coordinate: 'contracts.PlaybillAcceptedCoordinate | None') -> 'contracts.PlaybillWorkspaceFloorStatus'
```

Compare the installed configured floor with a daemon coordinate.

#### `observe_playbill_next_workspace`

```text
observe_playbill_next_workspace(workspace: 'str | Path') -> 'dict[str, object]'
```

Observe the configured floor and every resolvable installed catalog source.

The daemon compares `installed_coordinate` with its resolved coordinate.  Therefore
the local `stale` spelling produced without a daemon coordinate is only a transport
hint; it cannot manufacture a stale or current queue item. Invalid catalogs leave
sources unobserved; individual unavailable sources are omitted from an otherwise
valid observation so the daemon can explain each accepted citation separately.

#### `observe_playbill_next_workspace_with_coverage`

```text
observe_playbill_next_workspace_with_coverage(client: '_CoverageClient', instance_id: 'str', workspace: 'str | Path', *, observation: 'Mapping[str, object] | None' = None, coordinate: 'contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None' = None, access_profile: 'Mapping[str, Any] | None' = None, resolve_coordinate: 'Callable[[], contracts.PlaybillAcceptedCoordinate] | None' = None) -> 'tuple[dict[str, object], contracts.PlaybillAcceptedCoordinate | None]'
```

Enrich next with one existing, coordinate-bound coverage-scanner read.

This adapter never searches source bytes. Every accepted occurrence comes
from the existing server coverage card; the local slice check only verifies
that card against the exact bytes it previously sent to the sole scanner.

#### `observe_playbill_projection_coverage`

```text
observe_playbill_projection_coverage(workspace: 'str | Path', *, coordinate: 'contracts.PlaybillAcceptedCoordinate | Mapping[str, Any]') -> 'dict[str, object] | None'
```

Build bounded, coordinate-bound proof of configured local projections.

A valid catalog completely describes Procedure projection intent. Claim
coverage is complete only when every Document source can be read and its
complete marker set parses. Missing or malformed evidence removes that
kind from `complete_kinds` instead of manufacturing absence.

#### `materialize_playbill_floor`

```text
materialize_playbill_floor(workspace: 'str | Path', *, export: 'contracts.PlaybillFloorExport', force: 'bool' = True) -> 'contracts.PlaybillWorkspaceFloorWriteResult'
```

Verify and exactly replace one workspace-relative floor directory.

#### `record_playbill_floor_output`

```text
record_playbill_floor_output(workspace: 'str | Path', *, instance_id: 'str', server_url: 'str | None' = None, server_socket: 'str | None' = None) -> 'Path'
```

Record the fixed floor output while preserving safe existing coverage fields.

#### `refresh_workspace_floor`

```text
refresh_workspace_floor(client: '_FloorClient', instance_id: 'str', *, workspace: 'str | Path', at: 'contracts.PlaybillAcceptedCoordinate | None' = None) -> 'contracts.PlaybillFloorRefreshResult'
```

Refresh only the local floor and report the coordinate actually written.

A pinned request refuses a mismatched export before touching local files.
inspect_workspace_floor reports the installed coordinate independently,
including after a failed refresh. No projection prose or declaration changes.

#### `validate_playbill_workspace_config_write`

```text
validate_playbill_workspace_config_write(workspace: 'str | Path', *, instance_id: 'str | None', server_url: 'str | None' = None, server_socket: 'str | None' = None, replace: 'bool' = False) -> 'None'
```

Refuse a differing config before a host or init request mutates daemon state.

#### `verified_floor_files`

```text
verified_floor_files(export: 'contracts.PlaybillFloorExport') -> 'dict[str, bytes]'
```

Verify the v2 envelope, manifest, inventory, and bytes.

#### `write_playbill_workspace_config`

```text
write_playbill_workspace_config(workspace: 'str | Path', *, instance_id: 'str', server_url: 'str | None' = None, server_socket: 'str | None' = None, replace: 'bool' = False) -> 'Path'
```

Attach one workspace target without ever accepting or persisting a secret.

### Module `cruxible_client.authoring.world`

[Source](src/cruxible_client/authoring/world.py)

#### `CLAIM_TYPE_MEMBERS`

Exported constant or type alias; exact value and admissible members are defined in the linked module.

#### `SUBJECT_MEMBERS`

Exported constant or type alias; exact value and admissible members are defined in the linked module.

#### `WorldStructureError`

The world cannot answer this name at the shape it was asked for.

#### `admit_literal`

```text
admit_literal(value: 'object', *, predicate: 'str', schema: 'Mapping[str, object] | None') -> 'CanonicalValue'
```

Admit one value against a ClaimType's declared literal schema.

This is a pre-wire read of the schema the ClaimType already publishes, over
the keywords `ClaimTypeStructure` admits plus the exact string and numeric
bounds. It is deliberately not a general JSON Schema implementation: an
unrecognised keyword is left to the daemon, which stays the only authority
on admission. What it buys is the round trip -- a mistyped enum member or a
digest that is 39 hex characters refuses here, naming the predicate, rather
than after a proposal.

#### `build_world`

```text
build_world(playbill: 'Playbill', *, coordinate: 'AcceptedCoordinate', claim_type_envelopes: 'Sequence[Mapping[str, object]]') -> 'World'
```

Assemble one world from the accepted ClaimType vocabulary.

Subject kinds come from the vocabulary rather than from the Subjects
themselves, which is what lets `pb.world()` name every kind without reading
a single Subject: a kind with no ClaimType admitting it is a kind nothing
can be said about.

#### `literal_schema_members`

```text
literal_schema_members(schema: 'Mapping[str, object] | None') -> 'tuple[str, ...]'
```

Return the string enum members a literal schema names, in schema order.

### Module `cruxible_client.authoring.world_stub`

[Source](src/cruxible_client/authoring/world_stub.py)

#### `STUB_HEADER_TAG`

Exported constant or type alias; exact value and admissible members are defined in the linked module.

#### `render_world_stub`

```text
render_world_stub(world: 'World') -> 'str'
```

Return the `.pyi` source for one world, byte-identical per coordinate.

#### `render_world_stub_for`

```text
render_world_stub_for(client: 'CruxibleClient', instance_id: 'str', *, workspace: 'str | Path') -> 'str'
```

Render the `.pyi` for one instance's accepted world over an open client.

The sanctioned entry point for a caller that holds a client rather than a
`Playbill` -- the CLI leaf, and anything else outside this package -- so no
caller has to reach for a private constructor. The workspace is only the
root a relative source selection would resolve against; this reads nothing
from it, so a directory with no Playbill workspace is fine.

### Module `cruxible_client.provider_installation`

[Source](src/cruxible_client/provider_installation.py)

#### `install_provider_package`

```text
install_provider_package(client: 'CruxibleClient', instance_id: str, *, wheel: pathlib._local.Path, lock: pathlib._local.Path, dependency_wheels: tuple[pathlib._local.Path, ...] = (), extras: tuple[str, ...] = (), control_domain: str = 'operator', reverify: bool = False) -> cruxible_client.contracts.provider_installation.PlaybillProviderInstallResultV1
```

Paths are consumed here on the client; the daemon receives only CAS references.

### Contract model index

This is the complete public class index under `cruxible_client.contracts` at
the checked revision. Open the linked model for its declared fields, validation
rules, enum values, and historical format. The high-level SDK’s own return/value
fields are documented above; these links keep wire schema definitions singular.

**`contracts.__init__`** — [GitWorkspaceNoteV1](src/cruxible_client/contracts/__init__.py#L258), [PlaybillHostResult](src/cruxible_client/contracts/__init__.py#L268), [PlaybillHostWorkspaceRegistrationV1](src/cruxible_client/contracts/__init__.py#L276), [PlaybillHostCompatibilityReasonV1](src/cruxible_client/contracts/__init__.py#L298), [PlaybillHostInspectionV1](src/cruxible_client/contracts/__init__.py#L306), [RuntimeCredentialBootstrapResult](src/cruxible_client/contracts/__init__.py#L336), [RuntimeCredentialMetadata](src/cruxible_client/contracts/__init__.py#L343), [RuntimeCredentialResult](src/cruxible_client/contracts/__init__.py#L353), [RuntimeCredentialListResult](src/cruxible_client/contracts/__init__.py#L358), [ProviderLaneStatusV1](src/cruxible_client/contracts/__init__.py#L362), [ServerInfoResult](src/cruxible_client/contracts/__init__.py#L396), [ServerRestartResult](src/cruxible_client/contracts/__init__.py#L409), [ServerStopResult](src/cruxible_client/contracts/__init__.py#L415), [IsolatedExecutorRegistrationV1](src/cruxible_client/contracts/__init__.py#L424), [PlaybillAcceptedCoordinate](src/cruxible_client/contracts/__init__.py#L444), [PlaybillInitResult](src/cruxible_client/contracts/__init__.py#L454), [PlaybillCasObjectResult](src/cruxible_client/contracts/__init__.py#L467), [PlaybillProposalInspection](src/cruxible_client/contracts/__init__.py#L476), [PlaybillProposalListEntry](src/cruxible_client/contracts/__init__.py#L489), [PlaybillProposalList](src/cruxible_client/contracts/__init__.py#L507), [PlaybillProposalSelectorResultV1](src/cruxible_client/contracts/__init__.py#L516), [PlaybillProposalReadmitResult](src/cruxible_client/contracts/__init__.py#L524), [PlaybillProposalWithdrawResult](src/cruxible_client/contracts/__init__.py#L533), [PlaybillWhoAmI](src/cruxible_client/contracts/__init__.py#L544), [PlaybillRefusalInspection](src/cruxible_client/contracts/__init__.py#L557), [PlaybillSemanticFieldValue](src/cruxible_client/contracts/__init__.py#L566), [PlaybillSemanticFieldDelta](src/cruxible_client/contracts/__init__.py#L579), [PlaybillReviewedMember](src/cruxible_client/contracts/__init__.py#L588), [PlaybillProjectionAdvisory](src/cruxible_client/contracts/__init__.py#L606), [PlaybillProjectionEvidence](src/cruxible_client/contracts/__init__.py#L628), [PlaybillProposalReview](src/cruxible_client/contracts/__init__.py#L656), [PlaybillApprovalChallenge](src/cruxible_client/contracts/__init__.py#L678), [PlaybillApprovalReceipt](src/cruxible_client/contracts/__init__.py#L689), [PlaybillActivationReceipt](src/cruxible_client/contracts/__init__.py#L703), [PlaybillFloorRefreshResult](src/cruxible_client/contracts/__init__.py#L714), [PlaybillWorkspaceActivationResult](src/cruxible_client/contracts/__init__.py#L728), [PlaybillDocumentView](src/cruxible_client/contracts/__init__.py#L735), [PlaybillDocumentList](src/cruxible_client/contracts/__init__.py#L745), [PlaybillPrincipalList](src/cruxible_client/contracts/__init__.py#L753), [PlaybillBodyRead](src/cruxible_client/contracts/__init__.py#L761), [PlaybillDocumentHistory](src/cruxible_client/contracts/__init__.py#L772), [PlaybillExplainResult](src/cruxible_client/contracts/__init__.py#L780), [PlaybillExplainUnsupportedDetail](src/cruxible_client/contracts/__init__.py#L797), [PlaybillSourceContext](src/cruxible_client/contracts/__init__.py#L811), [PlaybillSourceCheckResult](src/cruxible_client/contracts/__init__.py#L819), [PlaybillInstanceDecommissionResultV1](src/cruxible_client/contracts/__init__.py#L828), [PlaybillLedgerMirrorV1](src/cruxible_client/contracts/__init__.py#L843), [PlaybillSubjectIncomingClaimV1](src/cruxible_client/contracts/__init__.py#L870), [PlaybillSubjectIncomingGroupV1](src/cruxible_client/contracts/__init__.py#L880), [PlaybillSubjectView](src/cruxible_client/contracts/__init__.py#L890), [PlaybillSubjectList](src/cruxible_client/contracts/__init__.py#L901), [PlaybillSubjectHistory](src/cruxible_client/contracts/__init__.py#L909), [PlaybillClaimTypeView](src/cruxible_client/contracts/__init__.py#L917), [PlaybillClaimTypeList](src/cruxible_client/contracts/__init__.py#L929), [PlaybillClaimTypeProposalLint](src/cruxible_client/contracts/__init__.py#L937), [PlaybillClaimTypeInputProposalResult](src/cruxible_client/contracts/__init__.py#L944), [PlaybillClaimTypeMigrationResult](src/cruxible_client/contracts/__init__.py#L954), [PlaybillClaimTypeMigrationPreflight](src/cruxible_client/contracts/__init__.py#L971), [PlaybillClaimTypeMigrationResultV2](src/cruxible_client/contracts/__init__.py#L988), [PlaybillClaimTypeMigrationResultV3](src/cruxible_client/contracts/__init__.py#L1005), [PlaybillClaimView](src/cruxible_client/contracts/__init__.py#L1030), [PlaybillCaptureEvidenceKindAdmission](src/cruxible_client/contracts/__init__.py#L1040), [PlaybillCaptureAdmissionAccount](src/cruxible_client/contracts/__init__.py#L1052), [PlaybillClaimViewV2](src/cruxible_client/contracts/__init__.py#L1066), [PlaybillClaimList](src/cruxible_client/contracts/__init__.py#L1079), [PlaybillClaimHistory](src/cruxible_client/contracts/__init__.py#L1087), [PlaybillClaimRetirePreflight](src/cruxible_client/contracts/__init__.py#L1095), [PlaybillClaimRetireResult](src/cruxible_client/contracts/__init__.py#L1112), [PlaybillClaimExplanation](src/cruxible_client/contracts/__init__.py#L1126), [PlaybillClaimExplanationV2](src/cruxible_client/contracts/__init__.py#L1141), [PlaybillClaimExplanationV3](src/cruxible_client/contracts/__init__.py#L1158), [PlaybillCandidateStatus](src/cruxible_client/contracts/__init__.py#L1176), [PlaybillAuthoringIntentView](src/cruxible_client/contracts/__init__.py#L1199), [PlaybillAuthoringExampleResult](src/cruxible_client/contracts/__init__.py#L1206), [PlaybillAuthoringIntentList](src/cruxible_client/contracts/__init__.py#L1216), [PlaybillAuthoringPreflightResult](src/cruxible_client/contracts/__init__.py#L1223), [PlaybillAuthoringSubmitResult](src/cruxible_client/contracts/__init__.py#L1238), [PlaybillInsertionPrepareResult](src/cruxible_client/contracts/__init__.py#L1253), [PlaybillPublicationPrepareWarning](src/cruxible_client/contracts/__init__.py#L1274), [PlaybillInsertionConfirmResultV2](src/cruxible_client/contracts/__init__.py#L1292), [PlaybillInsertionAbandonResult](src/cruxible_client/contracts/__init__.py#L1301), [PlaybillBlockDeclareResultV1](src/cruxible_client/contracts/__init__.py#L1309), [PlaybillBlockDepublishResultV1](src/cruxible_client/contracts/__init__.py#L1330), [PlaybillQueryDefinitionView](src/cruxible_client/contracts/__init__.py#L1366), [PlaybillQueryDefinitionList](src/cruxible_client/contracts/__init__.py#L1378), [PlaybillQueryRun](src/cruxible_client/contracts/__init__.py#L1386), [PlaybillProcedureReadiness](src/cruxible_client/contracts/__init__.py#L1420), [PlaybillPolicyInForce](src/cruxible_client/contracts/__init__.py#L1437), [PlaybillPolicyInForceList](src/cruxible_client/contracts/__init__.py#L1451), [PlaybillProcedureBindResult](src/cruxible_client/contracts/__init__.py#L1459), [PlaybillProcedureRunState](src/cruxible_client/contracts/__init__.py#L1469), [PlaybillNextResult](src/cruxible_client/contracts/__init__.py#L1516), [PlaybillCurationListResult](src/cruxible_client/contracts/__init__.py#L1576), [PlaybillCurationActionResult](src/cruxible_client/contracts/__init__.py#L1592), [PlaybillAuditFactors](src/cruxible_client/contracts/__init__.py#L1604), [PlaybillAuditEvidenceRef](src/cruxible_client/contracts/__init__.py#L1623), [PlaybillAuditRow](src/cruxible_client/contracts/__init__.py#L1640), [PlaybillAuditScope](src/cruxible_client/contracts/__init__.py#L1657), [PlaybillAuditCoveredClaim](src/cruxible_client/contracts/__init__.py#L1665), [PlaybillAuditCoverage](src/cruxible_client/contracts/__init__.py#L1672), [PlaybillAuditCursor](src/cruxible_client/contracts/__init__.py#L1685), [PlaybillAuditResult](src/cruxible_client/contracts/__init__.py#L1697), [PlaybillSinceCursor](src/cruxible_client/contracts/__init__.py#L1750), [PlaybillSinceRequest](src/cruxible_client/contracts/__init__.py#L1776), [PlaybillSinceRow](src/cruxible_client/contracts/__init__.py#L1790), [PlaybillSinceResult](src/cruxible_client/contracts/__init__.py#L1824), [PlaybillDiscoveryResult](src/cruxible_client/contracts/__init__.py#L1845), [PlaybillProviderInterfaceImplementation](src/cruxible_client/contracts/__init__.py#L1854), [PlaybillProviderInterfaceEntry](src/cruxible_client/contracts/__init__.py#L1865), [PlaybillInterfaceInventory](src/cruxible_client/contracts/__init__.py#L1882), [PlaybillSearchResult](src/cruxible_client/contracts/__init__.py#L1891), [PlaybillContextCapsule](src/cruxible_client/contracts/__init__.py#L1906), [PlaybillCoverageResult](src/cruxible_client/contracts/__init__.py#L1932), [PlaybillFloorFile](src/cruxible_client/contracts/__init__.py#L1950), [PlaybillFloorExport](src/cruxible_client/contracts/__init__.py#L1957), [PlaybillWorkspaceFloorWriteResult](src/cruxible_client/contracts/__init__.py#L1975), [PlaybillWorkspaceAttachResultV1](src/cruxible_client/contracts/__init__.py#L1992), [PlaybillWorkspaceDetachResultV1](src/cruxible_client/contracts/__init__.py#L2005), [PlaybillWorkspaceFloorStatus](src/cruxible_client/contracts/__init__.py#L2023).

**`contracts.accepted_attestations`** — [AcceptedAttestationVerdictStatement](src/cruxible_client/contracts/accepted_attestations.py#L82), [AcceptedClaimAttestationEvidenceV1](src/cruxible_client/contracts/accepted_attestations.py#L112).

**`contracts.acquisition_policies`** — [SourceAcquisitionPolicyError](src/cruxible_client/contracts/acquisition_policies.py#L57), [InputAcquisitionRuleV1](src/cruxible_client/contracts/acquisition_policies.py#L71), [IndependentCoherenceV1](src/cruxible_client/contracts/acquisition_policies.py#L135), [BoundedWindowCoherenceV1](src/cruxible_client/contracts/acquisition_policies.py#L140), [DeclaredSnapshotGroupCoherenceV1](src/cruxible_client/contracts/acquisition_policies.py#L146), [SourceAcquisitionPolicyV1](src/cruxible_client/contracts/acquisition_policies.py#L171), [AcceptedSourceAcquisitionPolicyV1](src/cruxible_client/contracts/acquisition_policies.py#L244), [SourceAcquisitionPolicyLawResultV1](src/cruxible_client/contracts/acquisition_policies.py#L258), [AcquisitionCandidateV1](src/cruxible_client/contracts/acquisition_policies.py#L341), [AcquisitionInputDecisionV1](src/cruxible_client/contracts/acquisition_policies.py#L381), [SourceSelectionReceiptV1](src/cruxible_client/contracts/acquisition_policies.py#L396).

**`contracts.approval_policy`** — [ApprovalPolicyFormatError](src/cruxible_client/contracts/approval_policy.py#L29), [ApprovalPolicyV1](src/cruxible_client/contracts/approval_policy.py#L33).

**`contracts.artifacts`** — [ArtifactIdentity](src/cruxible_client/contracts/artifacts.py#L30), [ArtifactPin](src/cruxible_client/contracts/artifacts.py#L66), [ArtifactLifecycle](src/cruxible_client/contracts/artifacts.py#L86), [GovernedArtifactProtocol](src/cruxible_client/contracts/artifacts.py#L106), [ArtifactPathKind](src/cruxible_client/contracts/artifacts.py#L123), [ArtifactFormatTag](src/cruxible_client/contracts/artifacts.py#L130), [ArtifactFormatRegistry](src/cruxible_client/contracts/artifacts.py#L137), [ArtifactKindRegistry](src/cruxible_client/contracts/artifacts.py#L183).

**`contracts.attestations`** — [ApprovalStatement](src/cruxible_client/contracts/attestations.py#L37), [ApprovalAttestation](src/cruxible_client/contracts/attestations.py#L70), [ApprovalSubmission](src/cruxible_client/contracts/attestations.py#L91), [VerifiedApproval](src/cruxible_client/contracts/attestations.py#L125).

**`contracts.authoring.inputs`** — [LiteralObjectInput](src/cruxible_client/contracts/authoring/inputs.py#L74), [SubjectObjectInput](src/cruxible_client/contracts/authoring/inputs.py#L79), [ExactContentObjectInput](src/cruxible_client/contracts/authoring/inputs.py#L84), [SelfSourceInput](src/cruxible_client/contracts/authoring/inputs.py#L117), [WorkingSelectionInput](src/cruxible_client/contracts/authoring/inputs.py#L122), [ExistingCaptureInput](src/cruxible_client/contracts/authoring/inputs.py#L127), [AcceptedReferenceInput](src/cruxible_client/contracts/authoring/inputs.py#L138), [SlotReferenceInput](src/cruxible_client/contracts/authoring/inputs.py#L144), [CarriedContractReferenceInput](src/cruxible_client/contracts/authoring/inputs.py#L149), [CarriedContractInput](src/cruxible_client/contracts/authoring/inputs.py#L155), [ClaimDispositionInput](src/cruxible_client/contracts/authoring/inputs.py#L162), [ClaimInput](src/cruxible_client/contracts/authoring/inputs.py#L167), [ProcedureInput](src/cruxible_client/contracts/authoring/inputs.py#L183), [SubjectInput](src/cruxible_client/contracts/authoring/inputs.py#L202), [QueryDefinitionInput](src/cruxible_client/contracts/authoring/inputs.py#L207), [ApprovalPolicyInput](src/cruxible_client/contracts/authoring/inputs.py#L212), [ProcedureRuntimePolicyInput](src/cruxible_client/contracts/authoring/inputs.py#L217), [ClaimTypeInput](src/cruxible_client/contracts/authoring/inputs.py#L222), [ClaimTypeSuccessionInput](src/cruxible_client/contracts/authoring/inputs.py#L227), [ClaimRetirementInput](src/cruxible_client/contracts/authoring/inputs.py#L235), [ProcedureMandateInputV1](src/cruxible_client/contracts/authoring/inputs.py#L243), [ChangeSetInput](src/cruxible_client/contracts/authoring/inputs.py#L271), [AuthoringInputError](src/cruxible_client/contracts/authoring/inputs.py#L300).

**`contracts.authoring.models`** — [AuthoringReferenceExpectationV1](src/cruxible_client/contracts/authoring/models.py#L154), [AuthoringReferenceSuccessorV1](src/cruxible_client/contracts/authoring/models.py#L180), [AuthoringProgramOperationV1](src/cruxible_client/contracts/authoring/models.py#L190), [AuthoringProgramStampV1](src/cruxible_client/contracts/authoring/models.py#L210), [AuthoringExactContentObjectV1](src/cruxible_client/contracts/authoring/models.py#L310), [AuthoringClaimStatementV1](src/cruxible_client/contracts/authoring/models.py#L331), [AuthoringExistingClaimDispositionV1](src/cruxible_client/contracts/authoring/models.py#L368), [WorkingGitBlobCoordinateV1](src/cruxible_client/contracts/authoring/models.py#L379), [WorkingDigestCoordinateV1](src/cruxible_client/contracts/authoring/models.py#L401), [WorkingAnchorWindowV1](src/cruxible_client/contracts/authoring/models.py#L418), [WorkingSelectionObservationV1](src/cruxible_client/contracts/authoring/models.py#L445), [InsertionAnchorWindowV1](src/cruxible_client/contracts/authoring/models.py#L537), [InsertionTargetV2](src/cruxible_client/contracts/authoring/models.py#L596), [PublicationSourceObservationV2](src/cruxible_client/contracts/authoring/models.py#L647), [SelfSourceBodyV1](src/cruxible_client/contracts/authoring/models.py#L725), [ExistingCaptureCitationSourceV1](src/cruxible_client/contracts/authoring/models.py#L740), [ClaimAuthoringPayloadV1](src/cruxible_client/contracts/authoring/models.py#L765), [ClaimDependencyDraftsV1](src/cruxible_client/contracts/authoring/models.py#L813), [ClaimAuthoringPayloadV2](src/cruxible_client/contracts/authoring/models.py#L819), [ClaimAuthoringPayloadV3](src/cruxible_client/contracts/authoring/models.py#L824), [AuthoringArtifactReferenceV1](src/cruxible_client/contracts/authoring/models.py#L830), [AuthoringCandidateReferenceV1](src/cruxible_client/contracts/authoring/models.py#L846), [ResolutionContractAuthoringPayloadV1](src/cruxible_client/contracts/authoring/models.py#L855), [AttestationAuthoringPayloadV1](src/cruxible_client/contracts/authoring/models.py#L862), [SubjectAuthoringPayloadV1](src/cruxible_client/contracts/authoring/models.py#L871), [QueryDefinitionAuthoringPayloadV1](src/cruxible_client/contracts/authoring/models.py#L876), [ApprovalPolicyAuthoringPayloadV1](src/cruxible_client/contracts/authoring/models.py#L883), [ProcedureRuntimePolicyAuthoringPayloadV1](src/cruxible_client/contracts/authoring/models.py#L890), [ProcedureMandateAuthoringPayloadV1](src/cruxible_client/contracts/authoring/models.py#L897), [CaptureContractAuthoringPayloadV1](src/cruxible_client/contracts/authoring/models.py#L913), [SourceAcquisitionPolicyAuthoringPayloadV1](src/cruxible_client/contracts/authoring/models.py#L922), [LineAuthoringPayloadV1](src/cruxible_client/contracts/authoring/models.py#L931), [ProcedureAuthoringPayloadV1](src/cruxible_client/contracts/authoring/models.py#L965), [ProcedureAuthoringPayloadV2](src/cruxible_client/contracts/authoring/models.py#L984), [ClaimTypeAuthoringPayloadV1](src/cruxible_client/contracts/authoring/models.py#L1024), [ClaimTypeSuccessionDependentV1](src/cruxible_client/contracts/authoring/models.py#L1057), [ClaimTypeSuccessionMemberV1](src/cruxible_client/contracts/authoring/models.py#L1137), [ClaimRetirementMemberV1](src/cruxible_client/contracts/authoring/models.py#L1192), [ChangeSetAuthoringPayloadV1](src/cruxible_client/contracts/authoring/models.py#L1345), [RepairAlternativeV1](src/cruxible_client/contracts/authoring/models.py#L1471), [AuthoringDiagnosticV1](src/cruxible_client/contracts/authoring/models.py#L1488), [BlockedCheckV1](src/cruxible_client/contracts/authoring/models.py#L1517), [DiagnosticFrontierLimitsV1](src/cruxible_client/contracts/authoring/models.py#L1530), [DiagnosticFrontierV1](src/cruxible_client/contracts/authoring/models.py#L1538), [AcceptanceConditionV1](src/cruxible_client/contracts/authoring/models.py#L1576), [CandidateStatusV1](src/cruxible_client/contracts/authoring/models.py#L1583), [PublicationPreparationV2](src/cruxible_client/contracts/authoring/models.py#L1644), [InsertionConfirmationObservationV2](src/cruxible_client/contracts/authoring/models.py#L1736), [InsertionTerminalTombstoneV2](src/cruxible_client/contracts/authoring/models.py#L1770), [InsertionExpectationV2](src/cruxible_client/contracts/authoring/models.py#L1856), [PreflightCertificateV1](src/cruxible_client/contracts/authoring/models.py#L2006), [PreflightResultV1](src/cruxible_client/contracts/authoring/models.py#L2109), [ChangeSetClaimIdentityV1](src/cruxible_client/contracts/authoring/models.py#L2131), [AuthoringIntentV1](src/cruxible_client/contracts/authoring/models.py#L2145), [AuthoringIntentV2](src/cruxible_client/contracts/authoring/models.py#L2340), [AuthoringIntentViewV1](src/cruxible_client/contracts/authoring/models.py#L2361), [AuthoringIntentListV1](src/cruxible_client/contracts/authoring/models.py#L2366), [AuthoringIntentCreateRequestV1](src/cruxible_client/contracts/authoring/models.py#L2371), [AuthoringIntentCompileRequestV1](src/cruxible_client/contracts/authoring/models.py#L2378), [AuthoringIntentCreateRequestV2](src/cruxible_client/contracts/authoring/models.py#L2386), [AuthoringIntentCompileRequestV2](src/cruxible_client/contracts/authoring/models.py#L2402), [AuthoringIntentCreateRequestV3](src/cruxible_client/contracts/authoring/models.py#L2419), [AuthoringIntentCompileRequestV3](src/cruxible_client/contracts/authoring/models.py#L2436), [AuthoringIntentPreflightRequestV1](src/cruxible_client/contracts/authoring/models.py#L2454), [AuthoringIntentSubmitRequestV1](src/cruxible_client/contracts/authoring/models.py#L2460), [AuthoringSubmitMemberV1](src/cruxible_client/contracts/authoring/models.py#L2466), [AuthoringSubmitResultV1](src/cruxible_client/contracts/authoring/models.py#L2477), [InsertionPrepareRequestV2](src/cruxible_client/contracts/authoring/models.py#L2504), [PublicationPrepareWarningV1](src/cruxible_client/contracts/authoring/models.py#L2512), [InsertionPrepareResultV2](src/cruxible_client/contracts/authoring/models.py#L2537), [InsertionConfirmRequestV2](src/cruxible_client/contracts/authoring/models.py#L2596), [InsertionConfirmResultV2](src/cruxible_client/contracts/authoring/models.py#L2602), [InsertionAbandonRequestV1](src/cruxible_client/contracts/authoring/models.py#L2609), [InsertionAbandonResultV1](src/cruxible_client/contracts/authoring/models.py#L2613), [PlaybillBlockSyncSuccessorCandidateV1](src/cruxible_client/contracts/authoring/models.py#L2637), [PlaybillBlockSyncReadRequestV1](src/cruxible_client/contracts/authoring/models.py#L2657), [ProjectionDependencyIssueV1](src/cruxible_client/contracts/authoring/models.py#L2677), [PlaybillBlockSyncReadResultV1](src/cruxible_client/contracts/authoring/models.py#L2684), [PlaybillProjectionCheckRequestV1](src/cruxible_client/contracts/authoring/models.py#L2736), [PlaybillProjectionCheckResultV1](src/cruxible_client/contracts/authoring/models.py#L2748), [PlaybillBlockSyncItemV1](src/cruxible_client/contracts/authoring/models.py#L2788), [PlaybillBlockSyncResultV1](src/cruxible_client/contracts/authoring/models.py#L2834).

**`contracts.authoring_profiles`** — [AuthoringProfileError](src/cruxible_client/contracts/authoring_profiles.py#L39), [ClaimTypeProfileDefinitionV1](src/cruxible_client/contracts/authoring_profiles.py#L47), [ClaimTypeProfileInputV1](src/cruxible_client/contracts/authoring_profiles.py#L137), [ClaimTypeExpansionEvidenceV1](src/cruxible_client/contracts/authoring_profiles.py#L163), [ClaimTypeExpansionResultV1](src/cruxible_client/contracts/authoring_profiles.py#L209).

**`contracts.candidates`** — [SemanticCandidate](src/cruxible_client/contracts/candidates.py#L73), [SemanticCandidateV2](src/cruxible_client/contracts/candidates.py#L112), [CandidateRecord](src/cruxible_client/contracts/candidates.py#L176), [CandidateMemberEvidence](src/cruxible_client/contracts/candidates.py#L236), [DependencyProofReferenceV1](src/cruxible_client/contracts/candidates.py#L278), [LawEvaluationCoordinateV1](src/cruxible_client/contracts/candidates.py#L308), [MemberLawEvaluationV2](src/cruxible_client/contracts/candidates.py#L344), [CandidateMemberLawEvidenceV2](src/cruxible_client/contracts/candidates.py#L420), [ClosureProofV2](src/cruxible_client/contracts/candidates.py#L504), [ClosureProofV3](src/cruxible_client/contracts/candidates.py#L523), [CandidateRecordV2](src/cruxible_client/contracts/candidates.py#L637), [CandidateRecordV3](src/cruxible_client/contracts/candidates.py#L690).

**`contracts.canonical`** — [ArtifactCodec](src/cruxible_client/contracts/canonical.py#L24), [CanonicalDigester](src/cruxible_client/contracts/canonical.py#L176), [Sha256Value](src/cruxible_client/contracts/canonical.py#L184), [BootstrapRoot](src/cruxible_client/contracts/canonical.py#L210), [ArtifactDigest](src/cruxible_client/contracts/canonical.py#L214), [CandidateDigest](src/cruxible_client/contracts/canonical.py#L218), [AcceptanceLawDigest](src/cruxible_client/contracts/canonical.py#L222), [ApprovalDigest](src/cruxible_client/contracts/canonical.py#L226), [ProposalDigest](src/cruxible_client/contracts/canonical.py#L230), [SemanticDiffDigest](src/cruxible_client/contracts/canonical.py#L234), [ChangeSetDigest](src/cruxible_client/contracts/canonical.py#L238), [SemanticManifestRoot](src/cruxible_client/contracts/canonical.py#L242), [MerkleNodeDigest](src/cruxible_client/contracts/canonical.py#L246), [SemanticMerkleRoot](src/cruxible_client/contracts/canonical.py#L250), [DependencyEdgeRoot](src/cruxible_client/contracts/canonical.py#L265), [SemanticRoot](src/cruxible_client/contracts/canonical.py#L285), [GenerationRoot](src/cruxible_client/contracts/canonical.py#L289), [LogicalDigest](src/cruxible_client/contracts/canonical.py#L293), [CasDigest](src/cruxible_client/contracts/canonical.py#L297).

**`contracts.capture_journal`** — [CaptureJournalError](src/cruxible_client/contracts/capture_journal.py#L26), [CaptureLandingEventV1](src/cruxible_client/contracts/capture_journal.py#L34), [CaptureLandingEventV2](src/cruxible_client/contracts/capture_journal.py#L81), [CaptureCursorV1](src/cruxible_client/contracts/capture_journal.py#L143), [CaptureLandingJournalProtocol](src/cruxible_client/contracts/capture_journal.py#L243), [InMemoryCaptureLandingJournal](src/cruxible_client/contracts/capture_journal.py#L256).

**`contracts.capture_reads`** — [CaptureReadRequestV1](src/cruxible_client/contracts/capture_reads.py#L15), [CaptureReadV1](src/cruxible_client/contracts/capture_reads.py#L28).

**`contracts.captures`** — [CaptureFormatError](src/cruxible_client/contracts/captures.py#L120), [CanonicalDurationV1](src/cruxible_client/contracts/captures.py#L141), [CaptureSelectionBudgetV1](src/cruxible_client/contracts/captures.py#L146), [CaptureRetentionErasurePolicyV1](src/cruxible_client/contracts/captures.py#L153), [CaptureContractV1](src/cruxible_client/contracts/captures.py#L194), [CaptureComponentRegistry](src/cruxible_client/contracts/captures.py#L319), [AcceptedCaptureContract](src/cruxible_client/contracts/captures.py#L599), [CaptureContractLawResult](src/cruxible_client/contracts/captures.py#L612), [CaptureRunCoordinateV1](src/cruxible_client/contracts/captures.py#L786), [CaptureRunCoordinateV2](src/cruxible_client/contracts/captures.py#L808), [SourceEffectiveTimeV1](src/cruxible_client/contracts/captures.py#L832), [CaptureEnvelopeV1](src/cruxible_client/contracts/captures.py#L849), [ProviderInvocationCaptureEvidenceV1](src/cruxible_client/contracts/captures.py#L899), [ProcedureEgressCaptureEvidenceV1](src/cruxible_client/contracts/captures.py#L968), [ProcedureProducerReceiptProtocol](src/cruxible_client/contracts/captures.py#L984), [ProducerReceiptResolverProtocol](src/cruxible_client/contracts/captures.py#L1015), [ProviderProducerReceiptResolution](src/cruxible_client/contracts/captures.py#L1025), [ProviderResultToExternalCaptureV1](src/cruxible_client/contracts/captures.py#L1052), [CaptureEnvelopeV2](src/cruxible_client/contracts/captures.py#L1121), [InputReceiptSetManifestV1](src/cruxible_client/contracts/captures.py#L1241), [DirectClaimSourceV1](src/cruxible_client/contracts/captures.py#L1285), [DirectByteSpanSelectionV1](src/cruxible_client/contracts/captures.py#L1310), [DirectExternalSelectionV1](src/cruxible_client/contracts/captures.py#L1327), [DirectForeignSourceSelectionV1](src/cruxible_client/contracts/captures.py#L1362), [CaptureObjectStoreProtocol](src/cruxible_client/contracts/captures.py#L1410), [LedgerMaterialResolverProtocol](src/cruxible_client/contracts/captures.py#L1419), [DirectCaptureBuildResult](src/cruxible_client/contracts/captures.py#L1423), [CaptureBuildResult](src/cruxible_client/contracts/captures.py#L1585).

**`contracts.cas_contracts`** — [BodyAccessContext](src/cruxible_client/contracts/cas_contracts.py#L17), [CasObjectMetadata](src/cruxible_client/contracts/cas_contracts.py#L25), [BodyProjectionProtocol](src/cruxible_client/contracts/cas_contracts.py#L32).

**`contracts.claim_attestation_store`** — [ClaimAttestationStoreManifestV1](src/cruxible_client/contracts/claim_attestation_store.py#L37), [ClaimAttestationEventPayloadV1](src/cruxible_client/contracts/claim_attestation_store.py#L53), [ClaimAttestationEventV1](src/cruxible_client/contracts/claim_attestation_store.py#L115), [ClaimAttestationPartitionGenesisV1](src/cruxible_client/contracts/claim_attestation_store.py#L135), [ClaimAttestationPartitionHeadV1](src/cruxible_client/contracts/claim_attestation_store.py#L153), [ClaimAttestationHeadMapEntryV1](src/cruxible_client/contracts/claim_attestation_store.py#L171), [ClaimAttestationHeadMapNodeV1](src/cruxible_client/contracts/claim_attestation_store.py#L184), [ClaimAttestationPublishedRootV1](src/cruxible_client/contracts/claim_attestation_store.py#L207), [ClaimAttestationPublishedPointerV1](src/cruxible_client/contracts/claim_attestation_store.py#L241), [ClaimAttestationOutstandingMembershipV1](src/cruxible_client/contracts/claim_attestation_store.py#L251), [ClaimAttestationAcceleratorV1](src/cruxible_client/contracts/claim_attestation_store.py#L262).

**`contracts.claim_attestations`** — [ClaimAttestationError](src/cruxible_client/contracts/claim_attestations.py#L46), [ClaimAttestationStatement](src/cruxible_client/contracts/claim_attestations.py#L54), [ClaimAttestation](src/cruxible_client/contracts/claim_attestations.py#L118), [VerifiedClaimAttestationV1](src/cruxible_client/contracts/claim_attestations.py#L232), [ClaimAttestationStatementV2](src/cruxible_client/contracts/claim_attestations.py#L261), [ClaimAttestationV2](src/cruxible_client/contracts/claim_attestations.py#L324), [ClaimAttestationCaptureReferenceV1](src/cruxible_client/contracts/claim_attestations.py#L340), [PreparedClaimAttestationRequestV1](src/cruxible_client/contracts/claim_attestations.py#L353), [ClaimAttestationResolvedArtifactV1](src/cruxible_client/contracts/claim_attestations.py#L404), [VerifiedClaimAttestationV2](src/cruxible_client/contracts/claim_attestations.py#L419), [ClaimAttestationAppendRequestV1](src/cruxible_client/contracts/claim_attestations.py#L475), [ClaimAttestationAppendResultV1](src/cruxible_client/contracts/claim_attestations.py#L515).

**`contracts.claim_reads`** — [ClaimReadBatchRequestV1](src/cruxible_client/contracts/claim_reads.py#L16), [ClaimReadBatchResultV1](src/cruxible_client/contracts/claim_reads.py#L43), [ClaimBackingsRequestV1](src/cruxible_client/contracts/claim_reads.py#L52), [ClaimBackingsResultV1](src/cruxible_client/contracts/claim_reads.py#L58).

**`contracts.claim_type_structure`** — [ClaimTypeStructure](src/cruxible_client/contracts/claim_type_structure.py#L35), [ClaimTypeStructuralCheck](src/cruxible_client/contracts/claim_type_structure.py#L130).

**`contracts.claim_types`** — [ClaimTypeFormatError](src/cruxible_client/contracts/claim_types.py#L48), [ClaimTypeFreshnessHorizonInvalid](src/cruxible_client/contracts/claim_types.py#L52), [ClaimFreshnessDurationV1](src/cruxible_client/contracts/claim_types.py#L60), [ClaimEvidenceFreshnessV1](src/cruxible_client/contracts/claim_types.py#L65), [ClaimAttestationConsequenceRuleV1](src/cruxible_client/contracts/claim_types.py#L76), [ClaimAttestationConsequencePolicyV1](src/cruxible_client/contracts/claim_types.py#L92), [ClaimType](src/cruxible_client/contracts/claim_types.py#L109), [AcceptedClaimType](src/cruxible_client/contracts/claim_types.py#L336), [ClaimTypeLawResult](src/cruxible_client/contracts/claim_types.py#L349).

**`contracts.claim_verdicts`** — [ClaimAdjudicationRuleV1](src/cruxible_client/contracts/claim_verdicts.py#L77), [CaptureVerdictEvidenceV1](src/cruxible_client/contracts/claim_verdicts.py#L140), [EvidenceControlComponentV1](src/cruxible_client/contracts/claim_verdicts.py#L176), [ClaimVerdictResultV1](src/cruxible_client/contracts/claim_verdicts.py#L284), [EvidenceFreshnessExpirationV1](src/cruxible_client/contracts/claim_verdicts.py#L317), [ClaimVerdictResultV2](src/cruxible_client/contracts/claim_verdicts.py#L345).

**`contracts.claims`** — [ClaimFormatError](src/cruxible_client/contracts/claims.py#L100), [ClaimUnsupportedFormatError](src/cruxible_client/contracts/claims.py#L104), [LiteralClaimObject](src/cruxible_client/contracts/claims.py#L112), [SubjectClaimObject](src/cruxible_client/contracts/claims.py#L122), [ExactContentClaimObject](src/cruxible_client/contracts/claims.py#L127), [ClaimStatement](src/cruxible_client/contracts/claims.py#L151), [ClaimStatementCardV1](src/cruxible_client/contracts/claims.py#L195), [ClaimReferentContext](src/cruxible_client/contracts/claims.py#L229), [ClaimBacking](src/cruxible_client/contracts/claims.py#L275), [ClaimCitationV1](src/cruxible_client/contracts/claims.py#L357), [LegacyCitationReferenceV1](src/cruxible_client/contracts/claims.py#L414), [ClaimBackingV2](src/cruxible_client/contracts/claims.py#L455), [ClaimArtifactV2](src/cruxible_client/contracts/claims.py#L515), [ClaimRetirementAttributionV1](src/cruxible_client/contracts/claims.py#L558), [ClaimRetireDependentV1](src/cruxible_client/contracts/claims.py#L565), [ClaimRetireRequestV1](src/cruxible_client/contracts/claims.py#L594), [ClaimArtifactV3](src/cruxible_client/contracts/claims.py#L629), [AcceptedClaim](src/cruxible_client/contracts/claims.py#L764), [ClaimLawEvidenceV1](src/cruxible_client/contracts/claims.py#L808), [ClaimLawEvidenceV2](src/cruxible_client/contracts/claims.py#L867), [ClaimLawResult](src/cruxible_client/contracts/claims.py#L942), [CitedSourceWindow](src/cruxible_client/contracts/claims.py#L1087), [CaptureEvidenceKindAdmission](src/cruxible_client/contracts/claims.py#L1346).

**`contracts.compiler_upgrade`** — [CompilerUpgradeV1](src/cruxible_client/contracts/compiler_upgrade.py#L14).

**`contracts.declared_blocks`** — [PlaybillPresentationPolicyV1](src/cruxible_client/contracts/declared_blocks.py#L64), [PlaybillProjectionAdvisoryPolicyV1](src/cruxible_client/contracts/declared_blocks.py#L81), [PlaybillPresentationPolicyV2](src/cruxible_client/contracts/declared_blocks.py#L88), [PlaybillProjectionCoverageBindingV1](src/cruxible_client/contracts/declared_blocks.py#L129), [PlaybillProjectionCoverageObservationV1](src/cruxible_client/contracts/declared_blocks.py#L146), [PlaybillReviewWorkspaceObservationV1](src/cruxible_client/contracts/declared_blocks.py#L188), [ProjectionClaimBackingV1](src/cruxible_client/contracts/declared_blocks.py#L208), [ProjectionArtifactBackingV1](src/cruxible_client/contracts/declared_blocks.py#L226), [ProjectionResolvedParameterBindingV1](src/cruxible_client/contracts/declared_blocks.py#L248), [ProjectionQueryBackingV1](src/cruxible_client/contracts/declared_blocks.py#L272), [ProjectionBlockStampV1](src/cruxible_client/contracts/declared_blocks.py#L346), [ProjectionBlockStampV2](src/cruxible_client/contracts/declared_blocks.py#L356), [ProjectionMarkerSummaryV1](src/cruxible_client/contracts/declared_blocks.py#L367), [ProjectionMarkerError](src/cruxible_client/contracts/declared_blocks.py#L386), [ProjectionProcessingPolicyV1](src/cruxible_client/contracts/declared_blocks.py#L393), [ProjectionProcessingLimitExceeded](src/cruxible_client/contracts/declared_blocks.py#L427), [ProjectionBootstrapUnstampedError](src/cruxible_client/contracts/declared_blocks.py#L454), [ParsedProjectionBlock](src/cruxible_client/contracts/declared_blocks.py#L459), [ProjectionWindow](src/cruxible_client/contracts/declared_blocks.py#L674).

**`contracts.diagnostics`** — [LocalDraftEdit](src/cruxible_client/contracts/diagnostics.py#L32), [GovernedOperationReference](src/cruxible_client/contracts/diagnostics.py#L70), [CompilerDiagnostic](src/cruxible_client/contracts/diagnostics.py#L78).

**`contracts.discovery`** — [DiscoveryHintsV1](src/cruxible_client/contracts/discovery.py#L123), [SemanticReuseInterfaceV1](src/cruxible_client/contracts/discovery.py#L138), [ProposedSemanticInterfaceV1](src/cruxible_client/contracts/discovery.py#L173), [ReuseMatchV1](src/cruxible_client/contracts/discovery.py#L200), [ReuseCandidateV1](src/cruxible_client/contracts/discovery.py#L206), [ReuseDispositionV1](src/cruxible_client/contracts/discovery.py#L229), [DistinctRelationMemberV1](src/cruxible_client/contracts/discovery.py#L243), [VocabularyReuseRequestV1](src/cruxible_client/contracts/discovery.py#L268), [VocabularyReuseLawEvidenceV1](src/cruxible_client/contracts/discovery.py#L277), [DescriptorClaimTypeSeedV1](src/cruxible_client/contracts/discovery.py#L479), [DescriptorAuthorityContextV1](src/cruxible_client/contracts/discovery.py#L553), [DescriptorAuthorityResultV1](src/cruxible_client/contracts/discovery.py#L574), [DiscoveryBudgetV1](src/cruxible_client/contracts/discovery.py#L618), [ExpansionBudgetV1](src/cruxible_client/contracts/discovery.py#L624), [DiscoveryMatchBasisV1](src/cruxible_client/contracts/discovery.py#L631), [DiscoveryHitV1](src/cruxible_client/contracts/discovery.py#L636), [DiscoveryRequestV1](src/cruxible_client/contracts/discovery.py#L653), [DiscoveryPageV1](src/cruxible_client/contracts/discovery.py#L669), [ExpandRequestV1](src/cruxible_client/contracts/discovery.py#L680), [ContextCapsuleV1](src/cruxible_client/contracts/discovery.py#L689), [ContextMaterialV1](src/cruxible_client/contracts/discovery.py#L730).

**`contracts.documents`** — [DocumentLink](src/cruxible_client/contracts/documents.py#L64), [DocumentPin](src/cruxible_client/contracts/documents.py#L79), [DocumentAuthority](src/cruxible_client/contracts/documents.py#L101), [DocumentLifecycle](src/cruxible_client/contracts/documents.py#L105), [DocumentShell](src/cruxible_client/contracts/documents.py#L111), [DocumentArtifactAdapter](src/cruxible_client/contracts/documents.py#L210), [BodyVerifierProtocol](src/cruxible_client/contracts/documents.py#L319), [AcceptedDocument](src/cruxible_client/contracts/documents.py#L323), [DocumentLawResult](src/cruxible_client/contracts/documents.py#L335).

**`contracts.errors`** — [PlaybillError](src/cruxible_client/contracts/errors.py#L11), [CanonicalEncodingError](src/cruxible_client/contracts/errors.py#L15), [MerkleIntegrityError](src/cruxible_client/contracts/errors.py#L19), [PlaybillFormatError](src/cruxible_client/contracts/errors.py#L23), [PlaybillSinceRequestInvalid](src/cruxible_client/contracts/errors.py#L27), [ClaimAttestationRequestInvalid](src/cruxible_client/contracts/errors.py#L69), [PlaybillInstanceIncompatiblePrereleaseContent](src/cruxible_client/contracts/errors.py#L98), [PlaybillReseedRequired](src/cruxible_client/contracts/errors.py#L111), [PlaybillInstanceDecommissioned](src/cruxible_client/contracts/errors.py#L123), [SemanticDeltaLimitError](src/cruxible_client/contracts/errors.py#L144), [PlaybillDeprecatedWriteError](src/cruxible_client/contracts/errors.py#L150), [PlaybillBootstrapError](src/cruxible_client/contracts/errors.py#L160), [PlaybillObjectFormatConflict](src/cruxible_client/contracts/errors.py#L164), [PlaybillGitError](src/cruxible_client/contracts/errors.py#L183), [PlaybillKeyError](src/cruxible_client/contracts/errors.py#L187), [PlaybillCasError](src/cruxible_client/contracts/errors.py#L191), [PlaybillJournalError](src/cruxible_client/contracts/errors.py#L195), [PlaybillJournalConflictError](src/cruxible_client/contracts/errors.py#L199), [PlaybillJournalIntegrityError](src/cruxible_client/contracts/errors.py#L209), [PlaybillExecutionError](src/cruxible_client/contracts/errors.py#L217), [DocumentFormatError](src/cruxible_client/contracts/errors.py#L221), [DocumentNotFoundError](src/cruxible_client/contracts/errors.py#L225), [SubjectFormatError](src/cruxible_client/contracts/errors.py#L229), [SubjectNotFoundError](src/cruxible_client/contracts/errors.py#L233), [ClaimNotFoundError](src/cruxible_client/contracts/errors.py#L237), [ProposalAdmissionError](src/cruxible_client/contracts/errors.py#L241), [ProposalWithdrawnError](src/cruxible_client/contracts/errors.py#L245), [ProposalNotFoundError](src/cruxible_client/contracts/errors.py#L276), [ProposalSelectorAmbiguousError](src/cruxible_client/contracts/errors.py#L293), [ProposalContentUnavailable](src/cruxible_client/contracts/errors.py#L323), [ProposalReadmitRequiresResubmission](src/cruxible_client/contracts/errors.py#L336), [ProposalActivationRequestInvalid](src/cruxible_client/contracts/errors.py#L349), [ProposalIntegrityError](src/cruxible_client/contracts/errors.py#L355), [ProposalEvaluationIntegrityError](src/cruxible_client/contracts/errors.py#L359), [ApprovalIntegrityError](src/cruxible_client/contracts/errors.py#L363), [PrincipalIntegrityError](src/cruxible_client/contracts/errors.py#L367), [SettlementIntegrityError](src/cruxible_client/contracts/errors.py#L371), [ReplayCheckpointError](src/cruxible_client/contracts/errors.py#L375), [ProjectionError](src/cruxible_client/contracts/errors.py#L379), [ProjectionCoordinateError](src/cruxible_client/contracts/errors.py#L383), [ProjectionFormatError](src/cruxible_client/contracts/errors.py#L387), [ProjectionPublicationError](src/cruxible_client/contracts/errors.py#L391), [ProjectionIntegrityError](src/cruxible_client/contracts/errors.py#L395).

**`contracts.governance`** — [ApprovalRequirement](src/cruxible_client/contracts/governance.py#L34), [AcceptanceLawCoordinate](src/cruxible_client/contracts/governance.py#L63).

**`contracts.laws`** — [InstalledAcceptanceLaw](src/cruxible_client/contracts/laws.py#L275), [AcceptanceLawRegistry](src/cruxible_client/contracts/laws.py#L284).

**`contracts.ledger_mirror`** — [PlaybillLedgerMirrorUrlInvalid](src/cruxible_client/contracts/ledger_mirror.py#L46), [PlaybillLedgerMirrorUnset](src/cruxible_client/contracts/ledger_mirror.py#L56).

**`contracts.merkle`** — [MerkleDomainFamily](src/cruxible_client/contracts/merkle.py#L68), [MerkleNode](src/cruxible_client/contracts/merkle.py#L101), [MerkleTree](src/cruxible_client/contracts/merkle.py#L200).

**`contracts.persistent`** — [PersistentMap](src/cruxible_client/contracts/persistent.py#L140), [MapMutation](src/cruxible_client/contracts/persistent.py#L237).

**`contracts.policies`** — [CorroborationRequirementV1](src/cruxible_client/contracts/policies.py#L75), [FreezeRequirementV1](src/cruxible_client/contracts/policies.py#L93), [ClaimAdmissionPolicyV1](src/cruxible_client/contracts/policies.py#L137), [ClaimResolutionPolicyV1](src/cruxible_client/contracts/policies.py#L162), [ClaimEvidenceAdmissionRuleV1](src/cruxible_client/contracts/policies.py#L190), [ClaimEvidenceAdmissionPolicyV1](src/cruxible_client/contracts/policies.py#L236), [ClaimCorroborationResultV1](src/cruxible_client/contracts/policies.py#L253), [ClaimAdmissionEvaluationAccountV1](src/cruxible_client/contracts/policies.py#L287), [ClaimAdmissionCandidateContextV1](src/cruxible_client/contracts/policies.py#L321), [ClaimAdmissionCandidateResultV1](src/cruxible_client/contracts/policies.py#L355), [EvidenceAdmissionInputV1](src/cruxible_client/contracts/policies.py#L415), [ClaimEvidenceAdmissionResultV1](src/cruxible_client/contracts/policies.py#L448), [ClaimEvidenceAdmissionTrace](src/cruxible_client/contracts/policies.py#L458), [ResolutionContenderV1](src/cruxible_client/contracts/policies.py#L585), [ClaimResolutionResultV1](src/cruxible_client/contracts/policies.py#L610).

**`contracts.predictions`** — [PlaybillPredictRequestV2](src/cruxible_client/contracts/predictions.py#L41), [PlaybillPredictResultV2](src/cruxible_client/contracts/predictions.py#L46), [ObservationSettlementEvidenceV2](src/cruxible_client/contracts/predictions.py#L54), [TerminalSettlementEvidenceV2](src/cruxible_client/contracts/predictions.py#L61), [PlaybillSettleRequestV2](src/cruxible_client/contracts/predictions.py#L82), [PlaybillSettleResultV2](src/cruxible_client/contracts/predictions.py#L89).

**`contracts.principals`** — [PrincipalRegistrySnapshot](src/cruxible_client/contracts/principals.py#L19).

**`contracts.procedure_mandates`** — [ProcedureMandateError](src/cruxible_client/contracts/procedure_mandates.py#L35), [ProcedureMandateV1](src/cruxible_client/contracts/procedure_mandates.py#L58), [AcceptedProcedureMandateV1](src/cruxible_client/contracts/procedure_mandates.py#L142), [ProcedureMandateLawResultV1](src/cruxible_client/contracts/procedure_mandates.py#L156), [ProcedureMandateInvocationV1](src/cruxible_client/contracts/procedure_mandates.py#L248), [ProcedureMandateEvaluationV1](src/cruxible_client/contracts/procedure_mandates.py#L286).

**`contracts.procedure_runtime_policy`** — [ProcedureRuntimePolicyFormatError](src/cruxible_client/contracts/procedure_runtime_policy.py#L25), [ProcedureRuntimePolicyV1](src/cruxible_client/contracts/procedure_runtime_policy.py#L29).

**`contracts.procedures.artifacts`** — [ProcedureFormatError](src/cruxible_client/contracts/procedures/artifacts.py#L58), [ProcedureArtifactV1](src/cruxible_client/contracts/procedures/artifacts.py#L74), [ProcedureOwnedContractV1](src/cruxible_client/contracts/procedures/artifacts.py#L138), [ProcedureArtifactV2](src/cruxible_client/contracts/procedures/artifacts.py#L189), [AcceptedProcedureV1](src/cruxible_client/contracts/procedures/artifacts.py#L341), [ProcedureLawResultV1](src/cruxible_client/contracts/procedures/artifacts.py#L355).

**`contracts.procedures.authoring`** — [GuardBuilderCommonV1](src/cruxible_client/contracts/procedures/authoring.py#L52), [AcceptedClaimGuardBuilderV1](src/cruxible_client/contracts/procedures/authoring.py#L107), [SourceCaptureGuardBuilderV1](src/cruxible_client/contracts/procedures/authoring.py#L130), [ExhaustGuardBuilderV1](src/cruxible_client/contracts/procedures/authoring.py#L157), [BuilderSourceMappingV1](src/cruxible_client/contracts/procedures/authoring.py#L177), [ProcedureGuardExpansionV1](src/cruxible_client/contracts/procedures/authoring.py#L185).

**`contracts.procedures.closure`** — [ProcedurePinClosureError](src/cruxible_client/contracts/procedures/closure.py#L21), [LineSlotBindingV1](src/cruxible_client/contracts/procedures/closure.py#L29), [ProviderExtrasEnvironmentPinMapV1](src/cruxible_client/contracts/procedures/closure.py#L35), [ProviderImplementationClosureV1](src/cruxible_client/contracts/procedures/closure.py#L52), [ProcedureSlotInterfaceV1](src/cruxible_client/contracts/procedures/closure.py#L78), [ClosedProcedurePinsV1](src/cruxible_client/contracts/procedures/closure.py#L110).

**`contracts.procedures.contract_schema`** — [PropertySchema](src/cruxible_client/contracts/procedures/contract_schema.py#L31), [ContractSchema](src/cruxible_client/contracts/procedures/contract_schema.py#L96).

**`contracts.procedures.contracts`** — [ProcedureContractValidationError](src/cruxible_client/contracts/procedures/contracts.py#L23), [ProcedureContractItemBudgetExceeded](src/cruxible_client/contracts/procedures/contracts.py#L38), [ProcedureContractListObservation](src/cruxible_client/contracts/procedures/contracts.py#L51), [ValidatedProcedureContract](src/cruxible_client/contracts/procedures/contracts.py#L57), [OwnedProcedureContractValidator](src/cruxible_client/contracts/procedures/contracts.py#L267).

**`contracts.procedures.graph`** — [ProcedureGraphFormatError](src/cruxible_client/contracts/procedures/graph.py#L42), [ProcedureGraphV3](src/cruxible_client/contracts/procedures/graph.py#L47), [ProcedureNodeDigestsV3](src/cruxible_client/contracts/procedures/graph.py#L61).

**`contracts.procedures.line_specs`** — [LineSpecFormatError](src/cruxible_client/contracts/procedures/line_specs.py#L76), [CadenceTriggerPolicyV1](src/cruxible_client/contracts/procedures/line_specs.py#L89), [CaptureLandingTriggerPolicyV1](src/cruxible_client/contracts/procedures/line_specs.py#L110), [WindowCloseTriggerPolicyV1](src/cruxible_client/contracts/procedures/line_specs.py#L121), [CaptureLandingTriggerPolicyV2](src/cruxible_client/contracts/procedures/line_specs.py#L135), [WindowCloseTriggerPolicyV2](src/cruxible_client/contracts/procedures/line_specs.py#L141), [ManualTriggerPolicyV1](src/cruxible_client/contracts/procedures/line_specs.py#L147), [LineSpecV1](src/cruxible_client/contracts/procedures/line_specs.py#L200), [LineSpecV2](src/cruxible_client/contracts/procedures/line_specs.py#L280), [LineSpecV3](src/cruxible_client/contracts/procedures/line_specs.py#L305), [AcceptedLineSpecV1](src/cruxible_client/contracts/procedures/line_specs.py#L423), [LineSpecLawResultV1](src/cruxible_client/contracts/procedures/line_specs.py#L437).

**`contracts.procedures.measurements`** — [ProcedureMeasurementExpectationV1](src/cruxible_client/contracts/procedures/measurements.py#L89), [AcceptedQueryProcedureMeasurementV1](src/cruxible_client/contracts/procedures/measurements.py#L130), [ClaimAttestationProcedureMeasurementV1](src/cruxible_client/contracts/procedures/measurements.py#L160), [ClaimStatementProcedureMeasurementV1](src/cruxible_client/contracts/procedures/measurements.py#L193), [ProcedureMeasurementSituationShapeV1](src/cruxible_client/contracts/procedures/measurements.py#L242), [ProcedureMeasurementReviewTriggerV1](src/cruxible_client/contracts/procedures/measurements.py#L273), [ProcedureMeasurementDeclarationV1](src/cruxible_client/contracts/procedures/measurements.py#L303).

**`contracts.procedures.models`** — [ProcedurePinSlotV1](src/cruxible_client/contracts/procedures/models.py#L39), [ProcedurePinSlotRefV1](src/cruxible_client/contracts/procedures/models.py#L74), [ProcedureBudgetV3](src/cruxible_client/contracts/procedures/models.py#L87), [ProcedureHardCapsV3](src/cruxible_client/contracts/procedures/models.py#L107), [PredicateOperandV1](src/cruxible_client/contracts/procedures/models.py#L126), [GuardPredicateV1](src/cruxible_client/contracts/procedures/models.py#L186), [StateTapNodeV3](src/cruxible_client/contracts/procedures/models.py#L240), [SourceNodeV3](src/cruxible_client/contracts/procedures/models.py#L254), [SourceNodeV4](src/cruxible_client/contracts/procedures/models.py#L280), [ExhaustTapNodeV3](src/cruxible_client/contracts/procedures/models.py#L307), [ProviderNodeV3](src/cruxible_client/contracts/procedures/models.py#L321), [ProviderNodeV4](src/cruxible_client/contracts/procedures/models.py#L339), [CallNodeV5](src/cruxible_client/contracts/procedures/models.py#L368), [TransformAdapterSpecV1](src/cruxible_client/contracts/procedures/models.py#L384), [TransformShapeItemsSpecV1](src/cruxible_client/contracts/procedures/models.py#L394), [TransformFilterItemsSpecV1](src/cruxible_client/contracts/procedures/models.py#L408), [TransformDedupeItemsSpecV1](src/cruxible_client/contracts/procedures/models.py#L421), [TransformJoinItemsSpecV1](src/cruxible_client/contracts/procedures/models.py#L434), [TransformAggregateItemsSpecV1](src/cruxible_client/contracts/procedures/models.py#L448), [TransformNodeV3](src/cruxible_client/contracts/procedures/models.py#L480), [GuardNodeV3](src/cruxible_client/contracts/procedures/models.py#L497), [ProjectNodeV3](src/cruxible_client/contracts/procedures/models.py#L512), [RepeatBodyNodeV3](src/cruxible_client/contracts/procedures/models.py#L526), [RepeatNodeV3](src/cruxible_client/contracts/procedures/models.py#L559), [RepeatBodyNodeV4](src/cruxible_client/contracts/procedures/models.py#L578), [RepeatNodeV4](src/cruxible_client/contracts/procedures/models.py#L635), [RepeatBodyNodeV5](src/cruxible_client/contracts/procedures/models.py#L654), [RepeatNodeV5](src/cruxible_client/contracts/procedures/models.py#L658), [CaptureEgressNodeV3](src/cruxible_client/contracts/procedures/models.py#L662), [InboxEgressNodeV3](src/cruxible_client/contracts/procedures/models.py#L674), [ProposeChangeSetNodeV3](src/cruxible_client/contracts/procedures/models.py#L685), [MandateSettlementNodeV3](src/cruxible_client/contracts/procedures/models.py#L700), [HaltNodeV3](src/cruxible_client/contracts/procedures/models.py#L713), [ProcedureDefinitionV3](src/cruxible_client/contracts/procedures/models.py#L781), [ProcedureDefinitionV4](src/cruxible_client/contracts/procedures/models.py#L906), [ProcedureDefinitionV5](src/cruxible_client/contracts/procedures/models.py#L1029).

**`contracts.procedures.pin_expectations`** — [PinExpectation](src/cruxible_client/contracts/procedures/pin_expectations.py#L37).

**`contracts.procedures.proposal_items`** — [ProcedureClaimProposalItemV1](src/cruxible_client/contracts/procedures/proposal_items.py#L29).

**`contracts.procedures.readings`** — [ProcedureMeasurementEligibilityV1](src/cruxible_client/contracts/procedures/readings.py#L92), [ProcedureMeasurementResolutionSummaryV1](src/cruxible_client/contracts/procedures/readings.py#L127), [ProcedureReadingSummaryV1](src/cruxible_client/contracts/procedures/readings.py#L177), [ProcedureMeasurementRowV1](src/cruxible_client/contracts/procedures/readings.py#L243), [PlaybillProcedureMeasureRequestV1](src/cruxible_client/contracts/procedures/readings.py#L261), [PlaybillProcedureMeasureResultV1](src/cruxible_client/contracts/procedures/readings.py#L293), [ProcedureMeasurementContractStatusV1](src/cruxible_client/contracts/procedures/readings.py#L319), [PlaybillProcedureReadingsRequestV1](src/cruxible_client/contracts/procedures/readings.py#L337), [PlaybillProcedureReadingsResultV1](src/cruxible_client/contracts/procedures/readings.py#L368).

**`contracts.procedures.results`** — [ProcedureJournalCoordinateV1](src/cruxible_client/contracts/procedures/results.py#L222), [ProcedureBudgetRefusalDetailV1](src/cruxible_client/contracts/procedures/results.py#L236), [ProcedureAdmissionRefusalV1](src/cruxible_client/contracts/procedures/results.py#L251), [ProcedureNodeRefusalV1](src/cruxible_client/contracts/procedures/results.py#L270), [ProcedureOperationalFailureV1](src/cruxible_client/contracts/procedures/results.py#L303), [ProcedureInternalFailureV1](src/cruxible_client/contracts/procedures/results.py#L324), [ProcedureRunAttributionV1](src/cruxible_client/contracts/procedures/results.py#L338), [ProcedurePendingSuccessorV1](src/cruxible_client/contracts/procedures/results.py#L353), [ProcedureRunReceiptV2](src/cruxible_client/contracts/procedures/results.py#L361), [ProcedureBudgetExceededDetailV1](src/cruxible_client/contracts/procedures/results.py#L403), [ProcedureBudgetExhaustedV1](src/cruxible_client/contracts/procedures/results.py#L414), [ProcedureHaltTerminalV1](src/cruxible_client/contracts/procedures/results.py#L429), [ProcedureBudgetBoundaryObservationV1](src/cruxible_client/contracts/procedures/results.py#L448), [ProcedureRunBudgetDeclaredV1](src/cruxible_client/contracts/procedures/results.py#L465), [ProcedureRunBudgetObservedV1](src/cruxible_client/contracts/procedures/results.py#L474), [ProcedureRunBudgetV1](src/cruxible_client/contracts/procedures/results.py#L485), [ProcedureRunReceiptV3](src/cruxible_client/contracts/procedures/results.py#L491), [ProcedureRunNodePinSetV1](src/cruxible_client/contracts/procedures/results.py#L504), [ProcedureReplayInputProjectionV1](src/cruxible_client/contracts/procedures/results.py#L528), [ProcedureProviderBindingV1](src/cruxible_client/contracts/procedures/results.py#L552), [ProviderBucketClassificationPlanV1](src/cruxible_client/contracts/procedures/results.py#L588), [ProcedureProviderBindingV2](src/cruxible_client/contracts/procedures/results.py#L616), [ProcedureSelectionDecisionV1](src/cruxible_client/contracts/procedures/results.py#L650), [ProcedureAdmissionMaterialMemberV1](src/cruxible_client/contracts/procedures/results.py#L684), [ProcedureAdmissionMaterialManifestV1](src/cruxible_client/contracts/procedures/results.py#L723), [ProcedureAcquisitionPlanV2](src/cruxible_client/contracts/procedures/results.py#L767), [ProcedureSourceObservationV1](src/cruxible_client/contracts/procedures/results.py#L853), [ProcedureSourceCaptureAssociationV1](src/cruxible_client/contracts/procedures/results.py#L892), [ProcedureTerminalEgressChildV1](src/cruxible_client/contracts/procedures/results.py#L922), [ProcedureTerminalEgressV1](src/cruxible_client/contracts/procedures/results.py#L942), [ProcedureRunBudgetDeclaredV2](src/cruxible_client/contracts/procedures/results.py#L1002), [ProcedureRunBudgetV2](src/cruxible_client/contracts/procedures/results.py#L1012), [ProcedureRunReceiptV4](src/cruxible_client/contracts/procedures/results.py#L1018), [ProcedureRunReceiptV5](src/cruxible_client/contracts/procedures/results.py#L1138), [ProcedureRunReceiptV6](src/cruxible_client/contracts/procedures/results.py#L1156).

**`contracts.procedures.windows`** — [CaptureEventSelectorV1](src/cruxible_client/contracts/procedures/windows.py#L26), [TriggerEventReferenceV1](src/cruxible_client/contracts/procedures/windows.py#L45), [FixedWindowV1](src/cruxible_client/contracts/procedures/windows.py#L58), [CaptureEventWindowV1](src/cruxible_client/contracts/procedures/windows.py#L75), [BoundObservationWindowV1](src/cruxible_client/contracts/procedures/windows.py#L84), [LineTriggerBindingV1](src/cruxible_client/contracts/procedures/windows.py#L131).

**`contracts.projection`** — [AcceptedProjectionCoordinate](src/cruxible_client/contracts/projection.py#L51), [AcceptedCoordinate](src/cruxible_client/contracts/projection.py#L94), [CandidateGenerationProjectionCoordinate](src/cruxible_client/contracts/projection.py#L137), [ProvisionalProjectionCoordinate](src/cruxible_client/contracts/projection.py#L186).

**`contracts.projection_extensions`** — [ProjectionFactDeclaration](src/cruxible_client/contracts/projection_extensions.py#L167), [ProjectionFact](src/cruxible_client/contracts/projection_extensions.py#L188), [ProjectionExtensionRegistry](src/cruxible_client/contracts/projection_extensions.py#L231).

**`contracts.proposal_models`** — [AuthenticatedActor](src/cruxible_client/contracts/proposal_models.py#L96), [ProposalReceiveLimits](src/cruxible_client/contracts/proposal_models.py#L167), [ProposalAdmissionRequest](src/cruxible_client/contracts/proposal_models.py#L254), [ProposalWithdrawalRecordV1](src/cruxible_client/contracts/proposal_models.py#L311), [ProposalAdmissionRecord](src/cruxible_client/contracts/proposal_models.py#L345), [ProposalEvaluationRecord](src/cruxible_client/contracts/proposal_models.py#L423), [ProposalResult](src/cruxible_client/contracts/proposal_models.py#L484), [ProposalTransportProtocol](src/cruxible_client/contracts/proposal_models.py#L497).

**`contracts.provider_contracts`** — [ProviderOperationContractV1](src/cruxible_client/contracts/provider_contracts.py#L19).

**`contracts.provider_execution`** — [ProviderSecretReferenceV1](src/cruxible_client/contracts/provider_execution.py#L24), [ProviderSecretBindingIdentityV1](src/cruxible_client/contracts/provider_execution.py#L52), [ProviderSecretReceiptReferenceV1](src/cruxible_client/contracts/provider_execution.py#L90), [ProviderSecretResolutionPlanV1](src/cruxible_client/contracts/provider_execution.py#L102), [ProviderBudgetTranslationV1](src/cruxible_client/contracts/provider_execution.py#L147), [ProviderEgressObservationV1](src/cruxible_client/contracts/provider_execution.py#L205), [VerifiedProviderBindingV1](src/cruxible_client/contracts/provider_execution.py#L253), [ProviderExternalOccurrencePlanV1](src/cruxible_client/contracts/provider_execution.py#L285), [ProviderInvocationOutcomeV1](src/cruxible_client/contracts/provider_execution.py#L409), [ProviderInvocationReceiptV1](src/cruxible_client/contracts/provider_execution.py#L450), [ProviderInvocationOutputDigestV1](src/cruxible_client/contracts/provider_execution.py#L536), [ProcedureDerivedSourceRequestV1](src/cruxible_client/contracts/provider_execution.py#L558), [ProviderInvocationStartedV1](src/cruxible_client/contracts/provider_execution.py#L632), [ProviderInvocationCompletedV1](src/cruxible_client/contracts/provider_execution.py#L650).

**`contracts.provider_installation`** — [ProviderWheelObjectV1](src/cruxible_client/contracts/provider_installation.py#L15), [PlaybillProviderInstallRequestV1](src/cruxible_client/contracts/provider_installation.py#L34), [ProviderOperationReadinessV1](src/cruxible_client/contracts/provider_installation.py#L72), [PlaybillProviderInstallResultV1](src/cruxible_client/contracts/provider_installation.py#L78), [ProviderPackageSummaryV1](src/cruxible_client/contracts/provider_installation.py#L91), [PlaybillProviderCatalogV1](src/cruxible_client/contracts/provider_installation.py#L97).

**`contracts.provider_interfaces`** — [ProviderInterfaceFormatError](src/cruxible_client/contracts/provider_interfaces.py#L38), [ProviderBucketClassV1](src/cruxible_client/contracts/provider_interfaces.py#L65), [ProviderBucketDimensionV1](src/cruxible_client/contracts/provider_interfaces.py#L77), [ProviderBucketVocabularyV1](src/cruxible_client/contracts/provider_interfaces.py#L101), [ProviderBucketConformanceFixtureV1](src/cruxible_client/contracts/provider_interfaces.py#L170), [ProviderBucketConformanceFixtureProofV1](src/cruxible_client/contracts/provider_interfaces.py#L200), [ProviderInterfaceRegistrationV1](src/cruxible_client/contracts/provider_interfaces.py#L283), [ProviderClassifierCodeV1](src/cruxible_client/contracts/provider_interfaces.py#L416), [ProviderInterfaceRegistrationV2](src/cruxible_client/contracts/provider_interfaces.py#L456), [AcceptedProviderInterfaceRegistrationV1](src/cruxible_client/contracts/provider_interfaces.py#L542), [ProviderInterfaceLawResultV1](src/cruxible_client/contracts/provider_interfaces.py#L556), [ProviderBucketClassifierInstallationResultV1](src/cruxible_client/contracts/provider_interfaces.py#L637), [ProviderBucketClassifierInstallationV1](src/cruxible_client/contracts/provider_interfaces.py#L645).

**`contracts.providers`** — [ProviderFormatError](src/cruxible_client/contracts/providers.py#L42), [ProviderSigningKeyV1](src/cruxible_client/contracts/providers.py#L50), [ProviderV1](src/cruxible_client/contracts/providers.py#L98), [ProviderDistributionRefV1](src/cruxible_client/contracts/providers.py#L186), [ProviderImplementationManifestV1](src/cruxible_client/contracts/providers.py#L191), [ProviderRuntimeManifestV1](src/cruxible_client/contracts/providers.py#L258), [ProviderImplementationManifestV2](src/cruxible_client/contracts/providers.py#L285), [ProviderRuntimeManifestV2](src/cruxible_client/contracts/providers.py#L297), [ProviderDistributionPinV1](src/cruxible_client/contracts/providers.py#L308), [ProviderLocalDistributionPinV1](src/cruxible_client/contracts/providers.py#L335), [ProviderImageProvenanceV1](src/cruxible_client/contracts/providers.py#L352), [ProviderContainerBackendPinV1](src/cruxible_client/contracts/providers.py#L365), [ProviderLocalEnvBackendPinV1](src/cruxible_client/contracts/providers.py#L373), [ProviderRuntimeArtifactPayloadV1](src/cruxible_client/contracts/providers.py#L395), [ProviderRuntimeArtifactPayloadV2](src/cruxible_client/contracts/providers.py#L417), [ProviderLocalMaterializationReferenceV1](src/cruxible_client/contracts/providers.py#L460), [ProviderContainerMaterializationReferenceV1](src/cruxible_client/contracts/providers.py#L471), [ProviderImplementationRecordV1](src/cruxible_client/contracts/providers.py#L514), [ProviderV2](src/cruxible_client/contracts/providers.py#L645), [ProviderV3](src/cruxible_client/contracts/providers.py#L705), [AcceptedProviderV1](src/cruxible_client/contracts/providers.py#L760), [ProviderLawResultV1](src/cruxible_client/contracts/providers.py#L774).

**`contracts.query.definitions`** — [QueryDefinitionFormatError](src/cruxible_client/contracts/query/definitions.py#L70), [QueryEvaluationPolicyV1](src/cruxible_client/contracts/query/definitions.py#L78), [QueryDefinitionV1](src/cruxible_client/contracts/query/definitions.py#L116), [QueryDefinitionSpecV1](src/cruxible_client/contracts/query/definitions.py#L372), [AcceptedQueryDefinitionV1](src/cruxible_client/contracts/query/definitions.py#L461), [QueryDefinitionLawResultV1](src/cruxible_client/contracts/query/definitions.py#L474).

**`contracts.query.grammar`** — [QueryReferenceInventoryV1](src/cruxible_client/contracts/query/grammar.py#L90), [QueryLiteralRefV1](src/cruxible_client/contracts/query/grammar.py#L112), [QueryParameterRefV1](src/cruxible_client/contracts/query/grammar.py#L129), [QueryClaimValueRefV1](src/cruxible_client/contracts/query/grammar.py#L146), [QuerySubjectFieldRefV1](src/cruxible_client/contracts/query/grammar.py#L172), [QueryEvaluationTimeRefV1](src/cruxible_client/contracts/query/grammar.py#L190), [QueryComparisonFilterV1](src/cruxible_client/contracts/query/grammar.py#L211), [QueryMembershipFilterV1](src/cruxible_client/contracts/query/grammar.py#L226), [QueryClaimPresenceFilterV1](src/cruxible_client/contracts/query/grammar.py#L251), [QueryConjunctionFilterV1](src/cruxible_client/contracts/query/grammar.py#L280), [QueryDisjunctionFilterV1](src/cruxible_client/contracts/query/grammar.py#L297), [QueryNegationFilterV1](src/cruxible_client/contracts/query/grammar.py#L314), [QueryParameterDeclarationV1](src/cruxible_client/contracts/query/grammar.py#L355), [QueryEntryV1](src/cruxible_client/contracts/query/grammar.py#L381), [QueryArtifactsEntryV2](src/cruxible_client/contracts/query/grammar.py#L410), [QueryTraversalStepV1](src/cruxible_client/contracts/query/grammar.py#L461), [QueryOrderingV1](src/cruxible_client/contracts/query/grammar.py#L504), [QueryProjectionFieldV1](src/cruxible_client/contracts/query/grammar.py#L525), [QueryProjectionV1](src/cruxible_client/contracts/query/grammar.py#L542), [QueryIncludeV1](src/cruxible_client/contracts/query/grammar.py#L564), [QueryBudgetsV1](src/cruxible_client/contracts/query/grammar.py#L614).

**`contracts.query.results`** — [QueryArtifactDefinitionV2](src/cruxible_client/contracts/query/results.py#L13).

**`contracts.repairs`** — [RepairOperationV1](src/cruxible_client/contracts/repairs.py#L12), [HandEditInstructionV1](src/cruxible_client/contracts/repairs.py#L19), [HandEditRepairV1](src/cruxible_client/contracts/repairs.py#L26), [ServedRepairEnvelopeV1](src/cruxible_client/contracts/repairs.py#L223).

**`contracts.resolution_contracts`** — [ClaimVersionReferenceV1](src/cruxible_client/contracts/resolution_contracts.py#L45), [ResolutionContractV1](src/cruxible_client/contracts/resolution_contracts.py#L72), [ResolutionContractReferenceV1](src/cruxible_client/contracts/resolution_contracts.py#L141), [InvestigationBindingV1](src/cruxible_client/contracts/resolution_contracts.py#L159), [ResolutionContractsRequestV1](src/cruxible_client/contracts/resolution_contracts.py#L165), [ResolutionContractViewV1](src/cruxible_client/contracts/resolution_contracts.py#L170), [ResolutionContractsResultV1](src/cruxible_client/contracts/resolution_contracts.py#L175).

**`contracts.resolution_rules`** — [PredictionEqualityRuleV1](src/cruxible_client/contracts/resolution_rules.py#L20), [PredictionThresholdRuleV1](src/cruxible_client/contracts/resolution_rules.py#L25), [PredictionPresenceRuleV1](src/cruxible_client/contracts/resolution_rules.py#L42), [PredictionObservationSelectorV1](src/cruxible_client/contracts/resolution_rules.py#L53).

**`contracts.semantic`** — [SemanticSelector](src/cruxible_client/contracts/semantic.py#L72), [SemanticAddress](src/cruxible_client/contracts/semantic.py#L90), [ContentSpan](src/cruxible_client/contracts/semantic.py#L161), [SourceMapping](src/cruxible_client/contracts/semantic.py#L182).

**`contracts.source_catalog`** — [SourceCatalogEntry](src/cruxible_client/contracts/source_catalog.py#L48), [ProcedureProjectionCatalogEntry](src/cruxible_client/contracts/source_catalog.py#L86), [SourceCatalog](src/cruxible_client/contracts/source_catalog.py#L121), [ResolvedSourceInput](src/cruxible_client/contracts/source_catalog.py#L160), [CompiledSourceDocument](src/cruxible_client/contracts/source_catalog.py#L170), [SourceCompilationManifest](src/cruxible_client/contracts/source_catalog.py#L179), [SourceCompilationBundle](src/cruxible_client/contracts/source_catalog.py#L215), [SourceAlignment](src/cruxible_client/contracts/source_catalog.py#L249).

**`contracts.source_references`** — [ProvisionalSemanticReadCoordinateV1](src/cruxible_client/contracts/source_references.py#L54), [CandidateGenerationReadCoordinateV1](src/cruxible_client/contracts/source_references.py#L79), [LedgerSourceReferenceV1](src/cruxible_client/contracts/source_references.py#L148), [CasSourceReferenceV1](src/cruxible_client/contracts/source_references.py#L155), [ExternalSourceReferenceV1](src/cruxible_client/contracts/source_references.py#L207), [SourceSchemaRegistry](src/cruxible_client/contracts/source_references.py#L252), [EvidenceCommitmentV1](src/cruxible_client/contracts/source_references.py#L279), [CoverageDescriptorV1](src/cruxible_client/contracts/source_references.py#L325), [SourceHandleV1](src/cruxible_client/contracts/source_references.py#L347), [BodyAccessResultV1](src/cruxible_client/contracts/source_references.py#L407), [SourceDereferenceResultV1](src/cruxible_client/contracts/source_references.py#L433), [OpenSourceRequestV1](src/cruxible_client/contracts/source_references.py#L470).

**`contracts.standing_mandates`** — [StandingMandateError](src/cruxible_client/contracts/standing_mandates.py#L38), [MandateGrantV1](src/cruxible_client/contracts/standing_mandates.py#L46), [StandingMandate](src/cruxible_client/contracts/standing_mandates.py#L73), [AcceptedStandingMandateV1](src/cruxible_client/contracts/standing_mandates.py#L200), [StandingMandateLawResultV1](src/cruxible_client/contracts/standing_mandates.py#L214), [MandateInvocationV1](src/cruxible_client/contracts/standing_mandates.py#L271), [MandateRuntimeCapV1](src/cruxible_client/contracts/standing_mandates.py#L297), [MandateEvaluationV1](src/cruxible_client/contracts/standing_mandates.py#L334), [StandingMandateQueryResultV1](src/cruxible_client/contracts/standing_mandates.py#L394).

**`contracts.subjects`** — [SubjectShell](src/cruxible_client/contracts/subjects.py#L50), [AcceptedSubject](src/cruxible_client/contracts/subjects.py#L176), [SubjectLawResult](src/cruxible_client/contracts/subjects.py#L189).

**`contracts.types`** — [StrictModel](src/cruxible_client/contracts/types.py#L27), [PrincipalRecord](src/cruxible_client/contracts/types.py#L31), [PlaybillTrustRoot](src/cruxible_client/contracts/types.py#L61), [StorageLayout](src/cruxible_client/contracts/types.py#L110), [CompilerCoordinate](src/cruxible_client/contracts/types.py#L148), [GenesisCoordinate](src/cruxible_client/contracts/types.py#L168), [GenerationDescriptor](src/cruxible_client/contracts/types.py#L190), [AuthorityMatrix](src/cruxible_client/contracts/types.py#L213), [PlaybillDecommissionV1](src/cruxible_client/contracts/types.py#L269), [PlaybillDescriptor](src/cruxible_client/contracts/types.py#L289), [PrincipalInspection](src/cruxible_client/contracts/types.py#L341), [PlaybillInspection](src/cruxible_client/contracts/types.py#L349).

**`contracts.workspace_advertisement`** — [PlaybillWorkspaceAdvertisement](src/cruxible_client/contracts/workspace_advertisement.py#L21).

**`contracts.workspace_file`** — [WorkspaceFileSourceRequestV1](src/cruxible_client/contracts/workspace_file.py#L33), [SourceReadReceiptV1](src/cruxible_client/contracts/workspace_file.py#L83).

## Examples

These are usage examples, not additional API definitions. Instance-dependent
examples require the named accepted ontology, definitions, and caller authority.

### Connect, snapshot, and review

Configure `CRUXIBLE_SERVER_BEARER_TOKEN` with the credential supplied by your
instance operator. Keep it out of source files and command output. The SDK uses
that credential for the explicit instance; it does not create a principal or
obtain approval authority by connecting.

```python
from pathlib import Path
from cruxible_client import Playbill

with Playbill.connect(
    target="http://localhost:8000",  # Or unix:/path/to/daemon.sock
    instance="your-instance-id",
    workspace=Path.cwd(),
) as pb:
    world = pb.world()
    print(world.coordinate)
```

Accepted reads on `pb` use the current head by default. Each request resolves
one coordinate on the daemon; pagination retains that coordinate. `pb.coordinate`
reports the last observed coordinate without performing I/O. Typed references
carry explicit coordinates and remain pinned. Mixed-coordinate Claim batches
refuse rather than silently moving a reference.

Use `snapshot = pb.at(coordinate)` (or `Playbill.connect(..., at=coordinate)`)
for a fixed context. `snapshot.world()` and its lazy reads stay at that coordinate.
`pb.world()` returns an independent snapshot that remains readable after the live
client advances. `refresh()` on a pinned context refreshes that same snapshot.
Borrowed contexts share the original connection's lifetime; closing one does not
close the original transport.

`pb.accept(proposal_id)` requests acceptance and returns its exact coordinate;
it does not export a workspace floor or approve the proposal. For exact readback,
even if another writer has advanced the head again:

```python
receipt = pb.accept(proposal_id)
if receipt.accepted_coordinate is not None:
    accepted = pb.at(receipt.accepted_coordinate)
    claims = accepted.claim_views(claim_ids)
```

Drafts retain their observed vocabulary/reference coordinate; admission still
checks current state and reports stale inputs. Operational queues, signing and
write admission retain their current authority/evidence checks; a pinned reading
context does not rewind operational state.

World attributes return live Claim contenders rather than silently selecting a
scalar. Use `world.prefetch(subjects=(...), predicates=(...))` for bounded reads
of known selections, then inspect each Claim's value and verdict. A returned
Claim's `subject` path can be passed directly to `pb.claim(subject=...)` or a
changeset Claim writer when revising it. Acceptance and
evidential support are distinct: an accepted Claim may remain unsupported under
its evidence policy.

File-backed authoring requires a declared `.playbill/sources.yaml` catalog in the
workspace. Supplying a body with `self_source` is an explicit self-assertion, not
an observation of an independent source. The SDK's `derived_by()` method currently
returns a typed unavailable refusal; it is not a supported derivation writer.

### Review and approve an exact candidate

Save `intent.intent_id` after preparation. After a process interruption, reopen
the daemon-owned work through the same instance and authenticated actor:

```python
intent = pb.resume_intent(saved_intent_id)
if intent.refused:
    print(intent.diagnostics)
proposal = intent.proposal  # Last observed proposal, without another HTTP call.
status = intent.status()    # Explicitly check the current lifecycle state.
```

Resuming reads the latest revision and persisted preflight diagnostics. It does
not prepare again, submit, approve, accept, or refresh the local floor. In
particular, recover an uncertain submission this way before deciding what to do
next. Python call-site locations and response-only lint warnings are not persisted
and are unavailable on the reopened handle. Review tokens are also process-local;
review the proposal again before signing in a new process.

Your operator supplies an `ApprovalSigner` capability, configured for an existing
accepted principal. The agent does not discover a key, select its own authority,
or send private key bytes to the daemon.

```python
proposal = pb.proposal(submitted.status().proposal_id)
reviewed = proposal.review()
review = reviewed.details  # Inspect all members, evidence, governance and provenance.
# After deciding to approve this exact candidate:
approval = proposal.approve(signer=configured_signer, reviewed=reviewed)
# After a separate decision to accept:
receipt = pb.accept(proposal.proposal_id)
world = pb.world()
```

`ReviewedProposal` is a process-local, originating-session/instance-bound snapshot.
`details` returns a fresh copy; editing it cannot alter the approved candidate.
The token binds identity, not proof that a human or agent actually read it.
The helper obtains fresh governance/challenge data, checks the reviewed candidate,
root and signer, verifies the local signature and submits it. It never accepts
or refreshes implicitly. The authenticated submitter may differ from the signer.

This convenience path requires a complete unredacted review. An
`ApprovalReviewMismatch` includes a repair instruction and never automatically
reviews or signs a replacement candidate. If the receipt check fails after
submission, inspect proposal status before retrying. Existing raw
`CruxibleClient` review/challenge/attestation APIs remain available for advanced
external signing and partial-visibility workflows under the server's policy.

For operator configuration, `LocalEd25519ApprovalSigner.open(...)` takes an explicit
principal ID, private key path, expected public key, and forbidden custody roots.
It preserves existing file permissions, nonsymlink/no-follow reads, and key checks
on every signature. The `ApprovalSigner` protocol is also the seam for a separately
provided signer backend; hardware and broker implementations are not included.

The [complete disposable example](examples/claim_review_repair.py) authors a
source-backed Claim, prompts for review and acceptance, reads its World state,
detects changed source evidence with free audit/`next`, and repairs the same Claim.
It needs an initialized disposable instance and operator-provisioned signer;
it does not bootstrap authority. World's qualified Claim IDs can be passed
directly to `revises` and `dispositions`; duplicate normalized keys are refused.

### Complete local Procedure preview



This example needs no daemon and invokes no provider:

```python
from cruxible_client.authoring.inputs import CarriedContractInput
from cruxible_client.authoring.procedures import Previous, Project, Sequence
from cruxible_client.contracts.captures import CanonicalDurationV1
from cruxible_client.contracts.procedures.contract_schema import PropertySchema
from cruxible_client.contracts.procedures.models import (
    ProcedureBudgetV3,
    ProcedureHardCapsV3,
)

empty = CarriedContractInput(name="empty", fields={})
count = CarriedContractInput(
    name="count", fields={"count": PropertySchema(type="int")},
)
duration = CanonicalDurationV1(microseconds=1_000_000)
blueprint = Sequence(
    [
        Project("seed", fields={"count": 1}, contract_out=count),
        Project("result", fields={"count": Previous("count")}, contract_out=count),
    ],
    name="count-example",
    contract_in=empty,
    contract_out=count,
    budget=ProcedureBudgetV3(
        wall_clock=duration, max_provider_calls=1, max_capture_bytes=1024,
    ),
    hard_caps=ProcedureHardCapsV3(
        max_wall_clock=duration, max_provider_calls=1, max_capture_bytes=1024,
        max_items=100, max_repeat_attempts=1,
    ),
)
preview = blueprint.preview()
assert preview.ready_for_prepare
print(preview.model_dump_json(indent=2))
definition = blueprint.build()
```

With an existing `pb`, `pb.procedure(definition=blueprint).prepare()` creates and
preflights an authoring intent. Submission, review, approval where required,
and acceptance remain separate operations. To include related definitions,
use `pb.changes(rationale=...).procedure(definition=blueprint)` before preparing
the changeset.
