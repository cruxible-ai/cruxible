# Cruxible Python SDK reference

**SDK v2 development API · 2026-09-22 (not yet released).** Install
`cruxible-client` for the Python client; install `cruxible` separately for the
daemon/CLI/MCP runtime. This is an API reference, not an implementation plan.
The [SDK v2 reference](../../docs/sdk-v2-reference.md) specifies retained source
authoring, contract-derived records, typed query/run results, and sequential
child calls. Source authoring is available on the development branch. Names and
typed handles are authoring inputs; exact versions and hashes resolve in the backend.

Coverage: every public `Cruxible` operation, returned authoring/run handles,
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
explicit coordinate. `cx.coordinate` is cached, not a freshness check. Methods
documented as using `cx.coordinate` use that exact last-observed/pinned value.
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

Import `Cruxible` from `cruxible_client`. Use `Cruxible.connect(...)` or
the context-manager form. Direct construction is an advanced injected-client
adapter; it does not perform the compatibility/orientation sequence of connect.

```text
Cruxible(
    *,
    client: CruxibleClient,
    instance_id: str,
    workspace: Path,
    access_profile: AccessProfile,
    clock: Any,
) -> Cruxible
```

All constructor arguments are required. `client` is the injected transport;
`instance_id` selects the daemon instance; `workspace` is expanded and resolved;
`access_profile` supplies the read/disclosure preference; `clock` is the injected
evaluation-time source. The object initially has no observed coordinate and owns
its transport. Prefer `connect()` for ordinary use so context and compatibility
checks run.

A Cruxible built on an injected client with no workspace (the internal
`_from_client(..., workspace=None)` path) still reads and writes accepted state:
`orient()` reports no floor, and members that need workspace files refuse with
`SourceSelectionError`.

| Setting | Default / behavior |
|---|---|
| `CRUXIBLE_SERVER_BEARER_TOKEN` | Used when `token` is omitted; never creates a principal or grants rights. |
| `CRUXIBLE_PRINCIPAL_ID` | Used when `principal_id` is omitted; the daemon checks it is a registered, active principal before any write and attributes the session to it; reads stay open. |
| `CRUXIBLE_CLI_CONTEXT_PATH` | Otherwise `~/.cruxible/client-context.json`. |
| `CRUXIBLE_CLIENT_TIMEOUT_S` | Ordinary HTTP read/write timeout: 180 seconds; connect/pool: 5 seconds. |
| Default access profile | `sdk-default`, classes `("instance", "public")`, disclose restricted existence `True`. |

Explicit target/instance/workspace arguments participate in the shared context
resolver. Workspace attachment, environment, and remembered context cannot
silently combine an instance with an incompatible transport. Resolve or repair
that mismatch instead of guessing a local instance.

<a id="api-cruxible-connect"></a>

### `Cruxible.connect`

[Source](src/cruxible_client/authoring/sdk.py)

```text
connect(
    *,
    context: str | Path | None = None,
    target: str | None = None,
    instance: str | None = None,
    token: SecretStr | None = None,
    principal_id: str | None = None,
    workspace: Path | None = None,
    access_profile: AccessProfile | None = None,
    at: AcceptedCoordinate | api.AcceptedCoordinate | None = None,
) -> Cruxible
```

Opens a transport, checks client/daemon contract compatibility, resolves context, and ordinarily performs orientation. A supplied at skips live orientation and pins that coordinate.

**Conditions and effects:** ValueError/ContextResolutionError for missing or inconsistent context; IncompatibleDaemonVersion for an incompatible authoring contract; authentication/transport errors.

| Parameter | Default | Meaning |
|---|---|---|
| `context` | `None` | Remembered client-context JSON path; otherwise CRUXIBLE_CLI_CONTEXT_PATH or ~/.cruxible/client-context.json. |
| `target` | `None` | Explicit HTTP(S) endpoint or unix:/absolute/socket. It does not select an arbitrary local instance directory. |
| `instance` | `None` | Daemon instance ID. Omission uses resolved workspace/client context. |
| `token` | `None` | Bearer credential as SecretStr; otherwise CRUXIBLE_SERVER_BEARER_TOKEN. |
| `principal_id` | `None` | Principal this session acts as; otherwise CRUXIBLE_PRINCIPAL_ID. With daemon auth off it is a claim of identity, not authentication; with auth on it must equal the credential's principal. |
| `workspace` | `None` | Client workspace root for source selection, projections, and configured floors. |
| `access_profile` | `None` | Declared access classes and disclosure preference; server authorization remains authoritative. |
| `at` | `None` | Explicit accepted coordinate. Omission follows the live/pinned object semantics stated above. |

<a id="api-cruxible-close"></a>

### `Cruxible.close`

[Source](src/cruxible_client/authoring/sdk.py)

```text
close() -> None
```

Closes an owning connection. Closing a context borrowed through at() leaves its owner’s transport open.

**Conditions and effects:** No automatic submission, acceptance, or workspace refresh.

<a id="api-cruxible-coordinate"></a>

### `Cruxible.coordinate`

[Source](src/cruxible_client/authoring/sdk.py)

```text
coordinate: AcceptedCoordinate
```

The pinned coordinate, or the live client's last observed coordinate.

This property performs no I/O. Live reads resolve current head in their
own request, so this value is not a freshness check.

<a id="api-cruxible-at"></a>

### `Cruxible.at`

[Source](src/cruxible_client/authoring/sdk.py)

```text
at(coordinate: AcceptedCoordinate | api.AcceptedCoordinate) -> Cruxible
```

Returns a borrowed pinned context without I/O. Accepted reads stay fixed; writes still undergo current admission.

**Conditions and effects:** The original connection must remain open; pinned context does not rewind operational queues or authority.

| Parameter | Default | Meaning |
|---|---|---|
| `coordinate` | Required | Exact accepted coordinate, not a timestamp or an instruction to refresh current head. |

<a id="api-cruxible-refresh"></a>

### `Cruxible.refresh`

[Source](src/cruxible_client/authoring/sdk.py)

```text
refresh() -> api.Head
```

Re-reads the accepted head through `CruxibleClient.head` (a pinned context re-reads its own coordinate) and records the observed coordinate. `Head` carries only `instance`, `coordinate` and `generation`; call `orient()` for the map.

**Conditions and effects:** A pinned context stays pinned; refresh does not accept proposals or update a local floor.

<a id="api-cruxible-world"></a>

### `Cruxible.world`

[Source](src/cruxible_client/authoring/sdk.py)

```text
world() -> World
```

Reads accepted vocabulary and creates an independent pinned World. Lazy field reads remain at that coordinate as the live client advances.

**Conditions and effects:** Current first Subject access loads the Subject listing; field access is not an automatic scalar resolution.

<a id="api-cruxible-block"></a>

### `Cruxible.block`

[Source](src/cruxible_client/authoring/sdk.py)

```text
block: ProjectionBlocks
```

Client-only declaration stamps; prose remains wholly agent-owned.

<a id="api-cruxible-enter"></a>

### `Cruxible.__enter__`

[Source](src/cruxible_client/authoring/sdk.py)

```text
__enter__() -> Cruxible
```

<a id="api-cruxible-exit"></a>

### `Cruxible.__exit__`

[Source](src/cruxible_client/authoring/sdk.py)

```text
__exit__(*_args: object) -> None
```

| Parameter | Default | Meaning |
|---|---|---|
| `_args` | `'<variadic>'` | Value required by the declared type; see operation semantics below. |

## Knowledge authoring

New ClaimType authoring emits the producer-independent v5 artifact and v2 evidence
policy. Evidence rules define admissible capture contracts, roles, subject binding,
and attestation requirements. A derivational rule also requires exact producer and
input-Claim provenance, but never an allowlist of producing Procedures. Producer
permission is enforced through the existing Procedure mandates and governed
proposal approval. Historical ClaimType formats retain their original validation.

The compact derivation profile is `replay-verifiable-derivation-v2`; it requires
capture-contract and evidence-kind parameters, with no reducer digest parameter.

<a id="api-cruxible-changes"></a>

### `Cruxible.changes`

[Source](src/cruxible_client/authoring/sdk.py)

```text
changes(*, rationale: str | None=None) -> ChangeSetDraft
changes(*, because: str) -> WriteBatch
```

With `rationale` (or nothing), returns a changeset builder retaining the last
observed coordinate for references and vocabulary. With `because`, returns a
`WriteBatch` of typed write changes: `.set(subject, field, value, ...)`,
`.add(subject, field, value, ...)` and `.retire(target, ...)`, sent as one change
set by `.write(dry_run=False, accept="if_allowed", at=...)`, which returns the
`WriteOutcome` or raises `WriteRefusalError`.

**Conditions and effects:** No prepare/submit/accept at construction. The set admits or refuses as one governed decision.

| Parameter | Default | Meaning |
|---|---|---|
| `rationale` | `None` | Author-supplied explanation retained with this Claim/change decision. |
| `because` | `None` | Why: opens a `WriteBatch`, whose change set carries it as its rationale. |

<a id="api-cruxible-subject"></a>

### `Cruxible.subject`

[Source](src/cruxible_client/authoring/sdk.py)

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

<a id="api-cruxible-claim-type"></a>

### `Cruxible.claim_type`

[Source](src/cruxible_client/authoring/sdk.py)

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
    admission_policy: ClaimAdmissionPolicy,
    resolution_policy: ClaimResolutionPolicy,
    pins: Sequence[ArtifactPin],
    evidence_freshness: Duration | None,
    attestation_consequence_policy: ClaimAttestationConsequencePolicy | None = None,
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

<a id="api-cruxible-claim"></a>

### `Cruxible.claim`

[Source](src/cruxible_client/authoring/sdk.py)

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

<a id="api-cruxible-set"></a>

### `Cruxible.set`

[Source](src/cruxible_client/authoring/sdk.py)

```text
set(
    subject: str | SubjectRef,
    field: str | ClaimTypeRef,
    value: ClaimValue | SubjectRef | LiteralValue,
    *,
    because: str,
    evidence: Evidence | None = None,
    role: WriteRole | None = None,
    contend: bool = False,
    dry_run: bool = False,
    accept: Literal['if_allowed', 'never'] = 'if_allowed',
    at: AcceptedCoordinate | str | None = <this context's coordinate>,
) -> WriteOutcome
```

Puts one value in one field of one Subject. On a single-value field it replaces
the live value; the Claim it revises is found for you. A missing Subject of a
known kind is added in the same change set.

**Conditions and effects:** Accepts in the same call when the approval policy
and the caller's tier allow it; otherwise the outcome is `awaiting_approval` with
the eligible approvers and the approve call. `dry_run` runs every check and
writes nothing. By default the write refuses `cruxible.write.slot_changed` when
the field moved since this context's coordinate; after that refusal the context
has seen the new value, so setting again replaces it. A refusal raises
`WriteRefusalError` carrying the outcome. Each change carries its `verdict`; a
verdict other than `supported` comes with a warning in `outcome.warnings`.

| Parameter | Default | Meaning |
|---|---|---|
| `subject` | Required | The Subject as `kind/id`, or a typed SubjectRef. |
| `field` | Required | A field of the kind as `orient` names it, or the full predicate. |
| `value` | Required | An enum member, text, number or boolean; a Subject for a Subject-valued field; the text itself for exact content. |
| `because` | Required | Why; also the default self evidence. |
| `evidence` | `None` | `SelfEvidence`, `CaptureEvidence` or `FileEvidence` (`PATH#ANCHOR`, read from this workspace). |
| `role` | `None` | Only when the field permits more than one role. |
| `contend` | `False` | Contest the live value instead of replacing it. |
| `dry_run` | `False` | Check everything and write nothing. |
| `accept` | `'if_allowed'` | `'never'` only proposes. |
| `at` | this context's coordinate | The coordinate you read at; `None` checks against the head. |

<a id="api-cruxible-retire"></a>

### `Cruxible.retire`

[Source](src/cruxible_client/authoring/sdk.py)

```text
retire(
    target: str | ClaimRef | SlotRef,
    *,
    because: str,
    reason: Literal['was-rescinded', 'was-wrong', 'superseded'] = 'was-rescinded',
    dry_run: bool = False,
    accept: Literal['if_allowed', 'never'] = 'if_allowed',
    at: AcceptedCoordinate | str | None = <this context's coordinate>,
) -> WriteOutcome
```

Ends one live Claim, named by Claim ID or by `SlotRef(subject=..., field=...)`
for a field that holds one value. The Claims that depend on it retire with it,
in one change set.

**Conditions and effects:** As `Cruxible.set`.

<a id="api-cruxible-query-definition"></a>

### `Cruxible.query_definition`

[Source](src/cruxible_client/authoring/sdk.py)

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

<a id="api-cruxible-resume-intent"></a>

### `Cruxible.resume_intent`

[Source](src/cruxible_client/authoring/sdk.py)

```text
resume_intent(intent_id: str) -> Intent
```

Reads the latest durable intent, preflight, and observed proposal into a handle.

**Conditions and effects:** Does not prepare, submit, sign, accept, or restore process-local source locations/review tokens.

| Parameter | Default | Meaning |
|---|---|---|
| `intent_id` | Required | Durable authoring intent identity from this instance. |

<a id="api-cruxible-proposal"></a>

### `Cruxible.proposal`

[Source](src/cruxible_client/authoring/sdk.py)

```text
proposal(proposal_id: str) -> Proposal
```

Creates a local handle for an existing proposal ID.

**Conditions and effects:** Does not read, review, create, approve, or accept the proposal.

| Parameter | Default | Meaning |
|---|---|---|
| `proposal_id` | Required | Exact proposal identity, not a branch name or candidate revision inferred from head. |

<a id="api-cruxible-accept"></a>

### `Cruxible.accept`

[Source](src/cruxible_client/authoring/sdk.py)

```text
accept(proposal_id: str) -> api.ActivationReceipt
```

Requests governed acceptance and returns ActivationReceipt. Updates a live connection’s last observed coordinate.

**Conditions and effects:** Does not sign approval or refresh the client workspace. Use cx.at(receipt.accepted_coordinate) for exact readback.

| Parameter | Default | Meaning |
|---|---|---|
| `proposal_id` | Required | Exact proposal identity, not a branch name or candidate revision inferred from head. |

<a id="api-cruxible-activate"></a>

### `Cruxible.activate`

[Source](src/cruxible_client/authoring/sdk.py)

```text
activate(proposal_id: str, *, no_sync: bool=False) -> api.WorkspaceActivationResult
```

Requests acceptance, refreshes the configured floor at the accepted coordinate, and normally checks workspace blocks.

**Conditions and effects:** no_sync skips the block check, not floor refresh. Acceptance can succeed even when later workspace maintenance reports a problem.

| Parameter | Default | Meaning |
|---|---|---|
| `proposal_id` | Required | Exact proposal identity, not a branch name or candidate revision inferred from head. |
| `no_sync` | `False` | Skip block checking after workspace activation; does not turn acceptance into a dry run. |

<a id="api-cruxible-refresh-workspace"></a>

### `Cruxible.refresh_workspace`

[Source](src/cruxible_client/authoring/sdk.py)

```text
refresh_workspace(
    *,
    at: AcceptedCoordinate | api.AcceptedCoordinate,
) -> api.FloorRefreshResult
```

Exports/materializes the configured floor at the explicit accepted coordinate and reports written/failed/not_configured.

**Conditions and effects:** Writes client workspace files; does not advance the reading context or check/repin projection blocks.

| Parameter | Default | Meaning |
|---|---|---|
| `at` | Required | Explicit accepted coordinate. Omission follows the live/pinned object semantics stated above. |

<a id="api-cruxible-file"></a>

### `Cruxible.file`

[Source](src/cruxible_client/authoring/sdk.py)

```text
file(path: str | Path) -> FileSelector
```

Reads a cataloged workspace file into a FileSelector using .cruxible/sources.yaml and applicable local catalog settings.

**Conditions and effects:** Uncataloged/out-of-root selections and malformed catalogs refuse. File bytes are observed now, before prepare.

| Parameter | Default | Meaning |
|---|---|---|
| `path` | Required | Workspace/source path or destination; interpretation is stated by its owning operation. |

## Reads and discovery

<a id="api-cruxible-claim-view"></a>

### `Cruxible.claim_view`

[Source](src/cruxible_client/authoring/sdk.py)

```text
claim_view(claim: str | ClaimRef) -> ClaimView
```

Reads and adapts one accepted Claim, including value, revision, verdict, and capture references. It reads `get(claim, detail="proof")`.

**Conditions and effects:** Missing/redacted/version-incompatible artifacts are daemon refusals; a wrong reference kind refuses locally.

| Parameter | Default | Meaning |
|---|---|---|
| `claim` | Required | Claim ID or typed ClaimRef; typed refs also assert their observed coordinate. |

<a id="api-cruxible-claim-views"></a>

### `Cruxible.claim_views`

[Source](src/cruxible_client/authoring/sdk.py)

```text
claim_views(claims: Sequence[str | ClaimRef]) -> tuple[ClaimView, ...]
```

Reads a complete identity batch, at most 256 Claims, preserving input order.

**Conditions and effects:** Mixed coordinates, incomplete/truncated response, or mismatched returned identities refuse.

| Parameter | Default | Meaning |
|---|---|---|
| `claims` | Required | Claim selection; identity batches preserve caller order. For repin, None preserves and an empty sequence removes this backing class. |

<a id="api-cruxible-capture"></a>

### `Cruxible.capture`

[Source](src/cruxible_client/authoring/sdk.py)

```text
capture(capture: str | CaptureRef, *, max_bytes: int=4 * 1024 * 1024) -> CaptureView
```

Reads retained capture metadata and available bytes at cx.coordinate. Does not refetch the external source.

**Conditions and effects:** Unavailable evidence is represented in CaptureView.result; .ref/.content refuse if the required material is unavailable.

| Parameter | Default | Meaning |
|---|---|---|
| `capture` | Required | Capture digest or typed CaptureRef to retained evidence; does not initiate acquisition. |
| `max_bytes` | `4 * 1024 * 1024` | Byte bound for this operation, in bytes. It is not permission to exceed effective daemon policy. |

<a id="api-cruxible-get"></a>

### `Cruxible.get`

[Source](src/cruxible_client/authoring/sdk.py)

```text
get(ref: str | TypedRef, *, detail: GetDetail = "summary", range: tuple[int, int] | str | None = None) -> KnowledgeCard
```

Reads one thing by reference; the daemon resolves the reference directly (never through search). `ref` is a typed Subject/ClaimType/Claim/Procedure/Query/Source ref (read at its coordinate) or any string an agent sees: `CLM-...` or a unique prefix, `kind/id`, a predicate (full or a unique leaf), `ClaimType:`/`Document:`/`Procedure:`/`query:`/`CaptureContract:<name>`, an artifact path, a proposal id or prefix, or an operational reference (`Line:<name>` or the Line identity digest `next` names, `CAP-<12+ hex>`/`Capture:<digest>`, `ResolutionContract:<name>`, `Mandate:<name>`, `ProcedureRun:<run_id>` or `RUN-<12+ hex>`; their cards live in `cruxible_client.contracts.operational_reads`), or a governance reference: `Principal:<id>`, `ApprovalPolicy:instance`, `ProviderInterface:<name>`. A Claim summary is a `ClaimView`; other summaries are the values-first card (`GetSubjectCard`, `GetClaimTypeCard`, ...) from `cruxible_client.contracts.get_reads`; other details carry their payload (`GetEvidence`, whose `value` is the Claim's whole value; `GetHistory`, newest first, with every page read; `GetBody`, whose `body_digest` names the whole body even when `range` reads part of it; the explain dict for `why`, on a Claim, Subject or Document; and for `proof` a `ClaimViewRecord` on a Claim or the envelope dict otherwise). A summary card cuts a string value over 500 characters to `{value, truncated: true, length}`. The card's `coordinate` is the full accepted coordinate: the SDK asks the daemon for it (`full_coordinate`), where MCP and CLI summaries carry only the 12-hex git oid prefix and generation.

**Conditions and effects:** An unknown or ambiguous reference refuses with `cruxible.get.ref_not_found` or `cruxible.get.ref_ambiguous` and the nearest names; a detail that does not apply to the kind refuses naming the ones that do. A Document body over 64 KiB needs `range`. Inspect `kind` before using `value`.

| Parameter | Default | Meaning |
|---|---|---|
| `ref` | Required | Typed reference or any supported reference string. |
| `detail` | `"summary"` | `summary`, `evidence` (Claims), `why` (Claims, Subjects, Documents), `history`, `proof`, or `body` (Documents). |
| `range` | `None` | Document body bytes as `(start, end)` or `"start:end"`; `detail="body"` only. |

<a id="api-cruxible-orient"></a>

### `Cruxible.orient`

[Source](src/cruxible_client/authoring/sdk.py)

```text
orient(
    *,
    kind: str | None = None,
    section: api.OrientSection | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> api.OrientResult
```

Maps accepted state in one call at the context’s coordinate: each Subject kind with its live count and predicates (type, cardinality, enum members, accepted evidence as CaptureContract names), artifact counts, named queries, `you` (whether this caller can author, and why not), `attention` from the `next` queue, and `next` suggestions written as SDK calls. `kind` reads one kind in full with sample Subject IDs and the predicates that point at it (`incoming`). `section` pages one family: `documents`, `procedures`, `claim_types`, `queries`, `interfaces`, `principals` or `policies`, or an operational one (`runs`, `running`, `lines`, `captures`, `capture_contracts`, `predictions`, `mandates`). An `interfaces` row carries `interface_digest`, `providers` (`[{provider, implementation_digest}]`) and `operation_contract`; read one in full with `get("ProviderInterface:<name>")`. When the workspace holds an exported floor, `floor` says its coordinate and how many generations it is behind; a connection without a workspace reports no floor.

There is no cross-kind name search: grep the exported floor under `.cruxible/floor/`, then `get` the reference you found.

**Conditions and effects:** Follow `next_cursor` while `truncated`. A wrong kind is refused with the nearest kinds.

| Parameter | Default | Meaning |
|---|---|---|
| `kind` | `None` | One Subject kind to read in full. |
| `section` | `None` | One artifact family to page instead of the map. |
| `limit` | `50` | Kinds or section rows per page. |
| `cursor` | `None` | `next_cursor` from the previous page of the same view. |

<a id="api-cruxible-run-query"></a>

### `Cruxible.run_query`

[Source](src/cruxible_client/authoring/sdk.py)

```text
run_query(
    query: str | QueryRef | QueryBinding,
    *,
    parameters: Mapping[str, object] | None = None,
    budgets: QueryBudgets | None = None,
) -> api.QueryRun
```

Runs a named accepted query in the live/pinned/reference context with explicit evaluation time and returns result plus receipt. It is `query(name=..., params=..., budgets=..., receipt="full")` underneath: the `QueryRun` is built from that answer's replay receipt (`definition_path`, the `ClaimQueryResult` result and the `QueryExecutionReceipt` execution receipt). A full receipt runs the definition's declared budgets (or the ones you pass), never the compact page's server ceiling, so a replay's result and digest match the old `run_query`.

**Conditions and effects:** Check verdict and truncation; artifact_definitions is a checked typed property for artifact queries only.

| Parameter | Default | Meaning |
|---|---|---|
| `query` | Required | Named query, QueryRef, or QueryBinding (whose `parameters` must come from `binding.parameters`). |
| `parameters` | `None` | Invocation/query parameters in the declared canonical contract. |
| `budgets` | `None` | Operation-specific bounds; the signature distinguishes QueryBudgets from Line budget mappings. |

<a id="api-cruxible-query"></a>

### `Cruxible.query`

[Source](src/cruxible_client/authoring/sdk.py)

```text
query(
    kind: str | None = None,
    *,
    where: Sequence[QueryFilter | Mapping[str, object]] | None = None,
    contains: str | None = None,
    select: Sequence[str] | None = None,
    follow: Sequence[
        QueryFollow | Mapping[str, str] | tuple[str, str] | tuple[str, str, QueryFollowDirection]
    ] | None = None,
    order_by: Sequence[str] | None = None,
    limit: int = 50,
    cursor: str | None = None,
    spec: QueryDefinitionSpec | None = None,
    name: str | QueryRef | None = None,
    params: Mapping[str, object] | None = None,
    at: AcceptedCoordinate | str | None = None,
    evaluation_time: datetime | str | None = None,
    status: Sequence[QueryClaimStatus] = ("live",),
    claims: bool = False,
    budgets: QueryBudgets | None = None,
    receipt: QueryReceiptDetail = "compact",
) -> QueryResult
```

Answers any question over accepted state in one call, exactly as the MCP tool
`cruxible_query` and `cruxible query` do. Exactly one mode:
compact (`kind` and/or `contains`, with `where`, `select`, `follow`,
`order_by`), a full `spec`, or a query `name` with `params`.

**Conditions and effects:** A `where` filter is `{"field": ..., "<op>": value}`
with one of `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in`, `exists`, `contains`;
filters combine as all-of and `ne` also matches a Subject without the value.
Wrong kinds, fields, enum members and operators refuse with the nearest valid
names. `QueryResult` has `.rows` (dicts of values plus `flags`), `.columns`,
`.truncated`, `.next_page()`, `.pages()`, `.table()` and iterates its rows. `subject`, `subject_id` and `flags` are row metadata; a column with one of those names is served as `value.<name>`. A
live connection reads the current head; a pinned one reads its coordinate.
Row `flags` come from `stale`, `contested`, `contradicted`, `uncovered` and
`unsure_hold`. With `claims=True` each row also carries
`claims[column]`: a list of `{claim, value, verdict, status, role, qualifier?}`
(`QueryClaim`), where `status` is `accepted`, `conflicted`,
`overturned`, `refused` or `retired`. A named query with `receipt="full"`
adds `receipt.replay`: `definition_path`, `result` (`ClaimQueryResult`) and
`execution` (`QueryExecutionReceipt`).

| Parameter | Default | Meaning |
|---|---|---|
| `kind` | `None` | A Subject kind, or `ClaimType` / `Procedure` for definitions. |
| `where` | `None` | Typed filters or plain mappings, all-of. |
| `contains` | `None` | Case-insensitive text in any live Claim value; alone, across kinds. |
| `select` | `None` | Column fields; without it a kind shows up to 12 predicates. |
| `follow` | `None` | One-hop relations as `(field, alias)`, or `(field, alias, "reverse")` along another kind's predicate that points here; later fields read `alias.field`. |
| `order_by` | `None` | Fields, `-` prefixed for descending. |
| `limit` / `cursor` | `50` / `None` | Rows per page (at most 500) and the `next_cursor` of the previous page. |
| `spec` / `name` / `params` | `None` | The spec and named modes. |
| `at` | `None` | A coordinate or git oid; the connection's otherwise. |
| `evaluation_time` | `None` | The instant flags are evaluated at; the connection's clock otherwise. |
| `status` | `("live",)` | Which Claims cells show: `live` (each slot's answer), plus opt-in `overturned`, `refused` or `retired`. Rows list live Subjects; `retired` also lists retired Subjects, each row then stating `lifecycle`. |
| `claims` | `False` | Also answer each cell's Claims as `rows[].claims[column]`. |
| `budgets` | `None` | Named query only: `QueryBudgets`, up to the definition's maximum; its own default otherwise. |
| `receipt` | `"compact"` | `"full"` adds a named query's replay receipt as `receipt.replay`. |

<a id="api-cruxible-since"></a>

### `Cruxible.since`

[Source](src/cruxible_client/authoring/sdk.py)

```text
since(
    generation: int,
    *,
    max_rows: int = 100,
    max_bytes: int = 65536,
    cursor: api.SinceCursor | Mapping[str, object] | None = None,
) -> api.SinceResult
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

<a id="api-cruxible-resolution-contracts"></a>

### `Cruxible.resolution_contracts`

[Source](src/cruxible_client/authoring/sdk.py)

```text
resolution_contracts(hypothesis: str | ClaimVersionReference) -> api.ResolutionContractsResult
```

Reads accepted tests of a Claim, including retired contracts, at cx.coordinate. `hypothesis` is a Claim ID; the daemon resolves its accepted version.

**Conditions and effects:** A history/snapshot read does not establish current execution authority.

| Parameter | Default | Meaning |
|---|---|---|
| `hypothesis` | Required | Claim ID (`CLM-...`) whose resolution contracts are requested; an exact `ClaimVersionReference` is the advanced form. |

<a id="api-cruxible-predict"></a>

### `Cruxible.predict`

[Source](src/cruxible_client/authoring/sdk.py)

```text
predict(contract: ResolutionContract | ResolutionContractInput) -> Prediction
```

Creates a governed proposal for an independent resolution contract over an already accepted hypothesis. The hypothesis may be a Claim ID (`ResolutionContractInput`); the daemon pins the exact version it resolves to. Returns proposal/intent identities.

**Conditions and effects:** Does not approve or accept the proposal.

| Parameter | Default | Meaning |
|---|---|---|
| `contract` | Required | Typed contract or exact contract reference named by the signature. |

<a id="api-cruxible-settle"></a>

### `Cruxible.settle`

[Source](src/cruxible_client/authoring/sdk.py)

```text
settle(
    prediction: str | ResolutionContractReference,
    *,
    observation: str | ClaimVersionReference,
    trigger_event: TriggerEventReference | None = None,
    terminal_run_id: str | None = None,
    terminal_record_digest: str | None = None,
) -> PredictionSettlement
```

Evaluates and records settlement for a prediction named by contract name or bound window id (`RSC-...`) from an observation named by Claim ID; the daemon resolves the exact contract, window and Claim version. Returns the mechanical Boolean outcome and relation.

**Conditions and effects:** terminal_run_id and terminal_record_digest must be supplied together. Missing/non-Boolean outcome refuses; procedure success alone is not settlement.

| Parameter | Default | Meaning |
|---|---|---|
| `prediction` | Required | Contract name or bound window id (`RSC-...`); an exact contract reference is the advanced form. |
| `observation` | Required | Claim ID of the accepted observation; an exact Claim version reference is the advanced form. |
| `trigger_event` | `None` | Retained trigger-event reference, when the contract/run requires event binding. |
| `terminal_run_id` | `None` | Run whose terminal evidence supports settlement; supplied together with terminal_record_digest. |
| `terminal_record_digest` | `None` | Exact retained terminal record; supplied together with terminal_run_id. |

<a id="api-cruxible-attest"></a>

### `Cruxible.attest`

[Source](src/cruxible_client/authoring/sdk.py)

```text
attest(
    claim: ClaimRef | str,
    *,
    stance: ClaimStance,
    signer: ClaimAttestationSigner,
    note: str | None = None,
    valid_until: datetime | None = None,
) -> ClaimAttestationAppendResult
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

<a id="api-cruxible-attest-new-capture"></a>

### `Cruxible.attest_new_capture`

[Source](src/cruxible_client/authoring/sdk.py)

```text
attest_new_capture(
    request: PreparedClaimAttestationRequest,
    *,
    signer: ClaimAttestationSigner,
) -> ClaimAttestationAppendResult
```

Signs and appends an already-staged new-capture attestation request.

**Conditions and effects:** attestation_basis must be new_capture; capture registration/acquisition is not invented by this method.

| Parameter | Default | Meaning |
|---|---|---|
| `request` | Required | Typed request specified by the signature; new-capture attestation requires that basis. |
| `signer` | Required | Caller-provisioned signing capability of the protocol required by this method. |

<a id="api-cruxible-next"></a>

### `Cruxible.next`

[Source](src/cruxible_client/authoring/sdk.py)

```text
next(*, expiring_within: Duration) -> NextPage
```

Scans the attached workspace and reads actionable work with explicit access profile, evaluation time, and expiry horizon.

**Conditions and effects:** Inspect observed_domains/unobserved_domains; an empty page does not imply every possible domain was observed. Each row's `repair.command` is the SDK call that performs it (for example `cx.arm_line("hourly")`), leaving out an operand only the caller holds, such as the signer or the observation; it is `None` when the SDK has no method for that repair.

| Parameter | Default | Meaning |
|---|---|---|
| `expiring_within` | Required | Duration defining the next-work expiry horizon. |

<a id="api-cruxible-audit"></a>

### `Cruxible.audit`

[Source](src/cruxible_client/authoring/sdk.py)

```text
audit(
    *,
    claim_type_identities: tuple[str, ...] = (),
    subject_kinds: tuple[str, ...] = (),
    max_rows: int = 100,
    max_bytes: int = 65536,
    cursor: api.AuditCursor | Mapping[str, object] | None = None,
) -> api.AuditResult
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

<a id="api-cruxible-curation-list"></a>

### `Cruxible.curation_list`

[Source](src/cruxible_client/authoring/sdk.py)

```text
curation_list(*, limit: int | None = None, cursor: str | None = None) -> api.CurationListResult
```

Performs an attributed workspace scan and reads one page of current operational
curation work (default 25 items). A truncated page carries `next_cursor`; pass it
back as `cursor`.

**Conditions and effects:** Operational state stays live even through a pinned accepted-reading context.

<a id="api-cruxible-curation-overrule"></a>

### `Cruxible.curation_overrule`

[Source](src/cruxible_client/authoring/sdk.py)

```text
curation_overrule(
    *,
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    attribution_refs: tuple[str, ...] = (),
) -> api.CurationActionResult
```

Records that the detector pattern is mechanically inapplicable.

**Conditions and effects:** Requires matching latest-event digest and attribution; does not revise accepted knowledge.

| Parameter | Default | Meaning |
|---|---|---|
| `item_id` | Required | Operational curation item identity. |
| `expected_latest_event_digest` | Required | Optimistic-concurrency assertion on the item’s latest event. |
| `reason` | Required | Attributed reason for refusal, retirement, or operational action as specified by the API. |
| `attribution_refs` | `()` | Retained references supporting attribution/reason for this operational action. |

<a id="api-cruxible-curation-accept-fixed"></a>

### `Cruxible.curation_accept_fixed`

[Source](src/cruxible_client/authoring/sdk.py)

```text
curation_accept_fixed(
    *,
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    accepted_proposal_id: str,
    accepted_changeset_digest: str,
    attribution_refs: tuple[str, ...] = (),
) -> api.CurationActionResult
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

<a id="api-cruxible-curation-suppress"></a>

### `Cruxible.curation_suppress`

[Source](src/cruxible_client/authoring/sdk.py)

```text
curation_suppress(
    *,
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    scope: Literal['item', 'pattern', 'instance'],
    until_generation: int | None = None,
    attribution_refs: tuple[str, ...] = (),
) -> api.CurationActionResult
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

<a id="api-cruxible-provider-binding"></a>

### `Cruxible.provider_binding`

[Source](src/cruxible_client/authoring/sdk.py)

```text
provider_binding(interface: str, *, provider: str | None=None) -> ProviderBinding
```

Discovers one accepted interface and selects one registered implementation.

**Conditions and effects:** No matching/unique interface, absent provider, or ambiguous implementation refuses. Does not install or authorize execution.

| Parameter | Default | Meaning |
|---|---|---|
| `interface` | Required | Accepted ProviderInterface name or qualified identity. |
| `provider` | `None` | Explicit accepted Provider selection. Omit only when discovery is unambiguous. |

<a id="api-cruxible-procedure"></a>

### `Cruxible.procedure`

[Source](src/cruxible_client/authoring/sdk.py)

```text
procedure(*, definition: ProcedureInput | ProcedureSequence) -> ProcedureDraft
```

Builds a ProcedureDraft from ProcedureInput or Sequence. Sequence is structurally built first.

**Conditions and effects:** Rejects unsupported SDK node kinds; accepts source/capture/proposal in graph v4/v5 and call in v5. Direct run-lane readiness is a separate check.

| Parameter | Default | Meaning |
|---|---|---|
| `definition` | Required | Typed definition being authored; use the exact input type shown in this operation’s signature. |

<a id="api-cruxible-accepted-procedure"></a>

### `Cruxible.accepted_procedure`

[Source](src/cruxible_client/authoring/sdk.py)

```text
accepted_procedure(procedure: str | ProcedureRef) -> Procedure
```

Returns an accepted Procedure handle; a typed ProcedureRef retains its coordinate.

**Conditions and effects:** Does not execute; a name on a live context follows live reads. Use a pinned context or ref for a fixed selection.

| Parameter | Default | Meaning |
|---|---|---|
| `procedure` | Required | Procedure name or typed reference; Line authoring also accepts a name defined earlier in the same changeset. |

<a id="api-cruxible-run-line"></a>

### `Cruxible.run_line`

[Source](src/cruxible_client/authoring/sdk.py)

```text
run_line(
    line: str,
    *,
    trigger: str | None = None,
    occurrence_id: str | None = None,
    resolution_contract: ResolutionContractReference | None = None,
    trigger_event: TriggerEventReference | None = None,
) -> ProcedureRun
```

Requests one daemon-derived occurrence of a named accepted Line and returns a ProcedureRun.
The backend resolves the Line identity; callers do not pass its digest.

**Conditions and effects:** May invoke providers, register captures, or submit proposals under admitted authority; it is not a preview or permission grant.

| Parameter | Default | Meaning |
|---|---|---|
| `line` | Required | Accepted Line name. The daemon resolves and binds its exact identity and version. |
| `trigger` | `None` | The Trigger this occurrence fires on. Required when live Triggers aim at the Line; omit it only for a Line with none, which runs when run explicitly. |
| `occurrence_id` | `None` | Optional retained occurrence identity; the daemon validates/derives its binding. |
| `resolution_contract` | `None` | Exact independent resolution-contract reference bound to this run. |
| `trigger_event` | `None` | Retained trigger-event reference, when the contract/run requires event binding. |

<a id="api-changesetdraft"></a>

## `ChangeSetDraft`

Import: `cruxible_client.authoring.sdk.ChangeSetDraft`. [Source](src/cruxible_client/authoring/sdk.py)

Obtained from `cx.changes(rationale=...)`. Methods stage members; `prepare()`
preflights the whole set and `submit()` compiles and submits it in one request. `claim`, `procedure`, `query_definition`, `line`, `trigger`, policy,
contract, and attestation methods return this builder for chaining unless their
signature returns a pending ref. The pending Subject/ClaimType references may
be used by sibling Claims without pretending they already exist in accepted state.

| Field | Type | Default / construction |
|---|---|---|
| `rationale` | `str \| None` | `None` |

### Member semantics

| Member | Return and effect | Additional requirements |
|---|---|---|
| `claim` | Stages one Claim; returns builder | Same value/evidence rules as `Cruxible.claim`. |
| `subject` | Stages a definition; returns PendingSubjectRef | Sibling Claims may use it. |
| `claim_type` | Stages a definition; returns PendingClaimTypeRef | Use succeed_claim_type for an accepted predecessor. |
| `succeed_claim_type` | Stages a full successor and closure dispositions | Exact reverse-pin closure; sibling reauthors retain Claim identity/subject/predicate. |
| `retire` | Stages an attributed Claim retirement | Explicit required dependents. |
| `signed_attestation` | Stages supplied signed bytes | Does not re-sign or accept them. |
| `attestation` | Reads/prepares the exact Claim, signs locally, then stages | Explicit signer; no implicit approval of the containing changeset. |
| `capture_contract`, `resolution_contract`, `acquisition_policy` | Stage typed definitions | Existing artifact contracts and normal admission apply. |
| `procedure`, `query_definition`, `procedure_mandate` | Stage the shared authoring inputs | Same validation as the corresponding individual draft. |
| `line` | Stages a Line naming its Procedure and acquisition policy | Runs explicitly until a Trigger aims at it; omitted budgets inherit Procedure hard caps; admission still checks live authority. |
| `trigger` | Stages a Trigger: one schedule aimed at one Line or internal action | A Line takes any schedule that supplies its input; an internal action takes cadence or cron only in this version. Cron is always evaluated in UTC. |
| `prepare` | Creates/preflights one durable intent | Atomic decision; malformed member refuses the set. |

Definitions and successions precede dependent Claims during lowering. Two
members cannot write the same artifact path. A live Claim cannot be carried
unchanged through an object-kind change. The source of truth remains accepted
artifacts; staging a definition does not mutate accepted state.

<a id="api-changesetdraft-claim"></a>

### `ChangeSetDraft.claim`

[Source](src/cruxible_client/authoring/sdk.py)

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

Add one Claim to this changeset; the signature is `Cruxible.claim`'s.

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

[Source](src/cruxible_client/authoring/sdk.py)

```text
signed_attestation(attestation: ClaimAttestation) -> ChangeSetDraft
```

Add an already signed statement, without changing its bytes or Claim.

It becomes accepted only when this changeset passes ordinary approval
and activation. The authenticated submitter need not be its signer.

| Parameter | Default | Meaning |
|---|---|---|
| `attestation` | Required | Already signed immutable ClaimAttestation envelope. |

<a id="api-changesetdraft-attestation"></a>

### `ChangeSetDraft.attestation`

[Source](src/cruxible_client/authoring/sdk.py)

```text
attestation(
    claim: ClaimRef | str,
    *,
    stance: ClaimStance,
    signer: ClaimAttestationSigner,
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

[Source](src/cruxible_client/authoring/sdk.py)

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

[Source](src/cruxible_client/authoring/sdk.py)

```text
capture_contract(contract: CaptureContract) -> ChangeSetDraft
```

Define one CaptureContract inside this changeset.

| Parameter | Default | Meaning |
|---|---|---|
| `contract` | Required | Typed contract or exact contract reference named by the signature. |

<a id="api-changesetdraft-resolution-contract"></a>

### `ChangeSetDraft.resolution_contract`

[Source](src/cruxible_client/authoring/sdk.py)

```text
resolution_contract(contract: ResolutionContract) -> ChangeSetDraft
```

Define one ResolutionContract inside this changeset.

| Parameter | Default | Meaning |
|---|---|---|
| `contract` | Required | Typed contract or exact contract reference named by the signature. |

<a id="api-changesetdraft-acquisition-policy"></a>

### `ChangeSetDraft.acquisition_policy`

[Source](src/cruxible_client/authoring/sdk.py)

```text
acquisition_policy(policy: SourceAcquisitionPolicy) -> ChangeSetDraft
```

Define one SourceAcquisitionPolicy inside this changeset.

| Parameter | Default | Meaning |
|---|---|---|
| `policy` | Required | Explicit typed policy for the corresponding operation. |

<a id="api-changesetdraft-procedure"></a>

### `ChangeSetDraft.procedure`

[Source](src/cruxible_client/authoring/sdk.py)

```text
procedure(*, definition: ProcedureInput | ProcedureSequence) -> ChangeSetDraft
```

Compose a Procedure with its Line and mandate in one existing changeset.

| Parameter | Default | Meaning |
|---|---|---|
| `definition` | Required | Typed definition being authored; use the exact input type shown in this operation’s signature. |

<a id="api-changesetdraft-procedure-mandate"></a>

### `ChangeSetDraft.procedure_mandate`

[Source](src/cruxible_client/authoring/sdk.py)

```text
procedure_mandate(definition: ProcedureMandateInput) -> ChangeSetDraft
```

Stage a typed mandate through the shared authoring-input lowering.

| Parameter | Default | Meaning |
|---|---|---|
| `definition` | Required | Typed definition being authored; use the exact input type shown in this operation’s signature. |

<a id="api-changesetdraft-line"></a>

### `ChangeSetDraft.line`

[Source](src/cruxible_client/authoring/sdk.py)

```text
line(
    *,
    name: str,
    procedure: str,
    acquisition_policy: str | None = None,
    max_authority: Literal["observe", "propose", "settle"] | None = None,
    trigger_input: str | None = None,
    parameters: CanonicalValue | None = None,
    budgets: Mapping[str, int] | None = None,
    occurrence_epoch: int = 1,
    retire: bool = False,
) -> ChangeSetDraft
```

Define one Line inside this changeset, naming its Procedure and policy.

Define the Line with `line(...)`, then author its schedule with
`trigger(...)`. Cron expressions are always evaluated in UTC.

Lowering resolves both names -- accepted at the base or defined earlier
in this same set -- into the exact pins the LineSpec (v6) carries. A Line
runs when run explicitly, or when a Trigger aimed at it fires, and
inherits the Procedure's hard caps as its budget unless one is given.
``trigger_input`` binds the triggering Capture to a named Source alias; the
Line then accepts only that Source's exact CaptureContract event, and every
Trigger aimed at it must fire on it.

Lowering refuses a Procedure that is not graph-v4/v5/v6 and one whose
Source nodes leave a Provider slot open: the Line pins exactly what the
Procedure names, and an open slot is nothing to pin.
``acquisition_policy`` is required only when the Procedure has Source
nodes. ``parameters`` is the Procedure's input record; lowering checks it
against the Procedure's input contract. ``max_authority`` (observe,
propose or settle) caps this Line below its Procedure's own capability
and defaults to it. A Line that proposes or settles also needs a live
ProcedureMandate covering its Procedure before it can run or be armed;
an observe-only Line needs none.

| Parameter | Default | Meaning |
|---|---|---|
| `name` | Required | Definition identity name, not an arbitrary file path. |
| `procedure` | Required | Procedure name or typed reference; Line authoring also accepts a name defined earlier in the same changeset. |
| `acquisition_policy` | `None` | Accepted or same-set SourceAcquisitionPolicy name; required only when the Procedure has Source nodes. |
| `max_authority` | `None` | Most this Line may do: `observe`, `propose` or `settle`. Defaults to its Procedure's capability; effective authority is checked at admission. |
| `trigger_input` | `None` | Source alias receiving the exact triggering Capture; the Line accepts only that Source's CaptureContract event, and Triggers aimed at it must fire on it. |
| `parameters` | `None` | Invocation/query parameters in the declared canonical contract. |
| `budgets` | `None` | Operation-specific bounds; the signature distinguishes QueryBudgets from Line budget mappings. |
| `occurrence_epoch` | `1` | Positive epoch distinguishing Line occurrence identity; it advances when the accepted event changes. |
| `retire` | `False` | Whether the authored Line is a retirement. Live Triggers aimed at it must be retired or retargeted in the same set. |

<a id="api-changesetdraft-trigger"></a>

### `ChangeSetDraft.trigger`

[Source](src/cruxible_client/authoring/sdk.py)

```text
trigger(
    *,
    name: str,
    schedule: TriggerSchedule,
    line: str | None = None,
    action: InternalActionName | None = None,
    retire: bool = False,
) -> ChangeSetDraft
```

Define one Trigger inside this changeset: a schedule aimed at one target.
Name exactly one of `line` (an accepted Line, or one defined in this same
set) or `action` (a registered internal action: `evidence.sweep` or
`prediction.anchor_retry`). A Line can have several Triggers.

`schedule` is a `CadenceSchedule(interval_seconds=...)`,
`CronSchedule(expression=...)`, `CaptureLandingSchedule(event=...)` or
`WindowCloseSchedule(window=...)`. Cron expressions are always evaluated in
UTC: convert local times first (09:00 New York in winter is 14:00 UTC); a
schedule that names a timezone is refused. An internal action takes a cadence
or cron schedule only in this version. A Line takes any schedule that supplies
its input: a Line with `trigger_input` needs one that fires on its exact
CaptureContract event. No Trigger fires retroactively: a new cadence first
fires one interval after acceptance, a cron schedule at its first instant
after acceptance, and Captures and windows count only when strictly after it.

```python
draft = cx.changes(rationale="Triage new findings each weekday morning")
draft.line(name="triage", procedure="triage-procedure")
draft.trigger(
    name="triage-weekdays",
    schedule=CronSchedule(expression="0 14 * * 1-5"),  # 09:00 New York (EST), in UTC
    line="triage",
)
draft.submit()
```

| Parameter | Default | Meaning |
|---|---|---|
| `name` | Required | Trigger identity name (`triggers/<name>.json`). |
| `schedule` | Required | Cadence, cron (UTC), capture landing, or window close. |
| `line` | `None` | Accepted or same-set Line this Trigger runs; omit when naming an action. |
| `action` | `None` | Registered internal action this Trigger fires (cadence or cron only); omit for a Line. |
| `retire` | `False` | Whether the authored Trigger is a retirement. |

<a id="api-changesetdraft-query-definition"></a>

### `ChangeSetDraft.query_definition`

[Source](src/cruxible_client/authoring/sdk.py)

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

[Source](src/cruxible_client/authoring/sdk.py)

```text
claim_type(definition: ClaimTypeDraft | ClaimType) -> PendingClaimTypeRef
```

| Parameter | Default | Meaning |
|---|---|---|
| `definition` | Required | Typed definition being authored; use the exact input type shown in this operation’s signature. |

<a id="api-changesetdraft-retire"></a>

### `ChangeSetDraft.retire`

[Source](src/cruxible_client/authoring/sdk.py)

```text
retire(
    claim: str | ClaimRef,
    *,
    reason: ClaimRetirementReason,
    effective_until: datetime | None = None,
    dependents: Sequence[ClaimRetireDependent] = (),
) -> ChangeSetDraft
```

Retire one accepted Claim, and its live closure, inside this changeset.

Takes a Claim ID in either spelling the SDK's rows and refs use
(`CLM-...` or `Claim:CLM-...`). The member carries the one canonical bare
spelling as `retires`, which is what keeps two spellings of one
retirement on one member identity and one digest. `Cruxible.retire` is
the typed write verb for the common case: it computes the closure.

| Parameter | Default | Meaning |
|---|---|---|
| `claim` | Required | Claim ID or typed ClaimRef; typed refs also assert their observed coordinate. |
| `reason` | Required | Attributed reason for refusal, retirement, or operational action as specified by the API. |
| `effective_until` | `None` | Optional effective-end instant for retirement. |
| `dependents` | `()` | Explicit dependency-closure dispositions required by retirement or ClaimType succession. |

<a id="api-changesetdraft-succeed-claim-type"></a>

### `ChangeSetDraft.succeed_claim_type`

[Source](src/cruxible_client/authoring/sdk.py)

```text
succeed_claim_type(
    successor: ClaimTypeDraft | ClaimType,
    *,
    dependents: Sequence[ClaimTypeSuccessionDependent] = (),
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

[Source](src/cruxible_client/authoring/sdk.py)

```text
prepare() -> Intent
```

Compile and preflight the whole changeset as one intent.

## Drafts, intents, proposals, and approvals

`ClaimDraft`, `ProcedureDraft`, `QueryDraft`, and `SubjectDraft` inherit
`prepare() -> Intent` and `submit() -> Intent`. They expose `payload`, `reference_expectations`,
`program_stamp`, and `source_map` for inspection. `prepare()` performs server
compilation/preflight; `submit()` compiles and submits in one request, with the
daemon preflighting once, and a refused intent carries the same `refused` and
`diagnostics` that `prepare()` reports; the program stamp is structured authoring provenance,
not retained executable Python source. `ClaimTypeDraft.propose(...)` is a direct
proposal helper and submits immediately; it is not a synonym for local staging.

`Intent` and `Proposal` are obtained from SDK factories. `from_preflight` and
`from_inspection` are advanced response adapters. Cached properties are not
automatic status polling. `Intent.path_to_acceptance` calls status.
`Intent.rebase()` clears observed preflight/status; prepare again before relying
on a new candidate. `reprepare()` requires the same owning connection.

`Proposal.review()` returns an immutable process-local `ReviewedProposal`.
`approve(signer=..., reviewed=...)` checks exact proposal/candidate/session/instance
binding, obtains current governance/challenge data, signs locally, and submits
public approval. Its `.details` returns a fresh copy. The token is not proof a
human read it. A new process must review again. Wait methods return the last
observed status at timeout; they do not throw a synthetic failure or retry writes.

<a id="api-claimdraft"></a>

## `ClaimDraft`

Import: `cruxible_client.authoring.sdk.ClaimDraft`. [Source](src/cruxible_client/authoring/sdk.py)

| Field | Type | Default / construction |
|---|---|---|
| `payload` | `ClaimAuthoringPayloadV1 \| ClaimAuthoringPayloadV2 \| ClaimAuthoringPayload \| ProcedureAuthoringPayloadV1 \| ProcedureAuthoringPayload \| SubjectAuthoringPayload \| ChangeSetAuthoringPayload \| QueryDefinitionAuthoringPayload` | `Required` |
| `reference_expectations` | `tuple[AuthoringReferenceExpectation, ...]` | `Required` |
| `program_stamp` | `AuthoringProgramStamp` | `Required` |
| `source_map` | `DiagnosticSourceMap` | `Required` |

<a id="api-claimdraft-derived-by"></a>

### `ClaimDraft.derived_by`

[Source](src/cruxible_client/authoring/sdk.py)

```text
derived_by(derivation: object) -> ClaimDraft
```

<a id="api-proceduredraft"></a>

## `ProcedureDraft`

Import: `cruxible_client.authoring.sdk.ProcedureDraft`. [Source](src/cruxible_client/authoring/sdk.py)

| Field | Type | Default / construction |
|---|---|---|
| `payload` | `ClaimAuthoringPayloadV1 \| ClaimAuthoringPayloadV2 \| ClaimAuthoringPayload \| ProcedureAuthoringPayloadV1 \| ProcedureAuthoringPayload \| SubjectAuthoringPayload \| ChangeSetAuthoringPayload \| QueryDefinitionAuthoringPayload` | `Required` |
| `reference_expectations` | `tuple[AuthoringReferenceExpectation, ...]` | `Required` |
| `program_stamp` | `AuthoringProgramStamp` | `Required` |
| `source_map` | `DiagnosticSourceMap` | `Required` |

<a id="api-querydraft"></a>

## `QueryDraft`

Import: `cruxible_client.authoring.sdk.QueryDraft`. [Source](src/cruxible_client/authoring/sdk.py)

| Field | Type | Default / construction |
|---|---|---|
| `payload` | `ClaimAuthoringPayloadV1 \| ClaimAuthoringPayloadV2 \| ClaimAuthoringPayload \| ProcedureAuthoringPayloadV1 \| ProcedureAuthoringPayload \| SubjectAuthoringPayload \| ChangeSetAuthoringPayload \| QueryDefinitionAuthoringPayload` | `Required` |
| `reference_expectations` | `tuple[AuthoringReferenceExpectation, ...]` | `Required` |
| `program_stamp` | `AuthoringProgramStamp` | `Required` |
| `source_map` | `DiagnosticSourceMap` | `Required` |

<a id="api-subjectdraft"></a>

## `SubjectDraft`

Import: `cruxible_client.authoring.sdk.SubjectDraft`. [Source](src/cruxible_client/authoring/sdk.py)

| Field | Type | Default / construction |
|---|---|---|
| `payload` | `ClaimAuthoringPayloadV1 \| ClaimAuthoringPayloadV2 \| ClaimAuthoringPayload \| ProcedureAuthoringPayloadV1 \| ProcedureAuthoringPayload \| SubjectAuthoringPayload \| ChangeSetAuthoringPayload \| QueryDefinitionAuthoringPayload` | `Required` |
| `reference_expectations` | `tuple[AuthoringReferenceExpectation, ...]` | `Required` |
| `program_stamp` | `AuthoringProgramStamp` | `Required` |
| `source_map` | `DiagnosticSourceMap` | `Required` |
| `shell` | `SubjectShell` | `Required` |

<a id="api-subjectdraft-address"></a>

### `SubjectDraft.address`

[Source](src/cruxible_client/authoring/sdk.py)

```text
address: str
```

<a id="api-claimtypedraft"></a>

## `ClaimTypeDraft`

Import: `cruxible_client.authoring.sdk.ClaimTypeDraft`. [Source](src/cruxible_client/authoring/sdk.py)

| Field | Type | Default / construction |
|---|---|---|
| `definition` | `ClaimType` | `Required` |

<a id="api-claimtypedraft-predicate"></a>

### `ClaimTypeDraft.predicate`

[Source](src/cruxible_client/authoring/sdk.py)

```text
predicate: str
```

<a id="api-claimtypedraft-propose"></a>

### `ClaimTypeDraft.propose`

[Source](src/cruxible_client/authoring/sdk.py)

```text
propose(*, proposal_name: str) -> Proposal
```

<a id="api-intent"></a>

## `Intent`

Import: `cruxible_client.authoring.sdk.Intent`. [Source](src/cruxible_client/authoring/sdk.py)

<a id="api-intent-from-preflight"></a>

### `Intent.from_preflight`

[Source](src/cruxible_client/authoring/sdk.py)

```text
from_preflight(
    cx: Cruxible,
    draft: _IntentDraft,
    result: api.AuthoringPreflightResult,
) -> Intent
```

<a id="api-intent-intent-id"></a>

### `Intent.intent_id`

[Source](src/cruxible_client/authoring/sdk.py)

```text
intent_id: str
```

<a id="api-intent-revision"></a>

### `Intent.revision`

[Source](src/cruxible_client/authoring/sdk.py)

```text
revision: int
```

<a id="api-intent-refused"></a>

### `Intent.refused`

[Source](src/cruxible_client/authoring/sdk.py)

```text
refused: bool
```

<a id="api-intent-lint"></a>

### `Intent.lint`

[Source](src/cruxible_client/authoring/sdk.py)

```text
lint: api.ClaimTypeProposalLint | None
```

<a id="api-intent-warnings"></a>

### `Intent.warnings`

[Source](src/cruxible_client/authoring/sdk.py)

```text
warnings: tuple[dict[str, Any], ...]
```

<a id="api-intent-diagnostics"></a>

### `Intent.diagnostics`

[Source](src/cruxible_client/authoring/sdk.py)

```text
diagnostics: tuple[Diagnostic, ...]
```

<a id="api-intent-path-to-acceptance"></a>

### `Intent.path_to_acceptance`

[Source](src/cruxible_client/authoring/sdk.py)

```text
path_to_acceptance: tuple[dict[str, object], ...]
```

<a id="api-intent-proposal"></a>

### `Intent.proposal`

[Source](src/cruxible_client/authoring/sdk.py)

```text
proposal: Proposal | None
```

Last observed proposal identity, without a server read.

Populated by resume_intent(), submit() or status(); None means no proposal was observed
for this local intent revision. Call status() for fresh server state.
This handle is not review, approval, or proof of activation eligibility.

<a id="api-intent-prepare"></a>

### `Intent.prepare`

[Source](src/cruxible_client/authoring/sdk.py)

```text
prepare() -> Intent
```

<a id="api-intent-reprepare"></a>

### `Intent.reprepare`

[Source](src/cruxible_client/authoring/sdk.py)

```text
reprepare(*, draft: ClaimDraft | ProcedureDraft | SubjectDraft) -> Intent
```

<a id="api-intent-submit"></a>

### `Intent.submit`

[Source](src/cruxible_client/authoring/sdk.py)

```text
submit() -> Intent
```

<a id="api-intent-status"></a>

### `Intent.status`

[Source](src/cruxible_client/authoring/sdk.py)

```text
status() -> api.CandidateStatusRecord
```

<a id="api-intent-rebase"></a>

### `Intent.rebase`

[Source](src/cruxible_client/authoring/sdk.py)

```text
rebase() -> Intent
```

<a id="api-intent-wait-for-acceptance"></a>

### `Intent.wait_for_acceptance`

[Source](src/cruxible_client/authoring/sdk.py)

```text
wait_for_acceptance(*, timeout: Duration, poll_interval: Duration) -> api.CandidateStatusRecord
```

<a id="api-proposal"></a>

## `Proposal`

Import: `cruxible_client.authoring.sdk.Proposal`. [Source](src/cruxible_client/authoring/sdk.py)

<a id="api-proposal-from-inspection"></a>

### `Proposal.from_inspection`

[Source](src/cruxible_client/authoring/sdk.py)

```text
from_inspection(cx: Cruxible, inspection: api.ProposalInspection) -> Proposal
```

<a id="api-proposal-review"></a>

### `Proposal.review`

[Source](src/cruxible_client/authoring/sdk.py)

```text
review() -> ReviewedProposal
```

Fetch an immutable full review; inspect its details before approving.

<a id="api-proposal-accept"></a>

### `Proposal.accept`

[Source](src/cruxible_client/authoring/sdk.py)

```text
accept() -> api.ActivationReceipt
```

Accepts this proposal once its approvals are in: `Cruxible.accept` by handle.

<a id="api-proposal-approve"></a>

### `Proposal.approve`

[Source](src/cruxible_client/authoring/sdk.py)

```text
approve(*, signer: ApprovalSigner, reviewed: ReviewedProposal) -> api.ApprovalReceipt
```

Sign this exact review with caller-configured custody; never activate.

<a id="api-proposal-warnings"></a>

### `Proposal.warnings`

[Source](src/cruxible_client/authoring/sdk.py)

```text
warnings: tuple[dict[str, Any], ...]
```

<a id="api-proposal-status"></a>

### `Proposal.status`

[Source](src/cruxible_client/authoring/sdk.py)

```text
status() -> api.ProposalListEntry
```

<a id="api-proposal-wait-for-acceptance"></a>

### `Proposal.wait_for_acceptance`

[Source](src/cruxible_client/authoring/sdk.py)

```text
wait_for_acceptance(*, timeout: Duration, poll_interval: Duration) -> api.ProposalListEntry
```

<a id="api-prediction"></a>

## `Prediction`

Import: `cruxible_client.authoring.sdk.Prediction`. [Source](src/cruxible_client/authoring/sdk.py)

| Field | Type | Default / construction |
|---|---|---|
| `contract_identity` | `str` | `Required` |
| `contract_digest` | `str` | `Required` |
| `intent_id` | `str` | `Required` |
| `proposal_id` | `str` | `Required` |

<a id="api-prediction-proposal"></a>

### `Prediction.proposal`

[Source](src/cruxible_client/authoring/sdk.py)

```text
proposal: Proposal
```

<a id="api-predictionsettlement"></a>

## `PredictionSettlement`

Import: `cruxible_client.authoring.sdk.PredictionSettlement`. [Source](src/cruxible_client/authoring/sdk.py)

| Field | Type | Default / construction |
|---|---|---|
| `prediction_id` | `str` | `Required` |
| `outcome` | `bool` | `Required` |
| `relation` | `dict[str, object]` | `Required` |

<a id="api-claimview"></a>

## `ClaimView`

Import: `cruxible_client.authoring.sdk.ClaimView`. [Source](src/cruxible_client/authoring/sdk.py)

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

Import: `cruxible_client.authoring.sdk.KnowledgeCard`. [Source](src/cruxible_client/authoring/sdk.py)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `RefKind` | `Required` |
| `identity` | `str` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `value` | `object` | `Required` |

<a id="api-knowledgecard-ref"></a>

### `KnowledgeCard.ref`

[Source](src/cruxible_client/authoring/sdk.py)

```text
ref: TypedRef
```

<a id="api-nextpage"></a>

## `NextPage`

Import: `cruxible_client.authoring.sdk.NextPage`. [Source](src/cruxible_client/authoring/sdk.py)

| Field | Type | Default / construction |
|---|---|---|
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `evaluation_time` | `str` | `Required` |
| `items` | `tuple[dict[str, object], ...]` | `Required` |
| `result_digest` | `str` | `Required` |
| `observed_domains` | `tuple[str, ...]` | `Required` |
| `unobserved_domains` | `tuple[str, ...]` | `Required` |
| `status` | `NextStatus` | `Required` |
| `attestation_head_digest` | `str \| None` | `None` |

`NextPage.hidden` is `status.hidden`: the rows and nested findings left out because this caller cannot perform their repair. An empty page with a nonzero `hidden` is not an empty queue.

<a id="api-nextpage-iter"></a>

### `NextPage.__iter__`

[Source](src/cruxible_client/authoring/sdk.py)

```text
__iter__()
```

<a id="api-measurementoutcome"></a>

## `MeasurementOutcome`

Import: `cruxible_client.authoring.sdk.MeasurementOutcome`. [Source](src/cruxible_client/authoring/sdk.py)

| Field | Type | Default / construction |
|---|---|---|
| `measurement_name` | `str` | `Required` |
| `status` | `str` | `Required` |
| `reading_status` | `str` | `Required` |
| `verdict` | `str \| None` | `Required` |
| `resolution_id` | `str \| None` | `Required` |
| `reading_id` | `str \| None` | `Required` |
| `detail` | `str \| None` | `Required` |
| `raw` | `api.ProcedureMeasurementRow` | `Required` |

<a id="api-measurementbatch"></a>

## `MeasurementBatch`

Import: `cruxible_client.authoring.sdk.MeasurementBatch`. [Source](src/cruxible_client/authoring/sdk.py)

| Field | Type | Default / construction |
|---|---|---|
| `run_id` | `str \| None` | `Required` |
| `activation_coordinate` | `AcceptedCoordinate` | `Required` |
| `observation_coordinate` | `AcceptedCoordinate` | `Required` |
| `observation_time` | `datetime` | `Required` |
| `outcomes` | `tuple[MeasurementOutcome, ...]` | `Required` |
| `raw` | `api.ProcedureMeasureResult` | `Required` |

<a id="api-measurementbatch-getitem"></a>

### `MeasurementBatch.__getitem__`

[Source](src/cruxible_client/authoring/sdk.py)

```text
__getitem__(measurement_name: str) -> MeasurementOutcome
```

<a id="api-reviewedproposal"></a>

## `ReviewedProposal`

Import: `cruxible_client.authoring.approval.ReviewedProposal`. [Source](src/cruxible_client/authoring/approval.py)

Use `Proposal.review()`; `.details` is a defensive copy of the exact review.

| Field | Type | Default / construction |
|---|---|---|
| `proposal_id` | `str` | `Required` |
| `candidate_digest` | `str` | `Required` |

<a id="api-reviewedproposal-details"></a>

### `ReviewedProposal.details`

[Source](src/cruxible_client/authoring/approval.py)

```text
details: api.ProposalReview
```

<a id="api-authoring-sdk-carry"></a>

### `authoring.sdk.carry`

[Source](src/cruxible_client/authoring/sdk.py)

```text
carry(claim: str | ClaimRef) -> ClaimTypeSuccessionDependent
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

[Source](src/cruxible_client/authoring/sdk.py)

```text
rescind(claim: str | ClaimRef) -> ClaimTypeSuccessionDependent
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

[Source](src/cruxible_client/authoring/sdk.py)

```text
retire(
    claim: str | ClaimRef,
    *,
    reason: ClaimRetirementReason,
    effective_until: datetime | None = None,
) -> ClaimTypeSuccessionDependent
```

Retire one dependent with an attributed reason as the succession lands.

| Parameter | Default | Meaning |
|---|---|---|
| `claim` | Required | Claim ID or typed ClaimRef; typed refs also assert their observed coordinate. |
| `reason` | Required | Attributed reason for refusal, retirement, or operational action as specified by the API. |
| `effective_until` | `None` | Optional effective-end instant for retirement. |

<a id="api-authoring-sdk-re-author"></a>

### `authoring.sdk.re_author`

[Source](src/cruxible_client/authoring/sdk.py)

```text
re_author(claim: str | ClaimRef, *, with_: str | ClaimRef | None=None) -> ClaimTypeSuccessionDependent
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
| `kind.where(state="open", count__gt=3)` | CompactQuery; `.select(...)`, `.order_by(...)`, `.limit(n)`, `.run()` or iterate every page |
| `world.stub()` | Generated type-stub source for this vocabulary |

`prefetch` installs only complete bounded selections. An exhausted budget,
invalid coordinate, duplicate row, or non-advancing continuation refuses before
a partial cache is installed. Current `ClaimView.value` is annotated `object`;
the constrained vocabulary and daemon checks are stronger than that annotation.
Generated stubs expose declared names and Claim tuples; they do not provide
the proposed typed Procedure field-selection wrapper.

<a id="api-world"></a>

## `World`

Import: `cruxible_client.authoring.world.World`. [Source](src/cruxible_client/authoring/world.py)

<a id="api-world-coordinate"></a>

### `World.coordinate`

[Source](src/cruxible_client/authoring/world.py)

```text
coordinate: AcceptedCoordinate
```

<a id="api-world-kinds"></a>

### `World.kinds`

[Source](src/cruxible_client/authoring/world.py)

```text
kinds: tuple[str, ...]
```

Every accepted Subject kind this world knows, byte-sorted.

<a id="api-world-predicates"></a>

### `World.predicates`

[Source](src/cruxible_client/authoring/world.py)

```text
predicates: tuple[str, ...]
```

Every accepted predicate this world knows, byte-sorted.

<a id="api-world-stub"></a>

### `World.stub`

[Source](src/cruxible_client/authoring/world.py)

```text
stub() -> str
```

Render this world as a `.pyi` module stub.

<a id="api-world-claim-type"></a>

### `World.claim_type`

[Source](src/cruxible_client/authoring/world.py)

```text
claim_type(predicate: str) -> WorldClaimType
```

Read one accepted predicate by its full dotted name.

<a id="api-world-kind"></a>

### `World.kind`

[Source](src/cruxible_client/authoring/world.py)

```text
kind(subject_kind: str) -> KindNamespace
```

Read one accepted Subject kind by its full dotted name.

The escape for a kind whose segments attribute access cannot spell -- a
Python keyword such as `dev.class` -- and for one a predicate of the same
dotted name wins, exactly as `claim_type` is the escape for a predicate.

<a id="api-world-prefetch"></a>

### `World.prefetch`

[Source](src/cruxible_client/authoring/world.py)

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

<a id="api-world-values"></a>

### `World.values`

```text
values(
    *,
    subjects: Sequence[str | SubjectRef],
    predicates: Sequence[str | ClaimTypeRef] = (),
) -> tuple[api.QueryClaimValue, ...]
```

Each live Claim's value, verdict and status for these Subjects, read through
`query` (one `query(kind, where=subject_id in ..., claims=True,
status=("live", "overturned", "refused"))` per Subject kind, pinned to this
World's coordinate) and without full Claim views -- the cheaper read when only
values and verdicts are needed. Every live contender of each selected slot is
returned, including Claims resolution overturned or refused. Each
`QueryClaimValue` carries `subject` (the Subject's `kind/id`),
`predicate`, `claim`, `value`, `verdict`, `status` (`accepted`, `conflicted`,
`overturned`, `refused` or `retired`), `role` and, when present, `qualifier`.
Strings are Subject `kind/id` addresses or paths and fully qualified
predicates; with no `predicates`, every predicate of each kind is read. Pages
are followed to the end at the same coordinate.

<a id="api-worldsubject"></a>

## `WorldSubject`

Import: `cruxible_client.authoring.world.WorldSubject`. [Source](src/cruxible_client/authoring/world.py)

Writes by field leaf: `subject.set(status="done", because=...)` sets
single-value fields, `subject.add(governs=other, because=...)` adds to
many-valued ones (`expect_absent=True` refuses a value already there instead of
answering it as done), and `subject.retire("status", because=...)` retires a
field's one live value. Every field in one call is one change of one change set, and
names are checked against the World before the wire. A World's writes are
checked from its coordinate advanced past its own accepted writes, so its
references stay valid after them; only a field someone else moved refuses. The
generated stub types `set` and `add` per kind, enum members as `Literal`s.

<a id="api-worldsubject-subject-kind"></a>

### `WorldSubject.subject_kind`

[Source](src/cruxible_client/authoring/world.py)

```text
subject_kind: str
```

<a id="api-worldsubject-subject-id"></a>

### `WorldSubject.subject_id`

[Source](src/cruxible_client/authoring/world.py)

```text
subject_id: str
```

<a id="api-worldsubject-claims"></a>

### `WorldSubject.claims`

[Source](src/cruxible_client/authoring/world.py)

```text
claims: tuple[ClaimView, ...]
```

Every live Claim this Subject is the subject of.

<a id="api-worldsubject-explain"></a>

### `WorldSubject.explain`

[Source](src/cruxible_client/authoring/world.py)

```text
explain() -> object
```

Read this Subject's governance and provenance context: the `why` payload of `get(subject, detail="why")` at this World's coordinate.

<a id="api-worldsubject-getitem"></a>

### `WorldSubject.__getitem__`

[Source](src/cruxible_client/authoring/world.py)

```text
__getitem__(predicate: str | ClaimTypeRef) -> tuple[ClaimView, ...]
```

Read the live Claims under one predicate, named in full or by leaf.

<a id="api-worldclaimtype"></a>

## `WorldClaimType`

Import: `cruxible_client.authoring.world.WorldClaimType`. [Source](src/cruxible_client/authoring/world.py)

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

[Source](src/cruxible_client/authoring/world.py)

```text
predicate: str
```

<a id="api-worldclaimtype-members"></a>

### `WorldClaimType.members`

[Source](src/cruxible_client/authoring/world.py)

```text
members: tuple[str, ...]
```

Return the enum members this predicate's literal schema names.

<a id="api-worldclaimtype-as-kind"></a>

### `WorldClaimType.as_kind`

[Source](src/cruxible_client/authoring/world.py)

```text
as_kind: KindNamespace
```

Reach the Subject kind this dotted name also names.

A ClaimType wins attribute access over a Subject kind of the same dotted
name, which would otherwise leave `define()` and `subject_ids`
unreachable. This is that escape.

<a id="api-worldclaimtype-call"></a>

### `WorldClaimType.__call__`

[Source](src/cruxible_client/authoring/world.py)

```text
__call__(value: object) -> LiteralValue
```

Mint one literal object for this predicate, admitted before the wire.

<a id="api-worldclaimtype-getitem"></a>

### `WorldClaimType.__getitem__`

[Source](src/cruxible_client/authoring/world.py)

```text
__getitem__(subject_id: str) -> WorldSubject
```

Read a Subject when this dotted name is also an accepted kind.

<a id="api-kindnamespace"></a>

## `KindNamespace`

Import: `cruxible_client.authoring.world.KindNamespace`. [Source](src/cruxible_client/authoring/world.py)

<a id="api-kindnamespace-subject-kind"></a>

### `KindNamespace.subject_kind`

[Source](src/cruxible_client/authoring/world.py)

```text
subject_kind: str | None
```

Return this namespace's Subject kind, or None if it is only a prefix.

<a id="api-kindnamespace-subject-ids"></a>

### `KindNamespace.subject_ids`

[Source](src/cruxible_client/authoring/world.py)

```text
subject_ids: tuple[str, ...]
```

Return every accepted Subject ID of this kind, loading them on first ask.

<a id="api-kindnamespace-define"></a>

### `KindNamespace.define`

[Source](src/cruxible_client/authoring/world.py)

```text
define(subject_id: str) -> SubjectDraft
```

Draft one new Subject of this kind for a changeset to define.

<a id="api-kindnamespace-where"></a>

### `KindNamespace.where` and `KindNamespace.select`

[Source](src/cruxible_client/authoring/world.py)

```text
where(**filters: object) -> CompactQuery
select(*fields: str) -> CompactQuery
```

Start a compact query over this kind at the World's coordinate, for example
`w.dev.roadmap_item.where(adoption_state="adopted", implementation_state__ne="completed").select("task_title")`.
A keyword is a predicate's short or full name, or `subject_id`; the suffixes
`__ne`, `__lt`, `__lte`, `__gt`, `__gte`, `__in`, `__exists` and `__contains`
pick the operator. A leaf that is `self`, a Python keyword, contains `__` or
ends in `_` is spelled with one trailing underscore before any suffix
(`self_`, `class___ne`, `status__ne_`). Names, operators and enum members are
checked against the World before the wire and raise `QueryNameError` with the
nearest names.
`run()` returns a `QueryResult`; iterating the query walks every page. The
generated stub types `where(...)` per kind, with enum members as `Literal`s, so
a type checker rejects a wrong predicate or member.

<a id="api-kindnamespace-getitem"></a>

### `KindNamespace.__getitem__`

[Source](src/cruxible_client/authoring/world.py)

```text
__getitem__(subject_id: str) -> WorldSubject
```

<a id="api-kindnamespace-iter"></a>

### `KindNamespace.__iter__`

[Source](src/cruxible_client/authoring/world.py)

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

Import: `cruxible_client.authoring.sdk_types.TypedRef`. [Source](src/cruxible_client/authoring/sdk_types.py)

<a id="api-typedref-kind"></a>

### `TypedRef.kind`

[Source](src/cruxible_client/authoring/sdk_types.py)

```text
kind: RefKind
```

<a id="api-typedref-address"></a>

### `TypedRef.address`

[Source](src/cruxible_client/authoring/sdk_types.py)

```text
address: str
```

<a id="api-typedref-coordinate"></a>

### `TypedRef.coordinate`

[Source](src/cruxible_client/authoring/sdk_types.py)

```text
coordinate: AcceptedCoordinate
```

<a id="api-subjectref"></a>

## `SubjectRef`

Import: `cruxible_client.authoring.sdk_types.SubjectRef`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Field | Type | Default / construction |
|---|---|---|
| `address` | `str` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `kind` | `ClassVar[RefKind]` | `RefKind.SUBJECT` |

<a id="api-claimtyperef"></a>

## `ClaimTypeRef`

Import: `cruxible_client.authoring.sdk_types.ClaimTypeRef`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Field | Type | Default / construction |
|---|---|---|
| `address` | `str` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `kind` | `ClassVar[RefKind]` | `RefKind.CLAIM_TYPE` |

<a id="api-claimref"></a>

## `ClaimRef`

Import: `cruxible_client.authoring.sdk_types.ClaimRef`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Field | Type | Default / construction |
|---|---|---|
| `address` | `str` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `kind` | `ClassVar[RefKind]` | `RefKind.CLAIM` |

<a id="api-procedureref"></a>

## `ProcedureRef`

Import: `cruxible_client.authoring.sdk_types.ProcedureRef`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Field | Type | Default / construction |
|---|---|---|
| `address` | `str` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `kind` | `ClassVar[RefKind]` | `RefKind.PROCEDURE` |

<a id="api-queryref"></a>

## `QueryRef`

Import: `cruxible_client.authoring.sdk_types.QueryRef`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Field | Type | Default / construction |
|---|---|---|
| `address` | `str` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `kind` | `ClassVar[RefKind]` | `RefKind.QUERY` |

<a id="api-sourceref"></a>

## `SourceRef`

Import: `cruxible_client.authoring.sdk_types.SourceRef`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Field | Type | Default / construction |
|---|---|---|
| `address` | `str` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `kind` | `ClassVar[RefKind]` | `RefKind.SOURCE` |

<a id="api-slotref"></a>

## `ProcedureSlotRef`

One input slot of a Procedure, as `Procedure.bind` names it. A Subject's field is
`cruxible_client.contracts.write.SlotRef` (also `cruxible_client.SlotRef`).

Import: `cruxible_client.authoring.sdk_types.ProcedureSlotRef`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Field | Type | Default / construction |
|---|---|---|
| `address` | `str` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `kind` | `ClassVar[RefKind]` | `RefKind.SLOT` |

<a id="api-pendingsubjectref"></a>

## `PendingSubjectRef`

Import: `cruxible_client.authoring.sdk_types.PendingSubjectRef`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Field | Type | Default / construction |
|---|---|---|
| `address` | `str` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `kind` | `ClassVar[RefKind]` | `RefKind.SUBJECT` |

<a id="api-pendingclaimtyperef"></a>

## `PendingClaimTypeRef`

Import: `cruxible_client.authoring.sdk_types.PendingClaimTypeRef`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Field | Type | Default / construction |
|---|---|---|
| `address` | `str` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `kind` | `ClassVar[RefKind]` | `RefKind.CLAIM_TYPE` |
| `object_kind` | `str` | `Required` |

<a id="api-captureref"></a>

## `CaptureRef`

Import: `cruxible_client.authoring.sdk_types.CaptureRef`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Field | Type | Default / construction |
|---|---|---|
| `capture_digest` | `str` | `Required` |
| `contract_address` | `str` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |
| `citation_role` | `Literal['evidence', 'copy', 'legacy']` | `Required` |

<a id="api-captureview"></a>

## `CaptureView`

Import: `cruxible_client.authoring.sdk_types.CaptureView`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Field | Type | Default / construction |
|---|---|---|
| `result` | `CaptureRead` | `Required` |

<a id="api-captureview-ref"></a>

### `CaptureView.ref`

[Source](src/cruxible_client/authoring/sdk_types.py)

```text
ref: CaptureRef
```

<a id="api-captureview-content"></a>

### `CaptureView.content`

[Source](src/cruxible_client/authoring/sdk_types.py)

```text
content: bytes
```

<a id="api-captureview-text"></a>

### `CaptureView.text`

[Source](src/cruxible_client/authoring/sdk_types.py)

```text
text(encoding: str='utf-8') -> str
```

<a id="api-captureview-json"></a>

### `CaptureView.json`

[Source](src/cruxible_client/authoring/sdk_types.py)

```text
json() -> object
```

<a id="api-literalvalue"></a>

## `LiteralValue`

Import: `cruxible_client.authoring.sdk_types.LiteralValue`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Field | Type | Default / construction |
|---|---|---|
| `predicate` | `str` | `Required` |
| `value` | `CanonicalValue` | `Required` |
| `coordinate` | `AcceptedCoordinate` | `Required` |

<a id="api-exactcontent"></a>

## `ExactContent`

Import: `cruxible_client.authoring.sdk_types.ExactContent`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Field | Type | Default / construction |
|---|---|---|
| `content` | `bytes` | `Required` |

<a id="api-duration"></a>

## `Duration`

Import: `cruxible_client.authoring.sdk_types.Duration`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Field | Type | Default / construction |
|---|---|---|
| `value` | `int` | `Required` |

<a id="api-duration-days"></a>

### `Duration.days`

[Source](src/cruxible_client/authoring/sdk_types.py)

```text
days(*, count: int) -> Duration
```

<a id="api-duration-hours"></a>

### `Duration.hours`

[Source](src/cruxible_client/authoring/sdk_types.py)

```text
hours(*, count: int) -> Duration
```

<a id="api-duration-microseconds"></a>

### `Duration.microseconds`

[Source](src/cruxible_client/authoring/sdk_types.py)

```text
microseconds(*, count: int) -> Duration
```

<a id="api-duration-model-dump"></a>

### `Duration.model_dump`

[Source](src/cruxible_client/authoring/sdk_types.py)

```text
model_dump() -> dict[str, object]
```

<a id="api-effectiveperiod"></a>

## `EffectivePeriod`

Import: `cruxible_client.authoring.sdk_types.EffectivePeriod`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Field | Type | Default / construction |
|---|---|---|
| `starts_at` | `datetime \| None` | `Required` |
| `ends_at` | `datetime \| None` | `Required` |

<a id="api-accessprofile"></a>

## `AccessProfile`

Import: `cruxible_client.authoring.sdk_types.AccessProfile`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Field | Type | Default / construction |
|---|---|---|
| `profile_id` | `str` | `Required` |
| `permitted_access_classes` | `tuple[str, ...]` | `Required` |
| `disclose_restricted_existence` | `bool` | `Required` |

<a id="api-accessprofile-model-dump"></a>

### `AccessProfile.model_dump`

[Source](src/cruxible_client/authoring/sdk_types.py)

```text
model_dump() -> dict[str, object]
```

<a id="api-refkind"></a>

## `RefKind`

Import: `cruxible_client.authoring.sdk_types.RefKind`. [Source](src/cruxible_client/authoring/sdk_types.py)

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

Import: `cruxible_client.authoring.sdk_types.ClaimRole`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Member | Value |
|---|---|
| `NORMATIVE` | `'normative'` |
| `OBSERVATION` | `'observation'` |
| `ENVIRONMENT_BINDING` | `'environment_binding'` |
| `DERIVATION` | `'derivation'` |

<a id="api-disposition"></a>

## `Disposition`

Import: `cruxible_client.authoring.sdk_types.Disposition`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Member | Value |
|---|---|
| `NOT_TESTED` | `'not_tested'` |
| `SUPPORT` | `'support'` |
| `CONTRADICT` | `'contradict'` |
| `UNSURE` | `'unsure'` |

<a id="api-audience"></a>

## `Audience`

Import: `cruxible_client.authoring.sdk_types.Audience`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Member | Value |
|---|---|
| `AGENT` | `'agent'` |
| `HUMAN` | `'human'` |
| `BOTH` | `'both'` |

<a id="api-activationpolicy"></a>

## `ActivationPolicy`

Import: `cruxible_client.authoring.sdk_types.ActivationPolicy`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Member | Value |
|---|---|
| `DRAIN` | `'drain'` |
| `ABORT` | `'abort'` |
| `SNAPSHOT` | `'snapshot'` |
| `EPOCH_CHECK` | `'epoch-check'` |

<a id="api-claimobjectkind"></a>

## `ClaimObjectKind`

Import: `cruxible_client.authoring.sdk_types.ClaimObjectKind`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Member | Value |
|---|---|
| `LITERAL` | `'literal'` |
| `SUBJECT` | `'subject'` |
| `EXACT_CONTENT` | `'exact_content'` |

<a id="api-cardinality"></a>

## `Cardinality`

Import: `cruxible_client.authoring.sdk_types.Cardinality`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Member | Value |
|---|---|
| `ONE` | `'one'` |
| `MANY` | `'many'` |

<a id="api-referentsensitivity"></a>

## `ReferentSensitivity`

Import: `cruxible_client.authoring.sdk_types.ReferentSensitivity`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Member | Value |
|---|---|
| `IDENTITY` | `'identity'` |
| `SHELL` | `'shell'` |

<a id="api-diagnostic"></a>

## `Diagnostic`

Import: `cruxible_client.authoring.sdk_types.Diagnostic`. [Source](src/cruxible_client/authoring/sdk_types.py)

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

Import: `cruxible_client.authoring.sdk_types.CallSite`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Field | Type | Default / construction |
|---|---|---|
| `logical_file` | `str` | `Required` |
| `line` | `int` | `Required` |
| `column` | `int \| None` | `Required` |
| `expression` | `str \| None` | `Required` |

<a id="api-sourcemapentry"></a>

## `SourceMapEntry`

Import: `cruxible_client.authoring.sdk_types.SourceMapEntry`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Field | Type | Default / construction |
|---|---|---|
| `builder_path` | `str` | `Required` |
| `emitted_paths` | `tuple[str, ...]` | `Required` |
| `call_site` | `CallSite` | `Required` |

<a id="api-derivationspec"></a>

## `DerivationSpec`

Import: `cruxible_client.authoring.sdk_types.DerivationSpec`. [Source](src/cruxible_client/authoring/sdk_types.py)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |

## Source selection

`cx.file(path)` selects only cataloged local sources. FileSelector retains
the observed bytes; `.anchor(text)` requires exactly one occurrence and records
byte offsets. Missing, empty, or ambiguous anchors refuse.
Selectors do not silently reinterpret character indexes as byte indexes.
Selections overlapping a governed projection cannot be claimed as independent
evidence. `.observation()` produces the typed working observation for normal
authoring; it is not itself acceptance or evidence registration.

<a id="api-fileselector"></a>

## `FileSelector`

Import: `cruxible_client.authoring.selectors.FileSelector`. [Source](src/cruxible_client/authoring/selectors.py)

| Field | Type | Default / construction |
|---|---|---|
| `path` | `Path` | `Required` |
| `source_id` | `str` | `Required` |
| `content` | `bytes` | `Required` |

<a id="api-fileselector-anchor"></a>

### `FileSelector.anchor`

[Source](src/cruxible_client/authoring/selectors.py)

```text
anchor(text: str) -> EvidenceSelection
```

<a id="api-evidenceselection"></a>

## `EvidenceSelection`

Import: `cruxible_client.authoring.selectors.EvidenceSelection`. [Source](src/cruxible_client/authoring/selectors.py)

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

[Source](src/cruxible_client/authoring/selectors.py)

```text
observation() -> WorkingSelectionObservation
```

<a id="api-workspacesources"></a>

## `WorkspaceSources`

Import: `cruxible_client.authoring.selectors.WorkspaceSources`. [Source](src/cruxible_client/authoring/selectors.py)

<a id="api-workspacesources-document-entries"></a>

### `WorkspaceSources.document_entries`

[Source](src/cruxible_client/authoring/selectors.py)

```text
document_entries: tuple[SourceCatalogEntry, ...]
```

<a id="api-workspacesources-procedure-projection-entries"></a>

### `WorkspaceSources.procedure_projection_entries`

[Source](src/cruxible_client/authoring/selectors.py)

```text
procedure_projection_entries: tuple[ProcedureProjectionCatalogEntry, ...]
```

<a id="api-workspacesources-select"></a>

### `WorkspaceSources.select`

[Source](src/cruxible_client/authoring/selectors.py)

```text
select(requested: str | Path) -> FileSelector
```

<a id="api-workspacesources-path-for-source"></a>

### `WorkspaceSources.path_for_source`

[Source](src/cruxible_client/authoring/selectors.py)

```text
path_for_source(source_id: str) -> Path
```

<a id="api-workspacesources-path-for-procedure"></a>

### `WorkspaceSources.path_for_procedure`

[Source](src/cruxible_client/authoring/selectors.py)

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
| SettleChangeSet | Settle candidate templates under the one covering settle mandate, end path | Served through Lines; falls back as that mandate declares |
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
| Accepted Line | Adds authorized EmitCapture, ProposeChangeSet and SettleChangeSet paths |
| Not served by this SDK authoring API | PostInbox |

Bounded Repeat is available in shared ProcedureInput but has no Sequence step
class. Current Sequence has no general value-merge, nested invoke, recursion, or
parallel execution. Python source compiles through `@procedure` /
`ProcedureBlueprint` in `cruxible_client.authoring.source`, not through Sequence. StateTap binds at admission;
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

Import: `cruxible_client.authoring.procedures.Sequence`. [Source](src/cruxible_client/authoring/procedures.py)

| Field | Type | Default / construction |
|---|---|---|
| `steps` | `tuple[Step, ...] \| list[Step]` | `Required` |
| `name` | `str` | `Required` |
| `contract_in` | `Contract` | `Required` |
| `contract_out` | `Contract` | `Required` |
| `budget` | `ProcedureBudget` | `Required` |
| `hard_caps` | `ProcedureHardCaps` | `Required` |
| `returns` | `str \| None` | `None` |
| `activation_policy` | `Literal['drain', 'abort', 'snapshot', 'epoch-check']` | `'snapshot'` |
| `acquisition_policy` | `str \| None` | `None` |
| `description` | `str \| None` | `None` |

<a id="api-sequence-bind"></a>

### `Sequence.bind`

[Source](src/cruxible_client/authoring/procedures.py)

```text
bind(**providers: ProviderBinding) -> Sequence
```

<a id="api-sequence-preview"></a>

### `Sequence.preview`

[Source](src/cruxible_client/authoring/procedures.py)

```text
preview() -> ProcedurePreview
```

<a id="api-sequence-build"></a>

### `Sequence.build`

[Source](src/cruxible_client/authoring/procedures.py)

```text
build() -> ProcedureInput
```

<a id="api-statetap"></a>

## `StateTap`

Import: `cruxible_client.authoring.procedures.StateTap`. [Source](src/cruxible_client/authoring/procedures.py)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |
| `next` | `str \| None` | `None` |
| `node_id` | `str \| None` | `dataclass_field(default=None, kw_only=True)` |
| `query` | `str` | `''` |
| `parameters` | `object` | `None` |

<a id="api-source"></a>

## `Source`

Import: `cruxible_client.authoring.procedures.Source`. [Source](src/cruxible_client/authoring/procedures.py)

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

Import: `cruxible_client.authoring.procedures.Call`. [Source](src/cruxible_client/authoring/procedures.py)

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

Import: `cruxible_client.authoring.procedures.Transform`. [Source](src/cruxible_client/authoring/procedures.py)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |
| `next` | `str \| None` | `None` |
| `node_id` | `str \| None` | `dataclass_field(default=None, kw_only=True)` |
| `transform_kind` | `TransformKind` | `Required` |
| `contract_in` | `Contract` | `Required` |
| `contract_out` | `Contract` | `Required` |
| `spec` | `ProcedureTransformSpec` | `Required` |

<a id="api-project"></a>

## `Project`

Import: `cruxible_client.authoring.procedures.Project`. [Source](src/cruxible_client/authoring/procedures.py)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |
| `next` | `str \| None` | `None` |
| `node_id` | `str \| None` | `dataclass_field(default=None, kw_only=True)` |
| `fields` | `object` | `Required` |
| `contract_out` | `Contract` | `Required` |

<a id="api-guard"></a>

## `Guard`

Import: `cruxible_client.authoring.procedures.Guard`. [Source](src/cruxible_client/authoring/procedures.py)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |
| `next` | `str \| None` | `None` |
| `node_id` | `str \| None` | `dataclass_field(default=None, kw_only=True)` |
| `predicate` | `GuardPredicate` | `Required` |
| `on_true` | `str \| None` | `None` |
| `on_false` | `str` | `'$abort'` |
| `refusal_code` | `str` | `'guard_refused'` |
| `message` | `str` | `'Procedure guard refused.'` |

<a id="api-emitcapture"></a>

## `EmitCapture`

Import: `cruxible_client.authoring.procedures.EmitCapture`. [Source](src/cruxible_client/authoring/procedures.py)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |
| `next` | `str \| None` | `None` |
| `node_id` | `str \| None` | `dataclass_field(default=None, kw_only=True)` |
| `capture_contract` | `str` | `''` |
| `input` | `object` | `Previous()` |

<a id="api-proposechangeset"></a>

## `ProposeChangeSet`

Import: `cruxible_client.authoring.procedures.ProposeChangeSet`. [Source](src/cruxible_client/authoring/procedures.py)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |
| `next` | `str \| None` | `None` |
| `node_id` | `str \| None` | `dataclass_field(default=None, kw_only=True)` |
| `candidate_templates` | `tuple[object, ...]` | `Required` |

<a id="api-settlechangeset"></a>

## `SettleChangeSet`

Import: `cruxible_client.authoring.procedures.SettleChangeSet`. [Source](src/cruxible_client/authoring/procedures.py)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |
| `next` | `str \| None` | `None` |
| `node_id` | `str \| None` | `dataclass_field(default=None, kw_only=True)` |
| `candidate_templates` | `tuple[object, ...]` | `Required` |

<a id="api-halt"></a>

## `Halt`

Import: `cruxible_client.authoring.procedures.Halt`. [Source](src/cruxible_client/authoring/procedures.py)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |
| `next` | `str \| None` | `None` |
| `node_id` | `str \| None` | `dataclass_field(default=None, kw_only=True)` |
| `reason` | `str \| None` | `None` |

<a id="api-output"></a>

## `Output`

Import: `cruxible_client.authoring.procedures.Output`. [Source](src/cruxible_client/authoring/procedures.py)

| Field | Type | Default / construction |
|---|---|---|
| `step` | `str` | `Required` |
| `path` | `str` | `''` |

<a id="api-previous"></a>

## `Previous`

Import: `cruxible_client.authoring.procedures.Previous`. [Source](src/cruxible_client/authoring/procedures.py)

| Field | Type | Default / construction |
|---|---|---|
| `path` | `str` | `''` |

<a id="api-providerbinding"></a>

## `ProviderBinding`

Import: `cruxible_client.authoring.procedures.ProviderBinding`. [Source](src/cruxible_client/authoring/procedures.py)

| Field | Type | Default / construction |
|---|---|---|
| `provider` | `str` | `Required` |
| `interface` | `str` | `Required` |
| `interface_digest` | `str` | `Required` |
| `implementation_digest` | `str` | `Required` |
| `effect_class` | `Literal['none', 'external_read', 'external_mutation'] \| None` | `None` |

<a id="api-providerbinding-from-interface"></a>

### `ProviderBinding.from_interface`

[Source](src/cruxible_client/authoring/procedures.py)

```text
from_interface(entry: ProviderInterfaceEntry, *, provider: str | None=None) -> ProviderBinding
```

<a id="api-procedurepreview"></a>

## `ProcedurePreview`

Import: `cruxible_client.authoring.procedures.ProcedurePreview`. [Source](src/cruxible_client/authoring/procedures.py)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |
| `ready_for_prepare` | `bool` | `Required` |
| `contracts` | `tuple[CarriedContractInput, ...]` | `Required` |
| `contract_in` | `dict[str, Any]` | `Required` |
| `contract_out` | `dict[str, Any]` | `Required` |
| `authority` | `Literal["observe", "propose", "settle"]` | `Required` |
| `acquisition_policy` | `str \| None` | `Required` |
| `nodes` | `tuple[dict[str, Any], ...]` | `Required` |
| `edges` | `dict[str, dict[str, str]]` | `Field(default_factory=dict)` |
| `providers` | `dict[str, ProviderBinding]` | `Field(default_factory=dict)` |
| `terminals` | `tuple[str, ...]` | `()` |
| `returns` | `str \| None` | `Required` |
| `budget` | `ProcedureBudget` | `Required` |
| `hard_caps` | `ProcedureHardCaps` | `Required` |
| `errors` | `tuple[CompositionDiagnostic, ...]` | `()` |
| `pending_checks` | `tuple[str, ...]` | `('Resolve accepted references at the intent base and verify provider interfaces.', 'Validate runtime values against contracts; preview does not execute any path.', 'Check authority, installation availability and effective policy budgets at admission.')` |

<a id="api-compositiondiagnostic"></a>

## `CompositionDiagnostic`

Import: `cruxible_client.authoring.procedures.CompositionDiagnostic`. [Source](src/cruxible_client/authoring/procedures.py)

| Field | Type | Default / construction |
|---|---|---|
| `step` | `str \| None` | `None` |
| `code` | `str` | `Required` |
| `message` | `str` | `Required` |

<a id="api-procedure"></a>

## `Procedure`

Import: `cruxible_client.authoring.sdk.Procedure`. [Source](src/cruxible_client/authoring/sdk.py)

<a id="api-procedure-ref"></a>

### `Procedure.ref`

[Source](src/cruxible_client/authoring/sdk.py)

```text
ref: ProcedureRef
```

<a id="api-procedure-readiness"></a>

### `Procedure.readiness`

[Source](src/cruxible_client/authoring/sdk.py)

```text
readiness() -> api.ProcedureReadiness
```

<a id="api-procedure-bind"></a>

### `Procedure.bind`

[Source](src/cruxible_client/authoring/sdk.py)

```text
bind(*, bindings: Mapping[str | ProcedureSlotRef, TypedRef]) -> api.ProcedureBindResult
```

<a id="api-procedure-run"></a>

### Typed invocation and child inspection

`Procedure.input(**fields)` constructs an immutable record from the exact accepted
input contract. Pass it as `Procedure.run(input=record)`; successful
`ProcedureRun.result` follows the declared output contract. `ProcedureRun.succeeded`
guards result access, and `.children` returns authorized child run handles.
`cx.query_binding(name)` provides a typed `.parameters(**fields)` constructor;
`cx.run_query(binding, parameters=record)` uses the existing query service and
returns typed result and receipt models. See the [v2 reference](../../docs/sdk-v2-reference.md)
for the exhaustive source syntax, arguments, validation, and execution boundaries.

### `Procedure.run`

[Source](src/cruxible_client/authoring/sdk.py)

```text
run(
    *,
    at: AcceptedCoordinate | None = None,
    resolution_contract: ResolutionContractReference | None = None,
    trigger_event: TriggerEventReference | None = None,
    input: Record,
) -> ProcedureRun
```

<a id="api-procedure-measure"></a>

### `Procedure.measure`

[Source](src/cruxible_client/authoring/sdk.py)

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

[Source](src/cruxible_client/authoring/sdk.py)

```text
readings(
    *,
    run: ProcedureRun | str | None = None,
    measurements: Sequence[str] = (),
    limit: int = 50,
    cursor: str | None = None,
    at: AcceptedCoordinate | None = None,
) -> api.ProcedureReadingsResult
```

Inspect measurement standing and retained readings. Never writes.

A page's `cursor` continues that page's selection: the observation
instant and coordinate the first page was answered at travel inside
it, so passing the cursor back with the same `run`/`measurements`
pages the same selection even though this call stamps a fresh clock.

<a id="api-procedurerun"></a>

## `ProcedureRun`

Import: `cruxible_client.authoring.sdk.ProcedureRun`. [Source](src/cruxible_client/authoring/sdk.py)

<a id="api-procedurerun-run-id"></a>

### `ProcedureRun.run_id`

[Source](src/cruxible_client/authoring/sdk.py)

```text
run_id: str | None
```

<a id="api-procedurerun-status"></a>

### `ProcedureRun.status`

[Source](src/cruxible_client/authoring/sdk.py)

```text
status: str
```

<a id="api-procedurerun-result"></a>

### `ProcedureRun.result`

[Source](src/cruxible_client/authoring/sdk.py)

```text
result: Record
```

<a id="api-procedurerun-receipt"></a>

### `ProcedureRun.receipt`

[Source](src/cruxible_client/authoring/sdk.py)

```text
receipt: str | None
```

<a id="api-procedurerun-coordinate"></a>

### `ProcedureRun.coordinate`

[Source](src/cruxible_client/authoring/sdk.py)

```text
coordinate: AcceptedCoordinate
```

<a id="api-procedurerun-track-record"></a>

### `ProcedureRun.track_record`

[Source](src/cruxible_client/authoring/sdk.py)

```text
track_record: tuple[GetProcedureTrackRecord, ...]
```

<a id="api-procedurerun-refresh"></a>

### `ProcedureRun.refresh`

[Source](src/cruxible_client/authoring/sdk.py)

```text
refresh() -> ProcedureRun
```

<a id="api-procedurerun-measure"></a>

### `ProcedureRun.measure`

[Source](src/cruxible_client/authoring/sdk.py)

```text
measure(*, measurements: Sequence[str]=()) -> MeasurementBatch
```

Credit this run's exact grain with every due measurement's standing answer.

## Projections and workspace

Access ProjectionBlocks through `cx.block`. Repin reads accepted backing
state and updates local declarations/manifests; it replaces prose only when
explicit body bytes are supplied. For claims/queries/artifacts, None preserves
the backing class and an empty sequence removes it. Query-only and artifact
backings are valid. `currency_policy` distinguishes `warn` from `require_current`.
Compact markers are the default; their local manifests are part of the view.

Sync checks declared block backings and reports drift. It does not regenerate
the author’s prose or silently repin. It never raises on drift: read
`has_refusals` (a blocking finding under the configured currency policy) and
`would_change`. `detach` is an explicit local mutation, and `check=True` reports
what it would change without making it. `repin(..., dry_run=True)` returns the
stamp it would write and writes nothing. The marker grammar is in the CLI
reference under "Projection block markers"; MCP agents use the
`cruxible_block_repin`, `cruxible_block_sync` (read-only) and
`cruxible_block_detach` (the page edit, previewed) tools, which
run this same adapter.

<a id="api-projectionblocks"></a>

## `ProjectionBlocks`

Import: `cruxible_client.authoring.sdk.ProjectionBlocks`. [Source](src/cruxible_client/authoring/sdk.py)

<a id="api-projectionblocks-repin"></a>

### `ProjectionBlocks.repin`

[Source](src/cruxible_client/authoring/sdk.py)

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
) -> ProjectionBlockStamp
```

Refresh backing pins and optionally replace this block's authored body.

Compact markers are the default: digest references with local manifests. Subsequent
repins preserve that format.

<a id="api-projectionblocks-sync"></a>

### `ProjectionBlocks.sync`

[Source](src/cruxible_client/authoring/sdk.py)

```text
sync(
    *paths: str | Path,
    all: bool = False,
    check: bool = False,
    detach: Sequence[str | Path] = (),
) -> api.BlockSyncResult
```

Check every block; policy controls whether drift fails the check.

## Signing capabilities

ApprovalSigner and ClaimAttestationSigner are separate protocols.
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

Import: `cruxible_client.authoring.signing.ApprovalSigner`. [Source](src/cruxible_client/authoring/signing.py)

<a id="api-approvalsigner-signer-id"></a>

### `ApprovalSigner.signer_id`

[Source](src/cruxible_client/authoring/signing.py)

```text
signer_id: str
```

<a id="api-approvalsigner-public-key"></a>

### `ApprovalSigner.public_key`

[Source](src/cruxible_client/authoring/signing.py)

```text
public_key: str
```

<a id="api-approvalsigner-sign"></a>

### `ApprovalSigner.sign`

[Source](src/cruxible_client/authoring/signing.py)

```text
sign(statement: ApprovalStatement) -> ApprovalAttestation
```

<a id="api-localed25519approvalsigner"></a>

## `LocalEd25519ApprovalSigner`

Import: `cruxible_client.authoring.signing.LocalEd25519ApprovalSigner`. [Source](src/cruxible_client/authoring/signing.py)

| Field | Type | Default / construction |
|---|---|---|
| `signer_id` | `str` | `Required` |
| `private_key_path` | `Path` | `Required` |
| `public_key` | `str` | `Required` |

<a id="api-localed25519approvalsigner-open"></a>

### `LocalEd25519ApprovalSigner.open`

[Source](src/cruxible_client/authoring/signing.py)

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

[Source](src/cruxible_client/authoring/signing.py)

```text
sign(statement: ApprovalStatement) -> ApprovalAttestation
```

<a id="api-claimattestationv2signer"></a>

## `ClaimAttestationSigner`

Import: `cruxible_client.authoring.attestations.ClaimAttestationSigner`. [Source](src/cruxible_client/authoring/attestations.py)

<a id="api-claimattestationv2signer-signer"></a>

### `ClaimAttestationSigner.signer`

[Source](src/cruxible_client/authoring/attestations.py)

```text
signer: str
```

<a id="api-claimattestationv2signer-signing-key-id"></a>

### `ClaimAttestationSigner.signing_key_id`

[Source](src/cruxible_client/authoring/attestations.py)

```text
signing_key_id: str
```

<a id="api-claimattestationv2signer-sign-claim-attestation-v2"></a>

### `ClaimAttestationSigner.sign_claim_attestation_v2`

[Source](src/cruxible_client/authoring/attestations.py)

```text
sign_claim_attestation_v2(statement: ClaimAttestationStatement) -> ClaimAttestation
```

<a id="api-localed25519claimattestationsigner"></a>

## `LocalEd25519ClaimAttestationSigner`

Import: `cruxible_client.authoring.attestations.LocalEd25519ClaimAttestationSigner`. [Source](src/cruxible_client/authoring/attestations.py)

| Field | Type | Default / construction |
|---|---|---|
| `signer` | `str` | `Required` |
| `signing_key_id` | `str` | `Required` |
| `private_key_path` | `Path` | `Required` |
| `public_key` | `str` | `Required` |

<a id="api-localed25519claimattestationsigner-open"></a>

### `LocalEd25519ClaimAttestationSigner.open`

[Source](src/cruxible_client/authoring/attestations.py)

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

[Source](src/cruxible_client/authoring/attestations.py)

```text
sign_claim_attestation_v2(statement: ClaimAttestationStatement) -> ClaimAttestation
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

Import: `cruxible_client.ArtifactIdentity`. [Source](src/cruxible_client/contracts/artifacts.py)

Kind and name must already be NFC-normalized and canonical. `qualified` returns `kind:name`; it performs no lookup.

| Field | Type | Default / validation |
|---|---|---|
| `kind` | `str` | `Required` |
| `name` | `str` | `Required` |

<a id="api-artifactlifecycle"></a>

### `ArtifactLifecycle`

Import: `cruxible_client.ArtifactLifecycle`. [Source](src/cruxible_client/contracts/artifacts.py)

An optional predecessor is a tagged artifact digest. Lifecycle metadata does not itself retire an artifact; it is admitted with the governed definition.

| Field | Type | Default / validation |
|---|---|---|
| `state` | `Literal['live', 'retired']` | `'live'` |
| `predecessor_digest` | `str \| None` | `None` |

<a id="api-artifactpin"></a>

### `ArtifactPin`

Import: `cruxible_client.ArtifactPin`. [Source](src/cruxible_client/contracts/artifacts.py)

`role` must be canonical. `artifact_digest` is a tagged artifact digest; the identity alone does not pin a version.

| Field | Type | Default / validation |
|---|---|---|
| `role` | `str` | `Required` |
| `target` | `ArtifactIdentity` | `Required` |
| `artifact_digest` | `str` | `Required` |

<a id="api-claimadmissionpolicyv1"></a>

### `ClaimAdmissionPolicy`

Import: `cruxible_client.ClaimAdmissionPolicy`. [Source](src/cruxible_client/contracts/policies.py)

Requirement groups must be sorted and unique by requirement ID, with IDs unique across kinds. Unknown freeze transition exceptions are refused.

| Field | Type | Default / validation |
|---|---|---|
| `tag` | `Literal['playbill-claim-admission-policy-v1']` | `'playbill-claim-admission-policy-v1'` |
| `corroboration_requirements` | `tuple[CorroborationRequirement, ...]` | `()` |
| `freeze_requirements` | `tuple[FreezeRequirement, ...]` | `()` |

<a id="api-claimresolutionpolicyv1"></a>

### `ClaimResolutionPolicy`

Import: `cruxible_client.ClaimResolutionPolicy`. [Source](src/cruxible_client/contracts/policies.py)

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

### `CanonicalDuration`

Import: `cruxible_client.CanonicalDuration`. [Source](src/cruxible_client/contracts/captures.py)

Duration is represented in integer microseconds, not floating-point seconds. Procedure budgets/caps additionally require their wall-clock duration to be nonzero.

| Field | Type | Default / validation |
|---|---|---|
| `tag` | `Literal['playbill-duration-v1']` | `'playbill-duration-v1'` |
| `microseconds` | `int` | `Field(ge=0)` |

<a id="api-contractschema"></a>

### `ContractSchema`

Import: `cruxible_client.ContractSchema`. [Source](src/cruxible_client/contracts/procedures/contract_schema.py)

Each field must explicitly supply its type, even though `PropertySchema` alone has a type default. Extra fields are disallowed by default.

| Field | Type | Default / validation |
|---|---|---|
| `description` | `str \| None` | `None` |
| `fields` | `dict[str, PropertySchema]` | `Required` |
| `allow_extra` | `bool` | `False` |

<a id="api-procedurebudgetv3"></a>

### `ProcedureBudget`

Import: `cruxible_client.ProcedureBudget`. [Source](src/cruxible_client/contracts/procedures/models.py)

Wall-clock budget must be nonzero. Omitted optional result/item limits do not disable effective daemon policy. Bounds in the field declarations remain enforced.

| Field | Type | Default / validation |
|---|---|---|
| `tag` | `Literal['playbill-procedure-budget-v1']` | `'playbill-procedure-budget-v1'` |
| `wall_clock` | `CanonicalDuration` | `Required` |
| `max_provider_calls` | `int` | `Field(ge=0, le=1000000)` |
| `max_capture_bytes` | `int` | `Field(ge=0, le=2 ** 63 - 1)` |
| `max_result_bytes` | `int \| None` | `Field(default=None, ge=1, exclude_if=lambda v: v is None)` |
| `max_items` | `int \| None` | `Field(default=None, ge=1, le=2 ** 31 - 1, exclude_if=lambda value: value is None)` |

<a id="api-proceduredefinitionv3"></a>

### `ProcedureDefinitionV3`

Import: `cruxible_client.ProcedureDefinitionV3`. [Source](src/cruxible_client/contracts/procedures/models.py)

This root export is the frozen graph-v3 model, not an alias for the latest graph version. The current Sequence builder emits graph v5. Use the corresponding versioned contract when authoring a raw definition.

| Field | Type | Default / validation |
|---|---|---|
| `graph_format` | `Literal[3]` | `3` |
| `name` | `str` | `Required` |
| `description` | `str \| None` | `None` |
| `contract_in` | `ProcedurePinBinding` | `Required` |
| `contract_out` | `ProcedurePinBinding` | `Required` |
| `parameter_contract` | `ProcedurePinBinding \| None` | `None` |
| `nodes` | `tuple[ProcedureNodeV3, ...]` | `Required` |
| `returns` | `str` | `Required` |
| `pin_slots` | `tuple[ProcedurePinSlot, ...]` | `()` |
| `measurements` | `tuple[ProcedureMeasurementDeclaration, ...]` | `()` |
| `budget` | `ProcedureBudget` | `Required` |
| `hard_caps` | `ProcedureHardCaps` | `Required` |
| `terminal_capability` | `Literal[1, 2, 3]` | `Required`; authoring derives it from the terminals and invoked children |
| `annotations` | `object` | `Field(default_factory=dict)` |

<a id="api-procedurehardcapsv3"></a>

### `ProcedureHardCaps`

Import: `cruxible_client.ProcedureHardCaps`. [Source](src/cruxible_client/contracts/procedures/models.py)

Maximum wall-clock duration must be nonzero. Caps constrain declared/effective execution; their values do not grant execution authority.

| Field | Type | Default / validation |
|---|---|---|
| `tag` | `Literal['playbill-procedure-hard-caps-v1']` | `'playbill-procedure-hard-caps-v1'` |
| `max_wall_clock` | `CanonicalDuration` | `Required` |
| `max_provider_calls` | `int` | `Field(ge=0, le=1000000)` |
| `max_capture_bytes` | `int` | `Field(ge=0, le=2 ** 63 - 1)` |
| `max_result_bytes` | `int \| None` | `Field(default=None, ge=1, exclude_if=lambda v: v is None)` |
| `max_items` | `int` | `Field(ge=1, le=2 ** 31 - 1)` |
| `max_repeat_attempts` | `int` | `Field(ge=1, le=2 ** 31 - 1)` |

<a id="api-procedureownedcontractv1"></a>

### `ProcedureOwnedContract`

Import: `cruxible_client.ProcedureOwnedContract`. [Source](src/cruxible_client/contracts/procedures/artifacts.py)

An owner-carried input/output contract is part of the governed Procedure envelope; constructing it does not publish a standalone global contract.

| Field | Type | Default / validation |
|---|---|---|
| `tag` | `Literal['playbill-procedure-owned-contract-v1']` | `'playbill-procedure-owned-contract-v1'` |
| `identity` | `ArtifactIdentity` | `Required` |
| `contract_schema` | `ContractSchema` | `Field(alias='schema')` |

<a id="api-procedurepinslotrefv1"></a>

### `ProcedurePinSlotRef`

Import: `cruxible_client.ProcedurePinSlotRef`. [Source](src/cruxible_client/contracts/procedures/models.py)

References an explicitly declared Procedure slot. It is a definition-time placeholder, not an arbitrary runtime dispatch key.

| Field | Type | Default / validation |
|---|---|---|
| `tag` | `Literal['playbill-procedure-pin-slot-ref-v1']` | `'playbill-procedure-pin-slot-ref-v1'` |
| `slot_name` | `str` | `Required` |

<a id="api-procedurepinslotv1"></a>

### `ProcedurePinSlot`

Import: `cruxible_client.ProcedurePinSlot`. [Source](src/cruxible_client/contracts/procedures/models.py)

Declares a slot under the frozen Procedure contract. `Procedure.bind(...)` uses the corresponding accepted binding rules.

| Field | Type | Default / validation |
|---|---|---|
| `tag` | `Literal['playbill-procedure-pin-slot-v1']` | `'playbill-procedure-pin-slot-v1'` |
| `slot_name` | `str` | `Required` |
| `pin_role` | `str` | `Required` |
| `artifact_kind` | `str` | `Required` |
| `interface_digest` | `str` | `Required` |

<a id="api-projectnodev3"></a>

### `ProjectNode`

Import: `cruxible_client.ProjectNode`. [Source](src/cruxible_client/contracts/procedures/models.py)

Pure projection under the declared output contract. It does not invoke a provider or mutate accepted state.

| Field | Type | Default / validation |
|---|---|---|
| `kind` | `Literal['project']` | `'project'` |
| `node_id` | `str` | `Required` |
| `fields` | `object` | `Required` |
| `contract_out` | `ProcedurePinBinding` | `Required` |
| `as_` | `str` | `Field(alias='as')` |
| `next` | `str \| None` | `None` |

<a id="api-propertyschema"></a>

### `PropertySchema`

Import: `cruxible_client.PropertySchema`. [Source](src/cruxible_client/contracts/procedures/contract_schema.py)

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

Import: `cruxible_client.StateTapNodeV3`. [Source](src/cruxible_client/contracts/procedures/models.py)

An admitted query selection. Its parameters cannot silently depend on a later provider output.

| Field | Type | Default / validation |
|---|---|---|
| `kind` | `Literal['state_tap']` | `'state_tap'` |
| `node_id` | `str` | `Required` |
| `query` | `ProcedurePinBinding` | `Required` |
| `parameters` | `object` | `Field(default_factory=dict)` |
| `as_` | `str` | `Field(alias='as')` |
| `next` | `str \| None` | `None` |

<a id="api-transformnodev3"></a>

### `TransformNode`

Import: `cruxible_client.TransformNode`. [Source](src/cruxible_client/contracts/procedures/models.py)

A contracted built-in transformation, restricted to the transform kind/specification in the versioned graph contract.

| Field | Type | Default / validation |
|---|---|---|
| `kind` | `Literal['transform']` | `'transform'` |
| `node_id` | `str` | `Required` |
| `transform_kind` | `TransformKind` | `Required` |
| `contract_in` | `ProcedurePinBinding` | `Required` |
| `contract_out` | `ProcedurePinBinding` | `Required` |
| `spec` | `ProcedureTransformSpec` | `Required` |
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

Import: `cruxible_client.contracts.authoring.inputs.LiteralObjectInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['literal']` | `Required` |
| `value` | `object` | `Required` |

<a id="api-subjectobjectinput"></a>

## `SubjectObjectInput`

Import: `cruxible_client.contracts.authoring.inputs.SubjectObjectInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['subject']` | `Required` |
| `subject` | `str` | `Required` |

<a id="api-exactcontentobjectinput"></a>

## `ExactContentObjectInput`

Import: `cruxible_client.contracts.authoring.inputs.ExactContentObjectInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['exact_content']` | `Required` |
| `text` | `str \| None` | `None` |
| `content_base64` | `str \| None` | `None` |

<a id="api-selfsourceinput"></a>

## `SelfSourceInput`

Import: `cruxible_client.contracts.authoring.inputs.SelfSourceInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['self_source']` | `Required` |
| `body` | `str` | `Required` |

<a id="api-workingselectioninput"></a>

## `WorkingSelectionInput`

Import: `cruxible_client.contracts.authoring.inputs.WorkingSelectionInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['working_selection']` | `Required` |
| `source_id` | `str` | `Required` |

<a id="api-existingcaptureinput"></a>

## `ExistingCaptureInput`

Import: `cruxible_client.contracts.authoring.inputs.ExistingCaptureInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['existing_capture']` | `Required` |
| `capture_digest` | `str` | `Required` |

<a id="api-acceptedreferenceinput"></a>

## `AcceptedReferenceInput`

Import: `cruxible_client.contracts.authoring.inputs.AcceptedReferenceInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['accepted']` | `Required` |
| `role` | `str` | `Required` |
| `target` | `str` | `Required` |

<a id="api-slotreferenceinput"></a>

## `SlotReferenceInput`

Import: `cruxible_client.contracts.authoring.inputs.SlotReferenceInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['slot']` | `Required` |
| `slot_name` | `str` | `Required` |

<a id="api-carriedcontractreferenceinput"></a>

## `CarriedContractReferenceInput`

Import: `cruxible_client.contracts.authoring.inputs.CarriedContractReferenceInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['carried_contract']` | `Required` |
| `name` | `str` | `Required` |
| `role` | `str` | `Required` |

<a id="api-carriedcontractinput"></a>

## `CarriedContractInput`

Import: `cruxible_client.contracts.authoring.inputs.CarriedContractInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

| Field | Type | Default / construction |
|---|---|---|
| `name` | `str` | `Required` |
| `description` | `str \| None` | `None` |
| `fields` | `dict[str, PropertySchema]` | `Required` |
| `allow_extra` | `bool` | `False` |

<a id="api-claimdispositioninput"></a>

## `ClaimDispositionInput`

Import: `cruxible_client.contracts.authoring.inputs.ClaimDispositionInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

| Field | Type | Default / construction |
|---|---|---|
| `claim_id` | `str` | `Required` |
| `disposition` | `Literal['not_tested', 'support', 'contradict', 'unsure']` | `Required` |

<a id="api-claiminput"></a>

## `ClaimInput`

Import: `cruxible_client.contracts.authoring.inputs.ClaimInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

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
| `revises` | `str \| None` | `None`; Claim ID this Claim revises; omit to state a new Claim. |
| `dispositions` | `tuple[ClaimDispositionInput, ...]` | `()` |

<a id="api-procedureinput"></a>

## `ProcedureInput`

Import: `cruxible_client.contracts.authoring.inputs.ProcedureInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

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

Import: `cruxible_client.contracts.authoring.inputs.SubjectInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['subject']` | `Required` |
| `subject` | `SubjectShell` | `Required` |

<a id="api-querydefinitioninput"></a>

## `QueryDefinitionInput`

Import: `cruxible_client.contracts.authoring.inputs.QueryDefinitionInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['query_definition']` | `Required` |
| `query_definition` | `QueryDefinitionSpec \| QueryDefinition` | `Required` |

<a id="api-approvalpolicyinput"></a>

## `ApprovalPolicyInput`

Import: `cruxible_client.contracts.authoring.inputs.ApprovalPolicyInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['approval_policy']` | `Required` |
| `approval_policy` | `ApprovalPolicy` | `Required` |

<a id="api-procedureruntimepolicyinput"></a>

## `ProcedureRuntimePolicyInput`

Import: `cruxible_client.contracts.authoring.inputs.ProcedureRuntimePolicyInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['procedure_runtime_policy']` | `Required` |
| `procedure_runtime_policy` | `ProcedureRuntimePolicy` | `Required` |

<a id="api-claimtypeinput"></a>

## `ClaimTypeInput`

Import: `cruxible_client.contracts.authoring.inputs.ClaimTypeInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['claim_type']` | `Required` |
| `claim_type` | `ClaimType` | `Required` |

<a id="api-claimtypesuccessioninput"></a>

## `ClaimTypeSuccessionInput`

Import: `cruxible_client.contracts.authoring.inputs.ClaimTypeSuccessionInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['claim_type_succession']` | `Required` |
| `successor` | `ClaimType` | `Required` |
| `dependents` | `tuple[ClaimTypeSuccessionDependent, ...]` | `()` |

<a id="api-claimretirementinput"></a>

## `ClaimRetirementInput`

Import: `cruxible_client.contracts.authoring.inputs.ClaimRetirementInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['claim_retirement']` | `Required` |
| `claim_id` | `str` | `Required` |
| `reason` | `ClaimRetirementReason` | `Required` |
| `effective_until` | `datetime \| None` | `None` |
| `dependents` | `tuple[ClaimRetireDependent, ...]` | `()` |

<a id="api-proceduremandateinputv1"></a>

## `ProcedureMandateInput`

Import: `cruxible_client.contracts.authoring.inputs.ProcedureMandateInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

| Field | Type | Default / construction |
|---|---|---|
| `tag` | `Literal['playbill-procedure-mandate-input-v1']` | `'playbill-procedure-mandate-input-v1'` |
| `kind` | `Literal['procedure_mandate']` | `Required` |
| `name` | `str` | `Required` |
| `procedure_name` | `str` | `Required` |
| `rung` | `Literal[2, 3]` | `Required` |
| `authority_ceiling` | `ProcedureHardCaps` | `Required` |
| `namespace` | `tuple[str, ...]` | `Required` |
| `valid_from` | `datetime` | `Required` |
| `expires_at` | `datetime` | `Required` |
| `retire` | `bool` | `False` |

<a id="api-acquisitionpolicyinput"></a>

## `AcquisitionPolicyInput`

Import: `cruxible_client.contracts.authoring.inputs.AcquisitionPolicyInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['acquisition_policy']` | `Required` |
| `acquisition_policy` | `SourceAcquisitionPolicy` | `Required` |

<a id="api-lineinput"></a>

## `LineInput`

Import: `cruxible_client.contracts.authoring.inputs.LineInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

One Line: a stable instantiation of an accepted or same-set Procedure. It runs
when run explicitly, or when a Trigger aimed at it fires; author its schedule
with a `TriggerInput`. A Line that proposes or settles needs a live
ProcedureMandate covering its Procedure; an observe-only Line needs none.

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['line']` | `Required` |
| `name` | `str` | `Required` |
| `procedure_name` | `str` | `Required` |
| `acquisition_policy_name` | `str \| None` | `None`; required only when the Procedure has Source nodes |
| `max_authority` | `Literal['observe', 'propose', 'settle'] \| None` | `None` (the Procedure's capability) |
| `trigger_input` | `str \| None` | `None` |
| `parameters` | `dict[str, object]` | `{}`; checked against the Procedure's input contract |
| `budgets` | `dict[str, int] \| None` | `None` (the Procedure's hard caps) |
| `occurrence_epoch` | `int` | `1` |
| `retire` | `bool` | `False` |

<a id="api-triggerinput"></a>

## `TriggerInput`

Import: `cruxible_client.contracts.authoring.inputs.TriggerInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

One Trigger: a schedule aimed at exactly one Line or internal action. A Line
target names an accepted or same-set Line and takes any schedule that supplies
its input; an internal action (`evidence.sweep`, `prediction.anchor_retry`)
takes a cadence or cron schedule only in this version. Cron expressions are
always evaluated in UTC; convert local times first (09:00 New York in winter
is 14:00 UTC). A Trigger is changed or retired (`retire`) through a successor.

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['trigger']` | `Required` |
| `name` | `str` | `Required` |
| `schedule` | `CadenceSchedule \| CronSchedule \| CaptureLandingSchedule \| WindowCloseSchedule` | `Required`; `CronSchedule.expression` is five UTC fields and names no timezone |
| `line_name` | `str \| None` | `None`; the Line this Trigger runs |
| `action` | `InternalActionName \| None` | `None`; a registered internal action |
| `retire` | `bool` | `False` |

<a id="api-changesetinput"></a>

## `ChangeSetInput`

Import: `cruxible_client.contracts.authoring.inputs.ChangeSetInput`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

| Field | Type | Default / construction |
|---|---|---|
| `kind` | `Literal['change_set']` | `Required` |
| `members` | `tuple[AuthoringChangeSetMemberInput, ...]` | `Field(min_length=1)` |
| `rationale` | `str \| None` | `Field(default=None, max_length=CHANGE_SET_RATIONALE_MAX_LENGTH)` |

<a id="api-authoringinputerror"></a>

## `AuthoringInputError`

Import: `cruxible_client.contracts.authoring.inputs.AuthoringInputError`. [Source](src/cruxible_client/contracts/authoring/inputs.py)

| Field | Type | Default / construction |
|---|---|---|
| `code` | `str` | `Required` |
| `field_path` | `str` | `Required` |
| `message` | `str` | `Required` |
| `repair` | `str` | `Required` |

## Lower-level HTTP client

`CruxibleClient` is synchronous, exported from `cruxible_client`. Construct
with exactly one of base_url or socket_path, an optional explicit bearer
token, and an optional principal ID sent as `X-Cruxible-Principal-Id` (with
daemon auth off it is a claim of identity, not authentication); unlike Cruxible.connect, this constructor does not resolve workspace
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
CruxibleClient(*, base_url: str | None=None, socket_path: str | None=None, token: str | None=None, principal_id: str | None=None) -> None
```

<a id="api-cruxibleclient-close"></a>

### `CruxibleClient.close`

[Source](src/cruxible_client/transport/http.py)

```text
close() -> None
```

<a id="api-cruxibleclient-enter"></a>

### `CruxibleClient.__enter__`

[Source](src/cruxible_client/transport/http.py)

```text
__enter__() -> CruxibleClient
```

<a id="api-cruxibleclient-exit"></a>

### `CruxibleClient.__exit__`

[Source](src/cruxible_client/transport/http.py)

```text
__exit__(*_args: object) -> None
```

<a id="api-cruxibleclient-version"></a>

### `CruxibleClient.version`

[Source](src/cruxible_client/transport/http.py)

```text
version() -> str
```

<a id="api-cruxibleclient-daemon-identity"></a>

### `CruxibleClient.daemon_identity`

[Source](src/cruxible_client/transport/http.py)

```text
daemon_identity() -> tuple[str, str | None]
```

The daemon's version and the boot id of its process image, from `/version`. A
restarted daemon answers with a new boot id.

<a id="api-cruxibleclient-check-projection-blocks"></a>

### `CruxibleClient.check_projection_blocks`

[Source](src/cruxible_client/transport/http.py)

```text
check_projection_blocks(
    instance_id: str,
    *,
    request: contracts.ProjectionCheckRequest,
) -> contracts.ProjectionCheckResult
```

HTTP: `POST f'/api/v1/{instance_id}/projections/check'`.

<a id="api-cruxibleclient-read-block-sync-backing"></a>

### `CruxibleClient.read_block_sync_backing`

[Source](src/cruxible_client/transport/http.py)

```text
read_block_sync_backing(
    instance_id: str,
    *,
    request: contracts.BlockSyncReadRequest,
) -> contracts.BlockSyncReadResult
```

HTTP: `POST f'/api/v1/{instance_id}/projections/sync-backing'`.

<a id="api-cruxibleclient-server-info"></a>

### `CruxibleClient.server_info`

[Source](src/cruxible_client/transport/http.py)

```text
server_info() -> contracts.ServerInfoResult
```

HTTP: `GET '/api/v1/server/info'`.

<a id="api-cruxibleclient-server-restart"></a>

### `CruxibleClient.server_restart`

[Source](src/cruxible_client/transport/http.py)

```text
server_restart() -> contracts.ServerRestartResult
```

HTTP: `POST '/api/v1/server/restart'`.

<a id="api-cruxibleclient-server-stop"></a>

### `CruxibleClient.server_stop`

[Source](src/cruxible_client/transport/http.py)

```text
server_stop() -> contracts.ServerStopResult
```

HTTP: `POST '/api/v1/server/stop'`.

<a id="api-cruxibleclient-create-host"></a>

### `CruxibleClient.create_host`

[Source](src/cruxible_client/transport/http.py)

```text
create_host(
    *,
    instance_id: str | None = None,
    workspace_root: str | None = None,
) -> contracts.HostResult
```

HTTP: `POST '/api/v1/runtime/instances'`.

<a id="api-cruxibleclient-declare-block"></a>

### `CruxibleClient.declare_block`

[Source](src/cruxible_client/transport/http.py)

```text
declare_block(
    instance_id: str,
    stamp: Mapping[str, Any],
) -> contracts.BlockDeclareResult
```

HTTP: `POST f'/api/v1/{instance_id}/blocks/declare'`.

<a id="api-cruxibleclient-depublish-block"></a>

### `CruxibleClient.depublish_block`

[Source](src/cruxible_client/transport/http.py)

```text
depublish_block(
    instance_id: str,
    source_id: str,
    block_id: str,
) -> contracts.BlockDepublishResult
```

HTTP: `POST f'/api/v1/{instance_id}/blocks/depublish'`.

<a id="api-cruxibleclient-host-workspace-detach"></a>

### `CruxibleClient.host_workspace_detach`

[Source](src/cruxible_client/transport/http.py)

```text
host_workspace_detach(instance_id: str) -> contracts.WorkspaceDetachResult
```

HTTP: `POST f'/api/v1/{instance_id}/workspace-detach'`.

<a id="api-cruxibleclient-host-workspace-registration"></a>

### `CruxibleClient.host_workspace_registration`

[Source](src/cruxible_client/transport/http.py)

```text
host_workspace_registration(instance_id: str) -> contracts.HostWorkspaceRegistration
```

HTTP: `GET f'/api/v1/{instance_id}/workspace-registration'`.

<a id="api-cruxibleclient-show-host"></a>

### `CruxibleClient.show_host`

[Source](src/cruxible_client/transport/http.py)

```text
show_host(instance_id: str) -> contracts.HostInspection
```

HTTP: `GET f'/api/v1/{instance_id}/host'`.

<a id="api-cruxibleclient-claim-runtime-bootstrap"></a>

### `CruxibleClient.claim_runtime_bootstrap`

[Source](src/cruxible_client/transport/http.py)

```text
claim_runtime_bootstrap(
    instance_id: str,
    bootstrap_secret: str,
) -> contracts.RuntimeCredentialBootstrapResult
```

HTTP: `POST f'/api/v1/{instance_id}/runtime/bootstrap/claim'`.

<a id="api-cruxibleclient-create-runtime-credential"></a>

### `CruxibleClient.create_runtime_credential`

[Source](src/cruxible_client/transport/http.py)

```text
create_runtime_credential(
    instance_id: str,
    *,
    principal_id: str,
    permission_mode: contracts.RuntimeCredentialPermissionMode,
    label: str | None = None,
    principal_proof: RuntimeCredentialPrincipalProof | None = None,
) -> contracts.RuntimeCredentialResult
```

HTTP: `POST f'/api/v1/{instance_id}/runtime/credentials'`.

Mints a credential that acts as `principal_id`. The daemon refuses unless the
principal is registered and active and the request carries its authority:
either the request already acts as that principal, or `principal_proof` is its
single-use consent signed with its registered key
(`cruxible_client.authoring.signing.sign_runtime_credential_mint`). An admin
credential alone is never enough. `label` is a description only.

<a id="api-cruxibleclient-list-runtime-credentials"></a>

### `CruxibleClient.list_runtime_credentials`

[Source](src/cruxible_client/transport/http.py)

```text
list_runtime_credentials(instance_id: str) -> contracts.RuntimeCredentialListResult
```

HTTP: `GET f'/api/v1/{instance_id}/runtime/credentials'`.

<a id="api-cruxibleclient-revoke-runtime-credential"></a>

### `CruxibleClient.revoke_runtime_credential`

[Source](src/cruxible_client/transport/http.py)

```text
revoke_runtime_credential(instance_id: str, credential_id: str) -> contracts.RuntimeCredentialResult
```

HTTP: `POST f'/api/v1/{instance_id}/runtime/credentials/{credential_id}/revoke'`.

<a id="api-cruxibleclient-rotate-runtime-credential"></a>

### `CruxibleClient.rotate_runtime_credential`

[Source](src/cruxible_client/transport/http.py)

```text
rotate_runtime_credential(
    instance_id: str,
    credential_id: str,
    *,
    principal_proof: RuntimeCredentialPrincipalProof | None = None,
) -> contracts.RuntimeCredentialResult
```

HTTP: `POST f'/api/v1/{instance_id}/runtime/credentials/{credential_id}/rotate'`.

A credential bound to a principal is replaced only with that principal's
authority, exactly as minting one: the request acts as the principal, or
`principal_proof` is its signed consent to the credential's current mode and
label. An unbound operator credential rotates on the admin tier alone.

<a id="api-cruxibleclient-init"></a>

### `CruxibleClient.init`

[Source](src/cruxible_client/transport/http.py)

```text
init(
    instance_id: str,
    *,
    principals: Sequence[Mapping[str, Any]],
    operating_profile: Literal['local', 'cloud'] = 'local',
    require_independent_approval: bool = False,
    workspace_root: str | None = None,
    git_object_format: Literal['sha1', 'sha256'] | None = None,
    mirror_url: str | None = None,
) -> contracts.InitResult
```

HTTP: `POST f'/api/v1/{instance_id}/init'`.

<a id="api-cruxibleclient-store-body"></a>

### `CruxibleClient.store_body`

[Source](src/cruxible_client/transport/http.py)

```text
store_body(instance_id: str, content: bytes) -> contracts.CasObjectResult
```

HTTP: `POST f'/api/v1/{instance_id}/bodies'`.

<a id="api-cruxibleclient-decommission-instance"></a>

### `CruxibleClient.decommission_instance`

[Source](src/cruxible_client/transport/http.py)

```text
decommission_instance(
    instance_id: str,
    *,
    reason: str,
) -> contracts.InstanceDecommissionResult
```

HTTP: `POST f'/api/v1/{instance_id}/instance/decommission'`.

<a id="api-cruxibleclient-set-ledger-mirror"></a>

### `CruxibleClient.set_ledger_mirror`

[Source](src/cruxible_client/transport/http.py)

```text
set_ledger_mirror(instance_id: str, *, url: str) -> contracts.LedgerMirror
```

HTTP: `POST f'/api/v1/{instance_id}/ledger/mirror'`.

<a id="api-cruxibleclient-publish-ledger"></a>

### `CruxibleClient.publish_ledger`

[Source](src/cruxible_client/transport/http.py)

```text
publish_ledger(instance_id: str, *, timeout: float=60.0) -> contracts.LedgerMirror
```

HTTP: `POST f'/api/v1/{instance_id}/ledger/publish'`.

<a id="api-cruxibleclient-get-ledger-mirror"></a>

### `CruxibleClient.get_ledger_mirror`

[Source](src/cruxible_client/transport/http.py)

```text
get_ledger_mirror(instance_id: str) -> contracts.LedgerMirror
```

HTTP: `GET f'/api/v1/{instance_id}/ledger/mirror'`.

<a id="api-cruxibleclient-list-provider-packages"></a>

### `CruxibleClient.list_provider_packages`

[Source](src/cruxible_client/transport/http.py)

```text
list_provider_packages(instance_id: str) -> ProviderCatalog
```

HTTP: `GET f'/api/v1/{instance_id}/providers'`.

<a id="api-cruxibleclient-install-provider"></a>

### `CruxibleClient.install_provider`

[Source](src/cruxible_client/transport/http.py)

```text
install_provider(
    instance_id: str,
    request: ProviderInstallRequest,
) -> ProviderInstallResult
```

HTTP: `POST f'/api/v1/{instance_id}/providers/install'`.

<a id="api-cruxibleclient-propose-document"></a>

### `CruxibleClient.propose_document`

[Source](src/cruxible_client/transport/http.py)

```text
propose_document(
    instance_id: str,
    *,
    shell: Mapping[str, Any],
    proposal_name: str,
    source_compilation_digest: str | None = None,
    base: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
) -> contracts.ProposalInspection
```

HTTP: `POST f'/api/v1/{instance_id}/documents/proposals'`.

<a id="api-cruxibleclient-propose-compiler-upgrade"></a>

### `CruxibleClient.propose_compiler_upgrade`

[Source](src/cruxible_client/transport/http.py)

```text
propose_compiler_upgrade(
    instance_id: str,
    *,
    target: CompilerCoordinate,
    base: AcceptedCoordinate | contracts.AcceptedCoordinate,
    proposal_name: str,
) -> contracts.ProposalInspection
```

HTTP: `POST f'/api/v1/{instance_id}/compiler/proposals'`.

<a id="api-cruxibleclient-propose-principal-change"></a>

### `CruxibleClient.propose_principal_change`

[Source](src/cruxible_client/transport/http.py)

```text
propose_principal_change(
    instance_id: str,
    *,
    principal: Mapping[str, Any],
    proposal_name: str,
    base: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
) -> contracts.ProposalInspection
```

HTTP: `POST f'/api/v1/{instance_id}/principals/proposals'`.

<a id="api-cruxibleclient-whoami"></a>

### `CruxibleClient.whoami`

[Source](src/cruxible_client/transport/http.py)

```text
whoami(instance_id: str) -> contracts.WhoAmI
```

HTTP: `GET f'/api/v1/{instance_id}/whoami'`.

<a id="api-cruxibleclient-head"></a>

### `CruxibleClient.head`

[Source](src/cruxible_client/transport/http.py)

```text
head(
    instance_id: str,
    *,
    at: contracts.AcceptedCoordinate | Mapping[str, Any] | str | None = None,
) -> contracts.Head
```

HTTP: `GET f'/api/v1/{instance_id}/head'`. The accepted head (or the coordinate `at` names) as `Head` `{instance, coordinate, generation}` and nothing else: the cheapest read, which `Cruxible.refresh()` uses in place of `orient`.

<a id="api-cruxibleclient-orient"></a>

### `CruxibleClient.orient`

[Source](src/cruxible_client/transport/http.py)

```text
orient(
    instance_id: str,
    *,
    kind: str | None = None,
    section: contracts.OrientSection | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    at: contracts.AcceptedCoordinate | Mapping[str, Any] | str | None = None,
    evaluation_time: str | None = None,
    surface: contracts.OrientSurface = 'sdk',
) -> contracts.OrientResult
```

HTTP: `GET f'/api/v1/{instance_id}/orient'`. `at` is an accepted coordinate or one accepted generation's Git OID; `surface` picks how `next` suggestions are written (`mcp`, `cli` or `sdk`).

<a id="api-cruxibleclient-list-proposals"></a>

### `CruxibleClient.list_proposals`

[Source](src/cruxible_client/transport/http.py)

```text
list_proposals(
    instance_id: str,
    *,
    status: Literal['open', 'settled', 'incomplete'] | None = None,
) -> contracts.ProposalList
```

HTTP: `GET f'/api/v1/{instance_id}/proposals'`.

<a id="api-cruxibleclient-resolve-proposal-selector"></a>

### `CruxibleClient.resolve_proposal_selector`

[Source](src/cruxible_client/transport/http.py)

```text
resolve_proposal_selector(
    instance_id: str,
    selector: str,
) -> contracts.ProposalSelectorResult
```

HTTP: `GET f'/api/v1/{instance_id}/proposal-selector'`.

<a id="api-cruxibleclient-readmit-proposal"></a>

### `CruxibleClient.readmit_proposal`

[Source](src/cruxible_client/transport/http.py)

```text
readmit_proposal(instance_id: str, proposal_id: str) -> contracts.ProposalReadmitResult
```

HTTP: `POST f'/api/v1/{instance_id}/proposals/{proposal_id}/readmit'`.

<a id="api-cruxibleclient-withdraw-proposal"></a>

### `CruxibleClient.withdraw_proposal`

[Source](src/cruxible_client/transport/http.py)

```text
withdraw_proposal(
    instance_id: str,
    proposal_id: str,
    *,
    reason: str,
) -> contracts.ProposalWithdrawResult
```

HTTP: `POST f'/api/v1/{instance_id}/proposals/{proposal_id}/withdraw'`.

<a id="api-cruxibleclient-inspect-proposal"></a>

### `CruxibleClient.inspect_proposal`

[Source](src/cruxible_client/transport/http.py)

```text
inspect_proposal(instance_id: str, proposal_id: str) -> contracts.ProposalInspection
```

HTTP: `GET f'/api/v1/{instance_id}/proposals/{proposal_id}'`.

<a id="api-cruxibleclient-proposal-status"></a>

### `CruxibleClient.proposal_status`

[Source](src/cruxible_client/transport/http.py)

```text
proposal_status(instance_id: str, proposal_id: str) -> contracts.ProposalListEntry
```

One proposal's list entry at the current accepted coordinate, read by ID.
`Proposal.status()` uses it.

HTTP: `GET f'/api/v1/{instance_id}/proposals/{proposal_id}/status'`.

<a id="api-cruxibleclient-inspect-refusal"></a>

### `CruxibleClient.inspect_refusal`

[Source](src/cruxible_client/transport/http.py)

```text
inspect_refusal(instance_id: str, proposal_id: str) -> contracts.RefusalInspection
```

HTTP: `GET f'/api/v1/{instance_id}/proposals/{proposal_id}/refusal'`.

<a id="api-cruxibleclient-review-proposal"></a>

### `CruxibleClient.review_proposal`

[Source](src/cruxible_client/transport/http.py)

```text
review_proposal(
    instance_id: str,
    proposal_id: str,
    *,
    include_body: bool = False,
    workspace_observation: Mapping[str, Any] | None = None,
) -> contracts.ProposalReview
```

HTTP: `POST f'/api/v1/{instance_id}/proposals/{proposal_id}/review'`.

<a id="api-cruxibleclient-prepare-approval"></a>

### `CruxibleClient.prepare_approval`

[Source](src/cruxible_client/transport/http.py)

```text
prepare_approval(
    instance_id: str,
    proposal_id: str,
    *,
    signer_id: str,
    include_body: bool = False,
) -> contracts.ApprovalChallenge
```

HTTP: `POST f'/api/v1/{instance_id}/proposals/{proposal_id}/approval-challenge'`.

<a id="api-cruxibleclient-submit-approval"></a>

### `CruxibleClient.submit_approval`

[Source](src/cruxible_client/transport/http.py)

```text
submit_approval(
    instance_id: str,
    proposal_id: str,
    *,
    attestation: Mapping[str, Any],
) -> contracts.ApprovalReceipt
```

HTTP: `POST f'/api/v1/{instance_id}/proposals/{proposal_id}/approvals'`.

<a id="api-cruxibleclient-approve-proposal"></a>

### `CruxibleClient.approve_proposal`

[Source](src/cruxible_client/transport/http.py)

```text
approve_proposal(
    instance_id: str,
    proposal_id: str,
    *,
    signer_id: str,
    signer: Callable[[dict[str, Any]], Mapping[str, Any]],
    include_body: bool = False,
) -> contracts.ApprovalReceipt
```

<a id="api-cruxibleclient-activate-proposal"></a>

### `CruxibleClient.activate_proposal`

[Source](src/cruxible_client/transport/http.py)

```text
activate_proposal(instance_id: str, proposal_id: str) -> contracts.ActivationReceipt
```

HTTP: `POST f'/api/v1/{instance_id}/proposals/{proposal_id}/activate'`.

<a id="api-cruxibleclient-read-capture"></a>

### `CruxibleClient.read_capture`

[Source](src/cruxible_client/transport/http.py)

```text
read_capture(instance_id: str, request: CaptureReadRequest) -> CaptureRead
```

HTTP: `POST f'/api/v1/{instance_id}/captures/read'`.

<a id="api-cruxibleclient-source-context"></a>

### `CruxibleClient.source_context`

[Source](src/cruxible_client/transport/http.py)

```text
source_context(instance_id: str) -> contracts.SourceContext
```

HTTP: `GET f'/api/v1/{instance_id}/sources/context'`.

<a id="api-cruxibleclient-check-source-bundle"></a>

### `CruxibleClient.check_source_bundle`

[Source](src/cruxible_client/transport/http.py)

```text
check_source_bundle(
    instance_id: str,
    *,
    bundle: Mapping[str, Any],
) -> contracts.SourceCheckResult
```

HTTP: `POST f'/api/v1/{instance_id}/sources/check'`.

<a id="api-cruxibleclient-propose-source-bundle"></a>

### `CruxibleClient.propose_source_bundle`

[Source](src/cruxible_client/transport/http.py)

```text
propose_source_bundle(
    instance_id: str,
    *,
    bundle: Mapping[str, Any],
    source_name: str,
    proposal_name: str,
) -> contracts.ProposalInspection
```

HTTP: `POST f'/api/v1/{instance_id}/sources/proposals'`.

<a id="api-cruxibleclient-propose-claim-type"></a>

### `CruxibleClient.propose_claim_type`

[Source](src/cruxible_client/transport/http.py)

```text
propose_claim_type(
    instance_id: str,
    *,
    claim_type: Mapping[str, Any],
    proposal_name: str,
    base: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
) -> contracts.ProposalInspection
```

HTTP: `POST f'/api/v1/{instance_id}/claim-types/proposals'`.

<a id="api-cruxibleclient-propose-claim-type-input"></a>

### `CruxibleClient.propose_claim_type_input`

[Source](src/cruxible_client/transport/http.py)

```text
propose_claim_type_input(
    instance_id: str,
    *,
    input: Mapping[str, Any],
    proposal_name: str,
) -> contracts.ClaimTypeInputProposalResult
```

HTTP: `POST f'/api/v1/{instance_id}/claim-types/proposals'`.

<a id="api-cruxibleclient-migrate-claim-type"></a>

### `CruxibleClient.migrate_claim_type`

[Source](src/cruxible_client/transport/http.py)

```text
migrate_claim_type(
    instance_id: str,
    *,
    request: Mapping[str, Any],
) -> contracts.ClaimTypeMigrationResponse
```

HTTP: `POST f'/api/v1/{instance_id}/claim-types/migrations'`.

<a id="api-cruxibleclient-set"></a>

### `CruxibleClient.set`

[Source](src/cruxible_client/transport/http.py)

```text
set(instance_id: str, *, request: SetRequest) -> WriteOutcome
```

HTTP: `POST f'/api/v1/{instance_id}/set'`. A refused write is an
outcome (`status: refused`), not an HTTP error.

<a id="api-cruxibleclient-retire"></a>

### `CruxibleClient.retire`

[Source](src/cruxible_client/transport/http.py)

```text
retire(instance_id: str, *, request: RetireRequest) -> WriteOutcome
```

HTTP: `POST f'/api/v1/{instance_id}/retire'`.

<a id="api-cruxibleclient-write"></a>

### `CruxibleClient.write`

[Source](src/cruxible_client/transport/http.py)

```text
write(instance_id: str, *, request: WriteRequest) -> WriteOutcome
```

HTTP: `POST f'/api/v1/{instance_id}/write'`.

<a id="api-cruxibleclient-append-claim-attestation"></a>

### `CruxibleClient.append_claim_attestation`

[Source](src/cruxible_client/transport/http.py)

```text
append_claim_attestation(
    instance_id: str,
    *,
    request: ClaimAttestationAppendRequest,
) -> ClaimAttestationAppendResult
```

HTTP: `POST f'/api/v1/{instance_id}/claim-attestations'`.

<a id="api-cruxibleclient-recover-claim-attestations"></a>

### `CruxibleClient.recover_claim_attestations`

[Source](src/cruxible_client/transport/http.py)

```text
recover_claim_attestations(instance_id: str) -> None
```

HTTP: `POST f'/api/v1/{instance_id}/claim-attestations/recover'`.

<a id="api-cruxibleclient-resolution-contracts"></a>

### `CruxibleClient.resolution_contracts`

[Source](src/cruxible_client/transport/http.py)

```text
resolution_contracts(
    instance_id: str,
    *,
    request: contracts.ResolutionContractsRequest,
) -> contracts.ResolutionContractsResult
```

HTTP: `POST f'/api/v1/{instance_id}/resolution-contracts/query'`.

<a id="api-cruxibleclient-predict"></a>

### `CruxibleClient.predict`

[Source](src/cruxible_client/transport/http.py)

```text
predict(
    instance_id: str,
    *,
    request: contracts.PredictRequest,
) -> contracts.PredictResult
```

HTTP: `POST f'/api/v1/{instance_id}/predictions'`.

<a id="api-cruxibleclient-settle-prediction"></a>

### `CruxibleClient.settle_prediction`

[Source](src/cruxible_client/transport/http.py)

```text
settle_prediction(
    instance_id: str,
    prediction_id: str,
    *,
    request: contracts.SettleRequest,
) -> contracts.SettleResult
```

HTTP: `POST f'/api/v1/{instance_id}/predictions/{prediction_id}/settlements'`.

<a id="api-cruxibleclient-get-authoring-intent"></a>

### `CruxibleClient.get_authoring_intent`

[Source](src/cruxible_client/transport/http.py)

```text
get_authoring_intent(instance_id: str, intent_id: str) -> contracts.AuthoringIntentViewRecord
```

HTTP: `GET f'/api/v1/{instance_id}/authoring/intents/{intent_id}'`.

<a id="api-cruxibleclient-list-pending-authoring-intents"></a>

### `CruxibleClient.list_pending_authoring_intents`

[Source](src/cruxible_client/transport/http.py)

```text
list_pending_authoring_intents(instance_id: str) -> contracts.AuthoringIntentListRecord
```

HTTP: `GET f'/api/v1/{instance_id}/authoring/intents'`.

<a id="api-cruxibleclient-compile-authoring"></a>

### `CruxibleClient.compile_authoring`

[Source](src/cruxible_client/transport/http.py)

```text
compile_authoring(
    instance_id: str,
    *,
    payload: Mapping[str, Any],
    intent_id: str | None = None,
    reference_expectations: Sequence[Mapping[str, Any]] | None = None,
    program_stamp: Mapping[str, Any] | None = None,
) -> contracts.AuthoringPreflightResult
```

HTTP: `POST f'/api/v1/{instance_id}/authoring/compile'`.

<a id="api-cruxibleclient-compile-authoring-input"></a>

### `CruxibleClient.compile_authoring_input`

[Source](src/cruxible_client/transport/http.py)

```text
compile_authoring_input(
    instance_id: str,
    *,
    input: Mapping[str, Any],
    intent_id: str | None = None,
) -> contracts.AuthoringPreflightResult
```

HTTP: `POST f'/api/v1/{instance_id}/authoring/compile'`.

<a id="api-cruxibleclient-preflight-authoring-intent"></a>

### `CruxibleClient.preflight_authoring_intent`

[Source](src/cruxible_client/transport/http.py)

```text
preflight_authoring_intent(
    instance_id: str,
    intent_id: str,
) -> contracts.AuthoringPreflightResult
```

HTTP: `POST f'/api/v1/{instance_id}/authoring/intents/{intent_id}/preflight'`.

<a id="api-cruxibleclient-rebase-authoring-intent"></a>

### `CruxibleClient.rebase_authoring_intent`

[Source](src/cruxible_client/transport/http.py)

```text
rebase_authoring_intent(
    instance_id: str,
    intent_id: str,
) -> contracts.AuthoringIntentViewRecord
```

HTTP: `POST f'/api/v1/{instance_id}/authoring/intents/{intent_id}/rebase'`.

<a id="api-cruxibleclient-submit-authoring-intent"></a>

### `CruxibleClient.submit_authoring_intent`

[Source](src/cruxible_client/transport/http.py)

```text
submit_authoring_intent(
    instance_id: str,
    intent_id: str,
) -> contracts.AuthoringSubmitResultRecord
```

HTTP: `POST f'/api/v1/{instance_id}/authoring/intents/{intent_id}/submit'`.

<a id="api-cruxibleclient-authoring-intent-status"></a>

### `CruxibleClient.authoring_intent_status`

[Source](src/cruxible_client/transport/http.py)

```text
authoring_intent_status(instance_id: str, intent_id: str) -> contracts.CandidateStatusRecord
```

HTTP: `GET f'/api/v1/{instance_id}/authoring/intents/{intent_id}/status'`.

<a id="api-cruxibleclient-read-claim-batch"></a>

### `CruxibleClient.read_claim_batch`

[Source](src/cruxible_client/transport/http.py)

```text
read_claim_batch(
    instance_id: str,
    *,
    request: ClaimReadBatchRequest,
) -> ClaimReadBatchResult
```

HTTP: `POST f'/api/v1/{instance_id}/claims/read-batch'`. SDK-internal: `Cruxible.claim_views` and World reads use it.

<a id="api-cruxibleclient-get-batch"></a>

### `CruxibleClient.get_batch`

[Source](src/cruxible_client/transport/http.py)

```text
get_batch(
    instance_id: str,
    *,
    request: GetBatchRequest,
) -> GetBatchResult
```

HTTP: `POST f'/api/v1/{instance_id}/get-batch'`. SDK-internal: several references read at one coordinate and one detail, so the SDK can read a whole vocabulary (every ClaimType envelope, for `world()`) in a few round trips. `GetBatchRequest` takes `refs` (1 to 64), `detail` (`summary` or `proof`, default `proof`), `at` and `evaluation_time`; every result answers the coordinate the first one resolved. Agents call `Cruxible.get` once per reference.

<a id="api-cruxibleclient-get-claim-backings"></a>

### `CruxibleClient.get_claim_backings`

[Source](src/cruxible_client/transport/http.py)

```text
get_claim_backings(
    instance_id: str,
    *,
    claim_ids: Sequence[str],
    at: contracts.AcceptedCoordinate | Mapping[str, Any],
) -> ClaimBackingsResult
```

HTTP: `POST f'/api/v1/{instance_id}/claims/backings'`. SDK-internal, kept beside `read_claim_batch`.

<a id="api-cruxibleclient-procedure-readiness"></a>

### `CruxibleClient.procedure_readiness`

[Source](src/cruxible_client/transport/http.py)

```text
procedure_readiness(
    instance_id: str,
    name: str,
    *,
    evaluation_time: str,
    at: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
) -> contracts.ProcedureReadiness
```

HTTP: `GET f'/api/v1/{instance_id}/procedures/{name}/readiness'`.

<a id="api-cruxibleclient-bind-procedure"></a>

### `CruxibleClient.bind_procedure`

[Source](src/cruxible_client/transport/http.py)

```text
bind_procedure(
    instance_id: str,
    name: str,
    *,
    bindings: Sequence[Mapping[str, Any]],
) -> contracts.ProcedureBindResult
```

HTTP: `POST f'/api/v1/{instance_id}/procedures/{name}/bind'`.

<a id="api-cruxibleclient-run-procedure"></a>

### `CruxibleClient.run_procedure`

[Source](src/cruxible_client/transport/http.py)

```text
run_procedure(
    instance_id: str,
    name: str,
    *,
    evaluation_time: str | None,
    at: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
    input: Any,
    resolution_contract: contracts.ResolutionContractReference | None = None,
    trigger_event: contracts.TriggerEventReference | None = None,
) -> contracts.ProcedureRunState
```

HTTP: `POST f'/api/v1/{instance_id}/procedures/{name}/runs'`.

<a id="api-cruxibleclient-get-procedure-run"></a>

### `CruxibleClient.get_procedure_run`

[Source](src/cruxible_client/transport/http.py)

```text
get_procedure_run(instance_id: str, run_id: str) -> contracts.ProcedureRunState
```

HTTP: `GET f'/api/v1/{instance_id}/procedure-runs/{run_id}'`.

<a id="api-cruxibleclient-measure-procedure"></a>

### `CruxibleClient.measure_procedure`

[Source](src/cruxible_client/transport/http.py)

```text
measure_procedure(
    instance_id: str,
    name: str,
    *,
    request: contracts.ProcedureMeasureRequest,
) -> contracts.ProcedureMeasureResult
```

HTTP: `POST f'/api/v1/{instance_id}/procedures/{name}/measurements'`.

<a id="api-cruxibleclient-list-procedure-readings"></a>

### `CruxibleClient.list_procedure_readings`

[Source](src/cruxible_client/transport/http.py)

```text
list_procedure_readings(
    instance_id: str,
    name: str,
    *,
    request: contracts.ProcedureReadingsRequest,
) -> contracts.ProcedureReadingsResult
```

HTTP: `POST f'/api/v1/{instance_id}/procedures/{name}/readings'`.

<a id="api-cruxibleclient-run-line"></a>

### `CruxibleClient.run_line`

[Source](src/cruxible_client/transport/http.py)

```text
run_line(
    instance_id: str,
    line: str,
    *,
    occurrence_id: str | None,
    evaluation_time: str | None = None,
    resolution_contract: contracts.ResolutionContractReference | None = None,
    trigger_event: contracts.TriggerEventReference | None = None,
    trigger: str | None = None,
) -> contracts.ProcedureRunState
```

HTTP: `POST f'/api/v1/{instance_id}/lines/{line_identity_digest}/runs'`.

<a id="api-cruxibleclient-next"></a>

### `CruxibleClient.next`

[Source](src/cruxible_client/transport/http.py)

```text
next(
    instance_id: str,
    *,
    evaluation_time: str,
    access_profile: Mapping[str, Any],
    at: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
    expiring_within: Mapping[str, Any] | None = None,
    workspace_observation: Mapping[str, Any] | None = None,
    since_result_digest: str | None = None,
    at_attestation_head_digest: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> contracts.NextResult
```

HTTP: `POST f'/api/v1/{instance_id}/next'`.

<a id="api-cruxibleclient-since"></a>

### `CruxibleClient.since`

[Source](src/cruxible_client/transport/http.py)

```text
since(
    instance_id: str,
    *,
    generation: int,
    access_profile: Mapping[str, Any],
    at: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
    max_rows: int = 100,
    max_bytes: int = 65536,
    cursor: contracts.SinceCursor | Mapping[str, Any] | None = None,
) -> contracts.SinceResult
```

HTTP: `POST f'/api/v1/{instance_id}/since'`.

<a id="api-cruxibleclient-list-curation"></a>

### `CruxibleClient.list_curation`

[Source](src/cruxible_client/transport/http.py)

```text
list_curation(
    instance_id: str,
    *,
    evaluation_time: str,
    access_profile: Mapping[str, Any],
    workspace_observation: Mapping[str, Any] | None = None,
) -> contracts.CurationListResult
```

HTTP: `POST f'/api/v1/{instance_id}/curation/list'`.

<a id="api-cruxibleclient-audit"></a>

### `CruxibleClient.audit`

[Source](src/cruxible_client/transport/http.py)

```text
audit(
    instance_id: str,
    *,
    evaluation_time: str,
    access_profile: Mapping[str, Any],
    at: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
    claim_type_identities: tuple[str, ...] = (),
    subject_kinds: tuple[str, ...] = (),
    max_rows: int = 100,
    max_bytes: int = 65536,
    cursor: contracts.AuditCursor | Mapping[str, Any] | None = None,
) -> contracts.AuditResult
```

HTTP: `POST f'/api/v1/{instance_id}/audit'`.

<a id="api-cruxibleclient-overrule-curation"></a>

### `CruxibleClient.overrule_curation`

[Source](src/cruxible_client/transport/http.py)

```text
overrule_curation(
    instance_id: str,
    *,
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    attribution_refs: tuple[str, ...] = (),
) -> contracts.CurationActionResult
```

HTTP: `POST f'/api/v1/{instance_id}/curation/overrule'`.

<a id="api-cruxibleclient-accept-fixed-curation"></a>

### `CruxibleClient.accept_fixed_curation`

[Source](src/cruxible_client/transport/http.py)

```text
accept_fixed_curation(
    instance_id: str,
    *,
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    accepted_proposal_id: str,
    accepted_changeset_digest: str,
    attribution_refs: tuple[str, ...] = (),
) -> contracts.CurationActionResult
```

HTTP: `POST f'/api/v1/{instance_id}/curation/accept-fixed'`.

<a id="api-cruxibleclient-suppress-curation"></a>

### `CruxibleClient.suppress_curation`

[Source](src/cruxible_client/transport/http.py)

```text
suppress_curation(
    instance_id: str,
    *,
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    scope: Literal['item', 'pattern', 'instance'],
    until_generation: int | None = None,
    attribution_refs: tuple[str, ...] = (),
) -> contracts.CurationActionResult
```

HTTP: `POST f'/api/v1/{instance_id}/curation/suppress'`.

<a id="api-cruxibleclient-resolve-coverage"></a>

### `CruxibleClient.resolve_coverage`

[Source](src/cruxible_client/transport/http.py)

```text
resolve_coverage(
    instance_id: str,
    *,
    observations: Sequence[Mapping[str, Any]],
    at: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
    budget: Mapping[str, Any] | None = None,
    scan_budget: Mapping[str, Any] | None = None,
) -> contracts.CoverageResult
```

Resolve coverage for a batch of already-observed working sources.

The caller observes its own working set -- binding each path to a
declared logical source and hashing the bytes it actually read -- and
this call carries those observations. Coverage is delivered against
them; the daemon reads no client filesystem.

HTTP: `POST f'/api/v1/{instance_id}/coverage/resolve'`.

<a id="api-cruxibleclient-export-floor"></a>

### `CruxibleClient.export_floor`

[Source](src/cruxible_client/transport/http.py)

```text
export_floor(
    instance_id: str,
    *,
    at: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
    format_version: Literal[2, 4] = 4,
    include: Sequence[contracts.FloorExportPart] = (),
    review_notes_oid: str | None = None,
) -> contracts.FloorExport
```

HTTP: `POST f'/api/v1/{instance_id}/floor/export'`.

## Errors and unsupported surfaces

| Family | Handling |
|---|---|
| SdkError | Local SDK refusal, also a ValueError/CoreError; inspect code and error-specific fields. |
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
`DerivationSpec` is a value type, not proof of a served
derivation writer.

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
| `ClaimAdmissionPolicy` | `cruxible_client.contracts.policies` · [Source](src/cruxible_client/contracts/policies.py) |
| `ClaimAttestationSigner` | `cruxible_client.authoring.attestations` · [Source](src/cruxible_client/authoring/attestations.py) |
| `ClaimRef` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `ClaimRole` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `ClaimTypeRef` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `ClaimResolutionPolicy` | `cruxible_client.contracts.policies` · [Source](src/cruxible_client/contracts/policies.py) |
| `CruxibleClient` | `cruxible_client.transport.http` · [Source](src/cruxible_client/transport/http.py) |
| `Disposition` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `Duration` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `CanonicalDuration` | `cruxible_client.contracts.captures` · [Source](src/cruxible_client/contracts/captures.py) |
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
| `Cruxible` | `cruxible_client.authoring.sdk` · [Source](src/cruxible_client/authoring/sdk.py) |
| `Prediction` | `cruxible_client.authoring.sdk` · [Source](src/cruxible_client/authoring/sdk.py) |
| `PredictionSettlement` | `cruxible_client.authoring.sdk` · [Source](src/cruxible_client/authoring/sdk.py) |
| `InsertionApplyError` | `cruxible_client.authoring.insertions` · [Source](src/cruxible_client/authoring/insertions.py) |
| `WorkspaceError` | `cruxible_client.authoring.workspace` · [Source](src/cruxible_client/authoring/workspace.py) |
| `activate_with_workspace_refresh` | `cruxible_client.authoring.workspace` · [Source](src/cruxible_client/authoring/workspace.py) |
| `inspect_workspace_floor` | `cruxible_client.authoring.workspace` · [Source](src/cruxible_client/authoring/workspace.py) |
| `observe_next_workspace` | `cruxible_client.authoring.workspace` · [Source](src/cruxible_client/authoring/workspace.py) |
| `materialize_floor` | `cruxible_client.authoring.workspace` · [Source](src/cruxible_client/authoring/workspace.py) |
| `ProcedureRef` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `ProcedureBudget` | `cruxible_client.contracts.procedures.models` · [Source](src/cruxible_client/contracts/procedures/models.py) |
| `ProcedureDefinitionV3` | `cruxible_client.contracts.procedures.models` · [Source](src/cruxible_client/contracts/procedures/models.py) |
| `ProcedureHardCaps` | `cruxible_client.contracts.procedures.models` · [Source](src/cruxible_client/contracts/procedures/models.py) |
| `ProcedureOwnedContract` | `cruxible_client.contracts.procedures.artifacts` · [Source](src/cruxible_client/contracts/procedures/artifacts.py) |
| `ProcedurePinSlotRef` | `cruxible_client.contracts.procedures.models` · [Source](src/cruxible_client/contracts/procedures/models.py) |
| `ProcedurePinSlot` | `cruxible_client.contracts.procedures.models` · [Source](src/cruxible_client/contracts/procedures/models.py) |
| `ProjectNode` | `cruxible_client.contracts.procedures.models` · [Source](src/cruxible_client/contracts/procedures/models.py) |
| `PropertySchema` | `cruxible_client.contracts.procedures.contract_schema` · [Source](src/cruxible_client/contracts/procedures/contract_schema.py) |
| `QueryRef` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `ReferentSensitivity` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `ProcedureSlotRef` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `SourceRef` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `StateTapNodeV3` | `cruxible_client.contracts.procedures.models` · [Source](src/cruxible_client/contracts/procedures/models.py) |
| `SubjectRef` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `TypedRef` | `cruxible_client.authoring.sdk_types` · [Source](src/cruxible_client/authoring/sdk_types.py) |
| `TransformNode` | `cruxible_client.contracts.procedures.models` · [Source](src/cruxible_client/contracts/procedures/models.py) |
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
prepare_claim_attestation(client: 'Any', instance_id: 'str', *, prepared: 'PreparedClaimAttestationRequest', signer: 'ClaimAttestationSigner') -> 'ClaimAttestation'
```

Bind and sign an exact accepted Claim without appending or accepting it.

#### `append_prepared_claim_attestation`

```text
append_prepared_claim_attestation(client: 'Any', instance_id: 'str', *, prepared: 'PreparedClaimAttestationRequest', signer: 'ClaimAttestationSigner') -> 'ClaimAttestationAppendResult'
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

ParsedProjectionBlock(source_id: 'str', block_id: 'str', stamp: 'ProjectionBlockStampAny | None', opening_start: 'int', opening_end: 'int', body_start: 'int', body_end: 'int', closing_end: 'int', body_digest: 'str')

#### `ProjectionIndependentEvidenceForbidden`

A citation's span lies inside a projection block: the client fast path.

Evidence never comes from a projection window, whatever the citation's role
or origin. The daemon refuses the same span with the same code at lowering
and at the citation gate; this raises before the wire so an author learns
it without a round trip.

#### `ProjectionMarkerError`

Base class for Cruxible protocol and storage refusals.

#### `ProjectionRepinError`

Base class for Cruxible protocol and storage refusals.

#### `ProjectionSyncError`

Base class for Cruxible protocol and storage refusals.

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
frame_projection_block(*, stamp: 'ProjectionBlockStampAny', body: 'bytes', compact: 'bool' = False) -> 'bytes'
```

Mechanically frame accepted bytes and prove the one frozen marker grammar.

#### `parse_projection_blocks`

```text
parse_projection_blocks(content: 'bytes', *, source_id: 'str', allow_bootstrap: 'bool' = False, manifests: 'Mapping[str, bytes] | None' = None) -> 'tuple[ParsedProjectionBlock, ...]'
```

Parse one complete source using its known logical source identity.

#### `render_projection_opening`

```text
render_projection_opening(stamp: 'ProjectionBlockStampAny') -> 'bytes'
```

#### `repin_projection_block`

```text
repin_projection_block(client: 'CruxibleClient', instance_id: 'str', *, workspace: 'str | Path', source_id: 'str', block_id: 'str', claims: 'Sequence[str] | None' = None, queries: 'Sequence[tuple[str, Mapping[str, object]]] | None' = None, artifacts: 'Sequence[ArtifactIdentity] | None' = None, currency_policy: 'ProjectionCurrencyPolicy | None' = None, backing_digest: 'str | None' = None, evaluation_time: 'datetime', coordinate: 'AcceptedCoordinate | None' = None, body: 'bytes | None' = None, compact: 'bool' = True) -> 'ProjectionBlockStamp'
```

Repin one block, optionally installing explicitly supplied agent-authored body bytes.

Omitted body preserves prose. Compact markers are the default; their manifests
are retained before the page write. Use a reviewed exact-content package Claim
for ledger recovery.
The whole-file compare-and-swap preserves concurrent author edits.

#### `sync_projection_blocks`

```text
sync_projection_blocks(client: 'CruxibleClient', instance_id: 'str', *, workspace: 'str | Path', paths: 'Sequence[str | Path]' = (), all_sources: 'bool' = False, check: 'bool' = False, detach_paths: 'Sequence[str | Path]' = ()) -> 'BlockSyncResult'
```

Check all dependencies without authoring prose. Only explicit detach edits files.

### Module `cruxible_client.authoring.context`

[Source](src/cruxible_client/authoring/context.py)

#### `ContextResolutionError`

A selected workspace or target layer is not a safe context source.

#### `WorkspaceBinding`

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

#### `ResolvedContext`

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

#### `resolve_context`

```text
resolve_context(*, server_url: 'str | None' = None, server_socket: 'str | None' = None, instance_id: 'str | None' = None, workspace: 'str | Path | None' = None, remembered: 'Mapping[str, object] | None' = None, environ: 'Mapping[str, str] | None' = None, cwd: 'Path | None' = None, no_workspace: 'bool' = False, home: 'Path | None' = None) -> 'ResolvedContext'
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

#### `claim_revision_example`

```text
claim_revision_example() -> 'ClaimInput'
```

Revise one accepted Claim: the same statement slot, a new generation of it.

`revises` names the Claim ID the revision replaces. Omitting it states a new
Claim instead, with its own freshly minted ID.

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
procedure_mandate_example() -> 'ProcedureMandateInput'
```

A propose grant over the `--example procedure` Procedure, within its caps.

Only a Line that proposes or settles needs a mandate; an observe-only Line
(like the example Procedure's) runs without one.

#### `line_example`

```text
line_example() -> 'LineInput'
```

A Line over the `--example procedure` Procedure.

That Procedure has no Source nodes, so the Line names no acquisition
policy, and its input contract is empty, so `parameters` is `{}`. It only
observes, so it runs without a ProcedureMandate. With no Trigger aimed at it
it runs when run explicitly; `--example trigger` schedules it.

#### `trigger_example`

```text
trigger_example() -> 'TriggerInput'
```

A Trigger that runs the `--example line` Line hourly, on the hour, in UTC.

Cron expressions are evaluated in UTC; convert local times first (09:00 New
York in winter is 14:00 UTC). Name `line_name` or `action`, never both; an
internal action takes `cadence` or `cron` only in this version. Nothing fires
before the Trigger is accepted.

#### `acquisition_policy_example`

```text
acquisition_policy_example() -> 'AcquisitionPolicyInput'
```

A SourceAcquisitionPolicy with one required input, for a Line with Source nodes.

Each rule's `input_name` is one Source node's output alias (`as`); a Line
names this policy in `acquisition_policy_name`.

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

#### `InsertionApplyError`

A local source cannot be reconciled with its insertion expectation.

#### `replace_publication_file`

```text
replace_publication_file(path: 'Path', *, expected: 'bytes', replacement: 'bytes') -> 'None'
```

Durably replace one exact preimage without overwriting a concurrent edit.

### Module `cruxible_client.authoring.projection_manifests`

[Source](src/cruxible_client/authoring/projection_manifests.py)

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

#### `SeedBundleEntry`

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

#### `SeedCarriedEntry`

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

#### `SeedPlan`

```text
tag: Literal['playbill-seed-plan-v1'] = 'playbill-seed-plan-v1'
proposal_name: str
body_paths: tuple[str, ...] = ()
groups: tuple[SeedProposalGroup, ...] = ()
carried: tuple[SeedCarriedEntry, ...] = ()
```

```text
group_ids() -> tuple[str, ...]
```

```text
group(group_id: str) -> SeedProposalGroup
```

The whole grouping, before a byte is stored or a proposal is opened.

Deterministic in the bundle's bytes alone: no clock, no accepted state, no
instance. That is what lets `--plan` answer offline and lets a run manifest
pin `plan_digest` as evidence that two arms seeded the same world.

#### `SeedPlanResult`

```text
tag: Literal['playbill-seed-plan-result-v1'] = 'playbill-seed-plan-result-v1'
plan: SeedPlan
plan_digest: str
rendered: tuple[str, ...]
```

The typed result of offline seed planning: the complete plan, its digest, and
its rendered explanation. It does not indicate that the plan was submitted.

#### `SeedProposalGroup`

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
plan_seed_bundle(files: 'Mapping[str, bytes]', *, proposal_name: 'str') -> 'SeedPlan'
```

Group one bundle into the fewest proposals its own closures allow.

#### `plan_seed_directory`

```text
plan_seed_directory(root: 'Path', *, proposal_name: 'str') -> 'SeedPlanResult'
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
read_seed_bundle(files: 'Mapping[str, bytes]') -> 'tuple[SeedBundleEntry, ...]'
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
render_seed_plan(plan: 'SeedPlan') -> 'tuple[str, ...]'
```

The human rendering: what will be proposed, in order, and what rides along.

#### `seed_group_operation_digest`

```text
seed_group_operation_digest(plan: 'SeedPlan', group: 'SeedProposalGroup') -> 'Sha256Value'
```

Bind one planned group to its exact, name-independent bundle content.

#### `seed_group_proposal_name`

```text
seed_group_proposal_name(plan: 'SeedPlan', group: 'SeedProposalGroup') -> 'str'
```

Return the machine-owned proposal-ref leaf for one seed operation.

#### `seed_plan_digest`

```text
seed_plan_digest(plan: 'SeedPlan') -> 'Sha256Value'
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

#### `WorkspaceAttachmentError`

Daemon registration and the requested client workspace disagree.

#### `WorkspaceError`

A client workspace or exported floor failed deterministic validation.

#### `activate_with_workspace_refresh`

```text
activate_with_workspace_refresh(client: '_FloorClient', instance_id: 'str', proposal_id: 'str', *, workspace: 'str | Path', sync: 'bool' = True) -> 'contracts.WorkspaceActivationResult'
```

Activate once, refresh the floor, then independently sync local blocks.

#### `configured_floor_path`

```text
configured_floor_path(workspace: 'str | Path') -> 'str | None'
```

Return the declared v2 floor path, or `None` when absent/unconfigured.

#### `inspect_workspace_floor`

```text
inspect_workspace_floor(workspace: 'str | Path', *, current_coordinate: 'contracts.AcceptedCoordinate | None') -> 'contracts.WorkspaceFloorStatus'
```

Compare the installed configured floor with a daemon coordinate.

#### `observe_next_workspace`

```text
observe_next_workspace(workspace: 'str | Path') -> 'dict[str, object]'
```

Observe the configured floor and every resolvable installed catalog source.

The daemon compares `installed_coordinate` with its resolved coordinate.  Therefore
the local `stale` spelling produced without a daemon coordinate is only a transport
hint; it cannot manufacture a stale or current queue item. Invalid catalogs leave
sources unobserved; individual unavailable sources are omitted from an otherwise
valid observation so the daemon can explain each accepted citation separately.

#### `observe_next_workspace_with_coverage`

```text
observe_next_workspace_with_coverage(client: '_CoverageClient', instance_id: 'str', workspace: 'str | Path', *, observation: 'Mapping[str, object] | None' = None, coordinate: 'contracts.AcceptedCoordinate | Mapping[str, Any] | None' = None, access_profile: 'Mapping[str, Any] | None' = None, resolve_coordinate: 'Callable[[], contracts.AcceptedCoordinate] | None' = None) -> 'tuple[dict[str, object], contracts.AcceptedCoordinate | None]'
```

Enrich next with one existing, coordinate-bound coverage-scanner read.

This adapter never searches source bytes. Every accepted occurrence comes
from the existing server coverage card; the local slice check only verifies
that card against the exact bytes it previously sent to the sole scanner.

#### `observe_projection_coverage`

```text
observe_projection_coverage(workspace: 'str | Path', *, coordinate: 'contracts.AcceptedCoordinate | Mapping[str, Any]') -> 'dict[str, object] | None'
```

Build bounded, coordinate-bound proof of configured local projections.

A valid catalog completely describes Procedure projection intent. Claim
coverage is complete only when every Document source can be read and its
complete marker set parses. Missing or malformed evidence removes that
kind from `complete_kinds` instead of manufacturing absence.

#### `materialize_floor`

```text
materialize_floor(workspace: 'str | Path', *, export: 'contracts.FloorExport', force: 'bool' = True) -> 'contracts.WorkspaceFloorWriteResult'
```

Verify and exactly replace one workspace-relative floor directory.

#### `record_floor_output`

```text
record_floor_output(workspace: 'str | Path', *, instance_id: 'str', server_url: 'str | None' = None, server_socket: 'str | None' = None) -> 'Path'
```

Record the fixed floor output while preserving safe existing coverage fields.

#### `refresh_workspace_floor`

```text
refresh_workspace_floor(client: '_FloorClient', instance_id: 'str', *, workspace: 'str | Path', at: 'contracts.AcceptedCoordinate | None' = None) -> 'contracts.FloorRefreshResult'
```

Refresh only the local floor and report the coordinate actually written.

A pinned request refuses a mismatched export before touching local files.
inspect_workspace_floor reports the installed coordinate independently,
including after a failed refresh. No projection prose or declaration changes.

#### `validate_workspace_config_write`

```text
validate_workspace_config_write(workspace: 'str | Path', *, instance_id: 'str | None', server_url: 'str | None' = None, server_socket: 'str | None' = None, replace: 'bool' = False) -> 'None'
```

Refuse a differing config before a host or init request mutates daemon state.

#### `verified_floor_files`

```text
verified_floor_files(export: 'contracts.FloorExport') -> 'dict[str, bytes]'
```

Verify the v2 envelope, manifest, inventory, and bytes.

#### `write_workspace_config`

```text
write_workspace_config(workspace: 'str | Path', *, instance_id: 'str', server_url: 'str | None' = None, server_socket: 'str | None' = None, replace: 'bool' = False) -> 'Path'
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
build_world(cx: 'Cruxible', *, coordinate: 'AcceptedCoordinate', claim_type_envelopes: 'Sequence[Mapping[str, object]]') -> 'World'
```

Assemble one world from the accepted ClaimType vocabulary.

Subject kinds come from the vocabulary rather than from the Subjects
themselves, which is what lets `cx.world()` name every kind without reading
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
`Cruxible` -- the CLI leaf, and anything else outside this package -- so no
caller has to reach for a private constructor. The workspace is only the
root a relative source selection would resolve against; this reads nothing
from it, so a directory with no Cruxible workspace is fine.

### Module `cruxible_client.provider_installation`

[Source](src/cruxible_client/provider_installation.py)

#### `install_provider_package`

```text
install_provider_package(client: 'CruxibleClient', instance_id: str, *, wheel: pathlib._local.Path, lock: pathlib._local.Path, dependency_wheels: tuple[pathlib._local.Path, ...] = (), extras: tuple[str, ...] = (), control_domain: str = 'operator', reverify: bool = False) -> cruxible_client.contracts.provider_installation.ProviderInstallResult
```

Paths are consumed here on the client; the daemon receives only CAS references.

### Contract model index

This is the complete public class index under `cruxible_client.contracts` at
the checked revision. Open the linked model for its declared fields, validation
rules, enum values, and historical format. The high-level SDK’s own return/value
fields are documented above; these links keep wire schema definitions singular.

**`contracts.__init__`** — [GitWorkspaceNote](src/cruxible_client/contracts/__init__.py), [HostResult](src/cruxible_client/contracts/__init__.py), [HostWorkspaceRegistration](src/cruxible_client/contracts/__init__.py), [HostCompatibilityReason](src/cruxible_client/contracts/__init__.py), [HostInspection](src/cruxible_client/contracts/__init__.py), [RuntimeCredentialBootstrapResult](src/cruxible_client/contracts/__init__.py), [RuntimeCredentialMetadata](src/cruxible_client/contracts/__init__.py), [RuntimeCredentialResult](src/cruxible_client/contracts/__init__.py), [RuntimeCredentialListResult](src/cruxible_client/contracts/__init__.py), [ProviderLaneStatus](src/cruxible_client/contracts/__init__.py), [ServerInfoResult](src/cruxible_client/contracts/__init__.py), [ServerRestartResult](src/cruxible_client/contracts/__init__.py), [ServerStopResult](src/cruxible_client/contracts/__init__.py), [IsolatedExecutorRegistration](src/cruxible_client/contracts/__init__.py), [AcceptedCoordinate](src/cruxible_client/contracts/__init__.py), [InitResult](src/cruxible_client/contracts/__init__.py), [CasObjectResult](src/cruxible_client/contracts/__init__.py), [ProposalInspection](src/cruxible_client/contracts/__init__.py), [ProposalListEntry](src/cruxible_client/contracts/__init__.py), [ProposalList](src/cruxible_client/contracts/__init__.py), [ProposalSelectorResult](src/cruxible_client/contracts/__init__.py), [ProposalReadmitResult](src/cruxible_client/contracts/__init__.py), [ProposalWithdrawResult](src/cruxible_client/contracts/__init__.py), [WhoAmI](src/cruxible_client/contracts/__init__.py), [RefusalInspection](src/cruxible_client/contracts/__init__.py), [SemanticFieldValue](src/cruxible_client/contracts/__init__.py), [SemanticFieldDelta](src/cruxible_client/contracts/__init__.py), [ReviewedMember](src/cruxible_client/contracts/__init__.py), [ProjectionAdvisory](src/cruxible_client/contracts/__init__.py), [ProjectionEvidence](src/cruxible_client/contracts/__init__.py), [ProposalReview](src/cruxible_client/contracts/__init__.py), [ApprovalChallenge](src/cruxible_client/contracts/__init__.py), [ApprovalReceipt](src/cruxible_client/contracts/__init__.py), [ActivationReceipt](src/cruxible_client/contracts/__init__.py), [FloorRefreshResult](src/cruxible_client/contracts/__init__.py), [WorkspaceActivationResult](src/cruxible_client/contracts/__init__.py), [SourceContext](src/cruxible_client/contracts/__init__.py), [SourceCheckResult](src/cruxible_client/contracts/__init__.py), [InstanceDecommissionResult](src/cruxible_client/contracts/__init__.py), [LedgerMirror](src/cruxible_client/contracts/__init__.py), [ClaimTypeProposalLint](src/cruxible_client/contracts/__init__.py), [ClaimTypeInputProposalResult](src/cruxible_client/contracts/__init__.py), [ClaimTypeMigrationResultV1](src/cruxible_client/contracts/__init__.py), [ClaimTypeMigrationPreflight](src/cruxible_client/contracts/__init__.py), [ClaimTypeMigrationResultV2](src/cruxible_client/contracts/__init__.py), [ClaimTypeMigrationResult](src/cruxible_client/contracts/__init__.py), [CaptureEvidenceKindAdmission](src/cruxible_client/contracts/__init__.py), [CaptureAdmissionAccount](src/cruxible_client/contracts/__init__.py), [ClaimViewRecord](src/cruxible_client/contracts/__init__.py), [CandidateStatusRecord](src/cruxible_client/contracts/__init__.py), [AuthoringIntentViewRecord](src/cruxible_client/contracts/__init__.py), [AuthoringExampleResult](src/cruxible_client/contracts/__init__.py), [AuthoringIntentListRecord](src/cruxible_client/contracts/__init__.py), [AuthoringPreflightResult](src/cruxible_client/contracts/__init__.py), [AuthoringSubmitResultRecord](src/cruxible_client/contracts/__init__.py), [BlockDeclareResult](src/cruxible_client/contracts/__init__.py), [BlockDepublishResult](src/cruxible_client/contracts/__init__.py), [QueryDefinitionView](src/cruxible_client/contracts/__init__.py), [QueryRun](src/cruxible_client/contracts/__init__.py), [ProcedureReadiness](src/cruxible_client/contracts/__init__.py), [PolicyInForce](src/cruxible_client/contracts/__init__.py), [PolicyInForceList](src/cruxible_client/contracts/__init__.py), [ProcedureBindResult](src/cruxible_client/contracts/__init__.py), [ProcedureRunState](src/cruxible_client/contracts/__init__.py), [NextResult](src/cruxible_client/contracts/__init__.py), [CurationListResult](src/cruxible_client/contracts/__init__.py), [CurationActionResult](src/cruxible_client/contracts/__init__.py), [AuditFactors](src/cruxible_client/contracts/__init__.py), [AuditEvidenceRef](src/cruxible_client/contracts/__init__.py), [AuditRow](src/cruxible_client/contracts/__init__.py), [AuditScope](src/cruxible_client/contracts/__init__.py), [AuditCoveredClaim](src/cruxible_client/contracts/__init__.py), [AuditCoverage](src/cruxible_client/contracts/__init__.py), [AuditCursor](src/cruxible_client/contracts/__init__.py), [AuditResult](src/cruxible_client/contracts/__init__.py), [SinceCursor](src/cruxible_client/contracts/__init__.py), [SinceRequest](src/cruxible_client/contracts/__init__.py), [SinceRow](src/cruxible_client/contracts/__init__.py), [SinceResult](src/cruxible_client/contracts/__init__.py), [ProviderInterfaceImplementation](src/cruxible_client/contracts/__init__.py), [ProviderInterfaceEntry](src/cruxible_client/contracts/__init__.py), [CoverageResult](src/cruxible_client/contracts/__init__.py), [FloorFile](src/cruxible_client/contracts/__init__.py), [FloorExport](src/cruxible_client/contracts/__init__.py), [WorkspaceFloorWriteResult](src/cruxible_client/contracts/__init__.py), [WorkspaceAttachResult](src/cruxible_client/contracts/__init__.py), [WorkspaceDetachResult](src/cruxible_client/contracts/__init__.py), [WorkspaceFloorStatus](src/cruxible_client/contracts/__init__.py).

**`contracts.accepted_attestations`** — [AcceptedAttestationVerdictStatement](src/cruxible_client/contracts/accepted_attestations.py), [AcceptedClaimAttestationEvidence](src/cruxible_client/contracts/accepted_attestations.py).

**`contracts.acquisition_policies`** — [SourceAcquisitionPolicyError](src/cruxible_client/contracts/acquisition_policies.py), [InputAcquisitionRule](src/cruxible_client/contracts/acquisition_policies.py), [IndependentCoherence](src/cruxible_client/contracts/acquisition_policies.py), [BoundedWindowCoherence](src/cruxible_client/contracts/acquisition_policies.py), [DeclaredSnapshotGroupCoherence](src/cruxible_client/contracts/acquisition_policies.py), [SourceAcquisitionPolicy](src/cruxible_client/contracts/acquisition_policies.py), [AcceptedSourceAcquisitionPolicy](src/cruxible_client/contracts/acquisition_policies.py), [SourceAcquisitionPolicyLawResult](src/cruxible_client/contracts/acquisition_policies.py), [AcquisitionCandidate](src/cruxible_client/contracts/acquisition_policies.py), [AcquisitionInputDecision](src/cruxible_client/contracts/acquisition_policies.py), [SourceSelectionReceipt](src/cruxible_client/contracts/acquisition_policies.py).

**`contracts.approval_policy`** — [ApprovalPolicyFormatError](src/cruxible_client/contracts/approval_policy.py), [ApprovalPolicy](src/cruxible_client/contracts/approval_policy.py).

**`contracts.artifacts`** — [ArtifactIdentity](src/cruxible_client/contracts/artifacts.py), [ArtifactPin](src/cruxible_client/contracts/artifacts.py), [ArtifactLifecycle](src/cruxible_client/contracts/artifacts.py), [GovernedArtifactProtocol](src/cruxible_client/contracts/artifacts.py), [ArtifactPathKind](src/cruxible_client/contracts/artifacts.py), [ArtifactKindRegistry](src/cruxible_client/contracts/artifacts.py).

**`contracts.attestations`** — [ApprovalStatement](src/cruxible_client/contracts/attestations.py), [ApprovalAttestation](src/cruxible_client/contracts/attestations.py), [ApprovalSubmission](src/cruxible_client/contracts/attestations.py), [VerifiedApproval](src/cruxible_client/contracts/attestations.py).

**`contracts.authoring.inputs`** — [LiteralObjectInput](src/cruxible_client/contracts/authoring/inputs.py), [SubjectObjectInput](src/cruxible_client/contracts/authoring/inputs.py), [ExactContentObjectInput](src/cruxible_client/contracts/authoring/inputs.py), [SelfSourceInput](src/cruxible_client/contracts/authoring/inputs.py), [WorkingSelectionInput](src/cruxible_client/contracts/authoring/inputs.py), [ExistingCaptureInput](src/cruxible_client/contracts/authoring/inputs.py), [AcceptedReferenceInput](src/cruxible_client/contracts/authoring/inputs.py), [SlotReferenceInput](src/cruxible_client/contracts/authoring/inputs.py), [CarriedContractReferenceInput](src/cruxible_client/contracts/authoring/inputs.py), [CarriedContractInput](src/cruxible_client/contracts/authoring/inputs.py), [ClaimDispositionInput](src/cruxible_client/contracts/authoring/inputs.py), [ClaimInput](src/cruxible_client/contracts/authoring/inputs.py), [ProcedureInput](src/cruxible_client/contracts/authoring/inputs.py), [SubjectInput](src/cruxible_client/contracts/authoring/inputs.py), [QueryDefinitionInput](src/cruxible_client/contracts/authoring/inputs.py), [ApprovalPolicyInput](src/cruxible_client/contracts/authoring/inputs.py), [ProcedureRuntimePolicyInput](src/cruxible_client/contracts/authoring/inputs.py), [ClaimTypeInput](src/cruxible_client/contracts/authoring/inputs.py), [ClaimTypeSuccessionInput](src/cruxible_client/contracts/authoring/inputs.py), [ClaimRetirementInput](src/cruxible_client/contracts/authoring/inputs.py), [ProcedureMandateInput](src/cruxible_client/contracts/authoring/inputs.py), [AcquisitionPolicyInput](src/cruxible_client/contracts/authoring/inputs.py), [LineInput](src/cruxible_client/contracts/authoring/inputs.py), [TriggerInput](src/cruxible_client/contracts/authoring/inputs.py), [ChangeSetInput](src/cruxible_client/contracts/authoring/inputs.py), [AuthoringInputError](src/cruxible_client/contracts/authoring/inputs.py).

**`contracts.authoring.models`** — [AuthoringReferenceExpectation](src/cruxible_client/contracts/authoring/models.py), [AuthoringReferenceSuccessor](src/cruxible_client/contracts/authoring/models.py), [AuthoringProgramOperation](src/cruxible_client/contracts/authoring/models.py), [AuthoringProgramStamp](src/cruxible_client/contracts/authoring/models.py), [AuthoringExactContentObject](src/cruxible_client/contracts/authoring/models.py), [AuthoringClaimStatement](src/cruxible_client/contracts/authoring/models.py), [AuthoringExistingClaimDisposition](src/cruxible_client/contracts/authoring/models.py), [WorkingGitBlobCoordinate](src/cruxible_client/contracts/authoring/models.py), [WorkingDigestCoordinate](src/cruxible_client/contracts/authoring/models.py), [WorkingAnchorWindow](src/cruxible_client/contracts/authoring/models.py), [WorkingSelectionObservation](src/cruxible_client/contracts/authoring/models.py), [InsertionAnchorWindow](src/cruxible_client/contracts/authoring/models.py), [InsertionTarget](src/cruxible_client/contracts/authoring/models.py), [PublicationSourceObservation](src/cruxible_client/contracts/authoring/models.py), [SelfSourceBody](src/cruxible_client/contracts/authoring/models.py), [ExistingCaptureCitationSource](src/cruxible_client/contracts/authoring/models.py), [ClaimAuthoringPayloadV1](src/cruxible_client/contracts/authoring/models.py), [ClaimDependencyDrafts](src/cruxible_client/contracts/authoring/models.py), [ClaimAuthoringPayloadV2](src/cruxible_client/contracts/authoring/models.py), [ClaimAuthoringPayload](src/cruxible_client/contracts/authoring/models.py), [AuthoringArtifactReference](src/cruxible_client/contracts/authoring/models.py), [AuthoringCandidateReference](src/cruxible_client/contracts/authoring/models.py), [ResolutionContractAuthoringPayload](src/cruxible_client/contracts/authoring/models.py), [AttestationAuthoringPayload](src/cruxible_client/contracts/authoring/models.py), [SubjectAuthoringPayload](src/cruxible_client/contracts/authoring/models.py), [QueryDefinitionAuthoringPayload](src/cruxible_client/contracts/authoring/models.py), [ApprovalPolicyAuthoringPayload](src/cruxible_client/contracts/authoring/models.py), [ProcedureRuntimePolicyAuthoringPayload](src/cruxible_client/contracts/authoring/models.py), [ProcedureMandateAuthoringPayload](src/cruxible_client/contracts/authoring/models.py), [CaptureContractAuthoringPayload](src/cruxible_client/contracts/authoring/models.py), [SourceAcquisitionPolicyAuthoringPayload](src/cruxible_client/contracts/authoring/models.py), [LineAuthoringPayload](src/cruxible_client/contracts/authoring/models.py), [ProcedureAuthoringPayloadV1](src/cruxible_client/contracts/authoring/models.py), [ProcedureAuthoringPayload](src/cruxible_client/contracts/authoring/models.py), [ClaimTypeAuthoringPayload](src/cruxible_client/contracts/authoring/models.py), [ClaimTypeSuccessionDependent](src/cruxible_client/contracts/authoring/models.py), [ClaimTypeSuccessionMember](src/cruxible_client/contracts/authoring/models.py), [ClaimRetirementMember](src/cruxible_client/contracts/authoring/models.py), [ChangeSetAuthoringPayload](src/cruxible_client/contracts/authoring/models.py), [RepairAlternative](src/cruxible_client/contracts/authoring/models.py), [AuthoringDiagnostic](src/cruxible_client/contracts/authoring/models.py), [BlockedCheck](src/cruxible_client/contracts/authoring/models.py), [DiagnosticFrontierLimits](src/cruxible_client/contracts/authoring/models.py), [DiagnosticFrontier](src/cruxible_client/contracts/authoring/models.py), [AcceptanceCondition](src/cruxible_client/contracts/authoring/models.py), [CandidateStatus](src/cruxible_client/contracts/authoring/models.py), [PublicationPreparation](src/cruxible_client/contracts/authoring/models.py), [InsertionTerminalTombstone](src/cruxible_client/contracts/authoring/models.py), [InsertionExpectation](src/cruxible_client/contracts/authoring/models.py), [PreflightCertificate](src/cruxible_client/contracts/authoring/models.py), [PreflightResult](src/cruxible_client/contracts/authoring/models.py), [ChangeSetClaimIdentity](src/cruxible_client/contracts/authoring/models.py), [AuthoringIntentV1](src/cruxible_client/contracts/authoring/models.py), [AuthoringIntent](src/cruxible_client/contracts/authoring/models.py), [AuthoringIntentView](src/cruxible_client/contracts/authoring/models.py), [AuthoringIntentList](src/cruxible_client/contracts/authoring/models.py), [AuthoringIntentCompileRequestV1](src/cruxible_client/contracts/authoring/models.py), [AuthoringIntentCompileRequestV2](src/cruxible_client/contracts/authoring/models.py), [AuthoringIntentCompileRequest](src/cruxible_client/contracts/authoring/models.py), [AuthoringIntentPreflightRequest](src/cruxible_client/contracts/authoring/models.py), [AuthoringIntentSubmitRequest](src/cruxible_client/contracts/authoring/models.py), [AuthoringSubmitMember](src/cruxible_client/contracts/authoring/models.py), [AuthoringSubmitResult](src/cruxible_client/contracts/authoring/models.py), [BlockSyncSuccessorCandidate](src/cruxible_client/contracts/authoring/models.py), [BlockSyncReadRequest](src/cruxible_client/contracts/authoring/models.py), [ProjectionDependencyIssue](src/cruxible_client/contracts/authoring/models.py), [BlockSyncReadResult](src/cruxible_client/contracts/authoring/models.py), [ProjectionCheckRequest](src/cruxible_client/contracts/authoring/models.py), [ProjectionCheckResult](src/cruxible_client/contracts/authoring/models.py), [BlockSyncItem](src/cruxible_client/contracts/authoring/models.py), [BlockSyncResult](src/cruxible_client/contracts/authoring/models.py).

**`contracts.authoring_profiles`** — [AuthoringProfileError](src/cruxible_client/contracts/authoring_profiles.py), [ClaimTypeProfileDefinition](src/cruxible_client/contracts/authoring_profiles.py), [ClaimTypeProfileInput](src/cruxible_client/contracts/authoring_profiles.py), [ClaimTypeExpansionEvidence](src/cruxible_client/contracts/authoring_profiles.py), [ClaimTypeExpansionResult](src/cruxible_client/contracts/authoring_profiles.py).

**`contracts.candidates`** — [SemanticCandidateV1](src/cruxible_client/contracts/candidates.py), [SemanticCandidate](src/cruxible_client/contracts/candidates.py), [CandidateRecordV1](src/cruxible_client/contracts/candidates.py), [CandidateMemberEvidence](src/cruxible_client/contracts/candidates.py), [DependencyProofReference](src/cruxible_client/contracts/candidates.py), [LawEvaluationCoordinate](src/cruxible_client/contracts/candidates.py), [MemberLawEvaluation](src/cruxible_client/contracts/candidates.py), [CandidateMemberLawEvidence](src/cruxible_client/contracts/candidates.py), [ClosureProofV2](src/cruxible_client/contracts/candidates.py), [ClosureProof](src/cruxible_client/contracts/candidates.py), [CandidateRecordV2](src/cruxible_client/contracts/candidates.py), [CandidateRecord](src/cruxible_client/contracts/candidates.py).

**`contracts.canonical`** — [ArtifactCodec](src/cruxible_client/contracts/canonical.py), [Sha256Value](src/cruxible_client/contracts/canonical.py), [BootstrapRoot](src/cruxible_client/contracts/canonical.py), [ArtifactDigest](src/cruxible_client/contracts/canonical.py), [CandidateDigest](src/cruxible_client/contracts/canonical.py), [AcceptanceLawDigest](src/cruxible_client/contracts/canonical.py), [ApprovalDigest](src/cruxible_client/contracts/canonical.py), [ProposalDigest](src/cruxible_client/contracts/canonical.py), [SemanticDiffDigest](src/cruxible_client/contracts/canonical.py), [ChangeSetDigest](src/cruxible_client/contracts/canonical.py), [SemanticManifestRoot](src/cruxible_client/contracts/canonical.py), [MerkleNodeDigest](src/cruxible_client/contracts/canonical.py), [SemanticMerkleRoot](src/cruxible_client/contracts/canonical.py), [DependencyEdgeRoot](src/cruxible_client/contracts/canonical.py), [SemanticRoot](src/cruxible_client/contracts/canonical.py), [GenerationRoot](src/cruxible_client/contracts/canonical.py), [LogicalDigest](src/cruxible_client/contracts/canonical.py), [CasDigest](src/cruxible_client/contracts/canonical.py).

**`contracts.capture_journal`** — [CaptureJournalError](src/cruxible_client/contracts/capture_journal.py), [CaptureLandingEventV1](src/cruxible_client/contracts/capture_journal.py), [CaptureLandingEvent](src/cruxible_client/contracts/capture_journal.py), [CaptureCursor](src/cruxible_client/contracts/capture_journal.py), [CaptureLandingJournalProtocol](src/cruxible_client/contracts/capture_journal.py), [InMemoryCaptureLandingJournal](src/cruxible_client/contracts/capture_journal.py).

**`contracts.capture_reads`** — [CaptureReadRequest](src/cruxible_client/contracts/capture_reads.py), [CaptureRead](src/cruxible_client/contracts/capture_reads.py).

**`contracts.captures`** — [CaptureFormatError](src/cruxible_client/contracts/captures.py), [CanonicalDuration](src/cruxible_client/contracts/captures.py), [CaptureSelectionBudget](src/cruxible_client/contracts/captures.py), [CaptureRetentionErasurePolicy](src/cruxible_client/contracts/captures.py), [CaptureContract](src/cruxible_client/contracts/captures.py), [CaptureComponentRegistry](src/cruxible_client/contracts/captures.py), [AcceptedCaptureContract](src/cruxible_client/contracts/captures.py), [CaptureContractLawResult](src/cruxible_client/contracts/captures.py), [CaptureRunCoordinateV1](src/cruxible_client/contracts/captures.py), [CaptureRunCoordinate](src/cruxible_client/contracts/captures.py), [SourceEffectiveTime](src/cruxible_client/contracts/captures.py), [CaptureEnvelopeV1](src/cruxible_client/contracts/captures.py), [ProviderInvocationCaptureEvidence](src/cruxible_client/contracts/captures.py), [ProcedureEgressCaptureEvidence](src/cruxible_client/contracts/captures.py), [ProcedureProducerReceiptProtocol](src/cruxible_client/contracts/captures.py), [ProducerReceiptResolverProtocol](src/cruxible_client/contracts/captures.py), [ProviderProducerReceiptResolution](src/cruxible_client/contracts/captures.py), [ProviderResultToExternalCapture](src/cruxible_client/contracts/captures.py), [CaptureEnvelope](src/cruxible_client/contracts/captures.py), [InputReceiptSetManifest](src/cruxible_client/contracts/captures.py), [DirectClaimSource](src/cruxible_client/contracts/captures.py), [DirectByteSpanSelection](src/cruxible_client/contracts/captures.py), [DirectExternalSelection](src/cruxible_client/contracts/captures.py), [DirectForeignSourceSelection](src/cruxible_client/contracts/captures.py), [CaptureObjectStoreProtocol](src/cruxible_client/contracts/captures.py), [LedgerMaterialResolverProtocol](src/cruxible_client/contracts/captures.py), [DirectCaptureBuildResult](src/cruxible_client/contracts/captures.py), [CaptureBuildResult](src/cruxible_client/contracts/captures.py).

**`contracts.cas_contracts`** — [BodyAccessContext](src/cruxible_client/contracts/cas_contracts.py), [CasObjectMetadata](src/cruxible_client/contracts/cas_contracts.py), [BodyProjectionProtocol](src/cruxible_client/contracts/cas_contracts.py).

**`contracts.claim_attestation_store`** — [ClaimAttestationStoreManifest](src/cruxible_client/contracts/claim_attestation_store.py), [ClaimAttestationEventPayload](src/cruxible_client/contracts/claim_attestation_store.py), [ClaimAttestationEvent](src/cruxible_client/contracts/claim_attestation_store.py), [ClaimAttestationPartitionGenesis](src/cruxible_client/contracts/claim_attestation_store.py), [ClaimAttestationPartitionHead](src/cruxible_client/contracts/claim_attestation_store.py), [ClaimAttestationHeadMapEntry](src/cruxible_client/contracts/claim_attestation_store.py), [ClaimAttestationHeadMapNode](src/cruxible_client/contracts/claim_attestation_store.py), [ClaimAttestationPublishedRoot](src/cruxible_client/contracts/claim_attestation_store.py), [ClaimAttestationPublishedPointer](src/cruxible_client/contracts/claim_attestation_store.py), [ClaimAttestationOutstandingMembership](src/cruxible_client/contracts/claim_attestation_store.py), [ClaimAttestationAccelerator](src/cruxible_client/contracts/claim_attestation_store.py).

**`contracts.claim_attestations`** — [ClaimAttestationError](src/cruxible_client/contracts/claim_attestations.py), [ClaimAttestationStatementV1](src/cruxible_client/contracts/claim_attestations.py), [ClaimAttestationV1](src/cruxible_client/contracts/claim_attestations.py), [VerifiedClaimAttestationV1](src/cruxible_client/contracts/claim_attestations.py), [ClaimAttestationStatement](src/cruxible_client/contracts/claim_attestations.py), [ClaimAttestation](src/cruxible_client/contracts/claim_attestations.py), [ClaimAttestationCaptureReference](src/cruxible_client/contracts/claim_attestations.py), [PreparedClaimAttestationRequest](src/cruxible_client/contracts/claim_attestations.py), [ClaimAttestationResolvedArtifact](src/cruxible_client/contracts/claim_attestations.py), [VerifiedClaimAttestation](src/cruxible_client/contracts/claim_attestations.py), [ClaimAttestationAppendRequest](src/cruxible_client/contracts/claim_attestations.py), [ClaimAttestationAppendResult](src/cruxible_client/contracts/claim_attestations.py).

**`contracts.claim_reads`** — [ClaimReadBatchRequest](src/cruxible_client/contracts/claim_reads.py), [ClaimReadBatchResult](src/cruxible_client/contracts/claim_reads.py), [ClaimBackingsRequest](src/cruxible_client/contracts/claim_reads.py), [ClaimBackingsResult](src/cruxible_client/contracts/claim_reads.py).

**`contracts.claim_type_structure`** — [ClaimTypeStructure](src/cruxible_client/contracts/claim_type_structure.py), [ClaimTypeStructuralCheck](src/cruxible_client/contracts/claim_type_structure.py).

**`contracts.claim_types`** — [ClaimTypeFormatError](src/cruxible_client/contracts/claim_types.py), [ClaimTypeFreshnessHorizonInvalid](src/cruxible_client/contracts/claim_types.py), [ClaimFreshnessDuration](src/cruxible_client/contracts/claim_types.py), [ClaimEvidenceFreshness](src/cruxible_client/contracts/claim_types.py), [ClaimAttestationConsequenceRule](src/cruxible_client/contracts/claim_types.py), [ClaimAttestationConsequencePolicy](src/cruxible_client/contracts/claim_types.py), [ClaimType](src/cruxible_client/contracts/claim_types.py), [AcceptedClaimType](src/cruxible_client/contracts/claim_types.py), [ClaimTypeLawResult](src/cruxible_client/contracts/claim_types.py).

**`contracts.claim_verdicts`** — [ClaimAdjudicationRuleV1](src/cruxible_client/contracts/claim_verdicts.py), [CaptureVerdictEvidence](src/cruxible_client/contracts/claim_verdicts.py), [EvidenceControlComponent](src/cruxible_client/contracts/claim_verdicts.py), [ClaimVerdictResultV1](src/cruxible_client/contracts/claim_verdicts.py), [EvidenceFreshnessExpiration](src/cruxible_client/contracts/claim_verdicts.py), [ClaimVerdictResult](src/cruxible_client/contracts/claim_verdicts.py).

**`contracts.claims`** — [ClaimFormatError](src/cruxible_client/contracts/claims.py), [ClaimUnsupportedFormatError](src/cruxible_client/contracts/claims.py), [LiteralClaimObject](src/cruxible_client/contracts/claims.py), [SubjectClaimObject](src/cruxible_client/contracts/claims.py), [ExactContentClaimObject](src/cruxible_client/contracts/claims.py), [ClaimStatement](src/cruxible_client/contracts/claims.py), [ClaimStatementCard](src/cruxible_client/contracts/claims.py), [ClaimReferentContext](src/cruxible_client/contracts/claims.py), [ClaimBackingV1](src/cruxible_client/contracts/claims.py), [ClaimCitation](src/cruxible_client/contracts/claims.py), [LegacyCitationReference](src/cruxible_client/contracts/claims.py), [ClaimBacking](src/cruxible_client/contracts/claims.py), [ClaimArtifactV2](src/cruxible_client/contracts/claims.py), [ClaimRetirementAttribution](src/cruxible_client/contracts/claims.py), [ClaimRetireDependent](src/cruxible_client/contracts/claims.py), [ClaimArtifact](src/cruxible_client/contracts/claims.py), [AcceptedClaim](src/cruxible_client/contracts/claims.py), [ClaimLawEvidenceV1](src/cruxible_client/contracts/claims.py), [ClaimLawEvidence](src/cruxible_client/contracts/claims.py), [ClaimLawResult](src/cruxible_client/contracts/claims.py), [CitedSourceWindow](src/cruxible_client/contracts/claims.py), [CaptureEvidenceKindEvaluation](src/cruxible_client/contracts/claims.py).

**`contracts.compiler_upgrade`** — [CompilerUpgrade](src/cruxible_client/contracts/compiler_upgrade.py).

**`contracts.cron`** — [CRON_UTC_HINT](src/cruxible_client/contracts/cron.py), [CronExpressionError](src/cruxible_client/contracts/cron.py), [CronSpec](src/cruxible_client/contracts/cron.py), [parse_cron](src/cruxible_client/contracts/cron.py). Standard five-field cron, always evaluated in UTC.

**`contracts.declared_blocks`** — [PresentationPolicyV1](src/cruxible_client/contracts/declared_blocks.py), [ProjectionAdvisoryPolicy](src/cruxible_client/contracts/declared_blocks.py), [PresentationPolicy](src/cruxible_client/contracts/declared_blocks.py), [ProjectionCoverageBinding](src/cruxible_client/contracts/declared_blocks.py), [ProjectionCoverageObservation](src/cruxible_client/contracts/declared_blocks.py), [ReviewWorkspaceObservation](src/cruxible_client/contracts/declared_blocks.py), [ProjectionClaimBacking](src/cruxible_client/contracts/declared_blocks.py), [ProjectionArtifactBacking](src/cruxible_client/contracts/declared_blocks.py), [ProjectionResolvedParameterBinding](src/cruxible_client/contracts/declared_blocks.py), [ProjectionQueryBacking](src/cruxible_client/contracts/declared_blocks.py), [ProjectionBlockStampV1](src/cruxible_client/contracts/declared_blocks.py), [ProjectionBlockStamp](src/cruxible_client/contracts/declared_blocks.py), [ProjectionMarkerSummary](src/cruxible_client/contracts/declared_blocks.py), [ProjectionMarkerError](src/cruxible_client/contracts/declared_blocks.py), [ProjectionProcessingPolicy](src/cruxible_client/contracts/declared_blocks.py), [ProjectionProcessingLimitExceeded](src/cruxible_client/contracts/declared_blocks.py), [ProjectionBootstrapUnstampedError](src/cruxible_client/contracts/declared_blocks.py), [ParsedProjectionBlock](src/cruxible_client/contracts/declared_blocks.py), [ProjectionWindow](src/cruxible_client/contracts/declared_blocks.py).

**`contracts.diagnostics`** — [LocalDraftEdit](src/cruxible_client/contracts/diagnostics.py), [GovernedOperationReference](src/cruxible_client/contracts/diagnostics.py), [CompilerDiagnostic](src/cruxible_client/contracts/diagnostics.py).

**`contracts.discovery`** — [DiscoveryBudget](src/cruxible_client/contracts/discovery.py), [DiscoveryMatchBasis](src/cruxible_client/contracts/discovery.py), [DiscoveryHit](src/cruxible_client/contracts/discovery.py), [DiscoveryRequest](src/cruxible_client/contracts/discovery.py), [DiscoveryPage](src/cruxible_client/contracts/discovery.py).

**`contracts.documents`** — [DocumentLink](src/cruxible_client/contracts/documents.py), [DocumentPin](src/cruxible_client/contracts/documents.py), [DocumentAuthority](src/cruxible_client/contracts/documents.py), [DocumentLifecycle](src/cruxible_client/contracts/documents.py), [DocumentShell](src/cruxible_client/contracts/documents.py), [DocumentArtifactAdapter](src/cruxible_client/contracts/documents.py), [BodyVerifierProtocol](src/cruxible_client/contracts/documents.py), [AcceptedDocument](src/cruxible_client/contracts/documents.py), [DocumentLawResult](src/cruxible_client/contracts/documents.py).

**`contracts.errors`** — [CruxibleError](src/cruxible_client/contracts/errors.py), [CanonicalEncodingError](src/cruxible_client/contracts/errors.py), [MerkleIntegrityError](src/cruxible_client/contracts/errors.py), [FormatError](src/cruxible_client/contracts/errors.py), [SinceRequestInvalid](src/cruxible_client/contracts/errors.py), [ClaimAttestationRequestInvalid](src/cruxible_client/contracts/errors.py), [InstanceIncompatiblePrereleaseContent](src/cruxible_client/contracts/errors.py), [ReseedRequired](src/cruxible_client/contracts/errors.py), [InstanceDecommissioned](src/cruxible_client/contracts/errors.py), [SemanticDeltaLimitError](src/cruxible_client/contracts/errors.py), [BootstrapError](src/cruxible_client/contracts/errors.py), [ObjectFormatConflict](src/cruxible_client/contracts/errors.py), [GitError](src/cruxible_client/contracts/errors.py), [SigningKeyError](src/cruxible_client/contracts/errors.py), [CasError](src/cruxible_client/contracts/errors.py), [JournalError](src/cruxible_client/contracts/errors.py), [JournalConflictError](src/cruxible_client/contracts/errors.py), [JournalIntegrityError](src/cruxible_client/contracts/errors.py), [ExecutionError](src/cruxible_client/contracts/errors.py), [DocumentFormatError](src/cruxible_client/contracts/errors.py), [DocumentNotFoundError](src/cruxible_client/contracts/errors.py), [SubjectFormatError](src/cruxible_client/contracts/errors.py), [SubjectNotFoundError](src/cruxible_client/contracts/errors.py), [ClaimNotFoundError](src/cruxible_client/contracts/errors.py), [ProposalAdmissionError](src/cruxible_client/contracts/errors.py), [ProposalWithdrawnError](src/cruxible_client/contracts/errors.py), [ProposalNotFoundError](src/cruxible_client/contracts/errors.py), [ProposalSelectorAmbiguousError](src/cruxible_client/contracts/errors.py), [ProposalContentUnavailable](src/cruxible_client/contracts/errors.py), [ProposalReadmitRequiresResubmission](src/cruxible_client/contracts/errors.py), [ProposalActivationRequestInvalid](src/cruxible_client/contracts/errors.py), [ProposalIntegrityError](src/cruxible_client/contracts/errors.py), [ProposalEvaluationIntegrityError](src/cruxible_client/contracts/errors.py), [ApprovalIntegrityError](src/cruxible_client/contracts/errors.py), [PrincipalIntegrityError](src/cruxible_client/contracts/errors.py), [SettlementIntegrityError](src/cruxible_client/contracts/errors.py), [ReplayCheckpointError](src/cruxible_client/contracts/errors.py), [ProjectionError](src/cruxible_client/contracts/errors.py), [ProjectionCoordinateError](src/cruxible_client/contracts/errors.py), [ProjectionFormatError](src/cruxible_client/contracts/errors.py), [ProjectionPublicationError](src/cruxible_client/contracts/errors.py), [ProjectionIntegrityError](src/cruxible_client/contracts/errors.py).

**`contracts.governance`** — [ApprovalRequirement](src/cruxible_client/contracts/governance.py), [AcceptanceLawCoordinate](src/cruxible_client/contracts/governance.py).

**`contracts.laws`** — [InstalledAcceptanceLaw](src/cruxible_client/contracts/laws.py), [AcceptanceLawRegistry](src/cruxible_client/contracts/laws.py).

**`contracts.ledger_mirror`** — [LedgerMirrorUrlInvalid](src/cruxible_client/contracts/ledger_mirror.py), [LedgerMirrorUnset](src/cruxible_client/contracts/ledger_mirror.py).

**`contracts.merkle`** — [MerkleDomainFamily](src/cruxible_client/contracts/merkle.py), [MerkleNode](src/cruxible_client/contracts/merkle.py), [MerkleTree](src/cruxible_client/contracts/merkle.py).

**`contracts.persistent`** — [PersistentMap](src/cruxible_client/contracts/persistent.py), [MapMutation](src/cruxible_client/contracts/persistent.py).

**`contracts.policies`** — [CorroborationRequirement](src/cruxible_client/contracts/policies.py), [FreezeRequirement](src/cruxible_client/contracts/policies.py), [ClaimAdmissionPolicy](src/cruxible_client/contracts/policies.py), [ClaimResolutionPolicy](src/cruxible_client/contracts/policies.py), [ClaimEvidenceAdmissionRuleV1](src/cruxible_client/contracts/policies.py), [ClaimEvidenceAdmissionPolicyV1](src/cruxible_client/contracts/policies.py), [ClaimCorroborationResult](src/cruxible_client/contracts/policies.py), [ClaimAdmissionEvaluationAccount](src/cruxible_client/contracts/policies.py), [ClaimAdmissionCandidateContext](src/cruxible_client/contracts/policies.py), [ClaimAdmissionCandidateResult](src/cruxible_client/contracts/policies.py), [EvidenceAdmissionInput](src/cruxible_client/contracts/policies.py), [ClaimEvidenceAdmissionResult](src/cruxible_client/contracts/policies.py), [ClaimEvidenceAdmissionTrace](src/cruxible_client/contracts/policies.py), [ResolutionContender](src/cruxible_client/contracts/policies.py), [ClaimResolutionResult](src/cruxible_client/contracts/policies.py).

**`contracts.predictions`** — [PredictRequest](src/cruxible_client/contracts/predictions.py), [PredictResult](src/cruxible_client/contracts/predictions.py), [ObservationSettlementEvidence](src/cruxible_client/contracts/predictions.py), [TerminalSettlementEvidence](src/cruxible_client/contracts/predictions.py), [SettleRequest](src/cruxible_client/contracts/predictions.py), [SettleResult](src/cruxible_client/contracts/predictions.py).

**`contracts.principals`** — [PrincipalRegistrySnapshot](src/cruxible_client/contracts/principals.py).

**`contracts.procedure_mandates`** — [ProcedureMandateError](src/cruxible_client/contracts/procedure_mandates.py), [ProcedureMandateV1](src/cruxible_client/contracts/procedure_mandates.py), [AcceptedProcedureMandate](src/cruxible_client/contracts/procedure_mandates.py), [ProcedureMandateLawResult](src/cruxible_client/contracts/procedure_mandates.py), [ProcedureMandateInvocation](src/cruxible_client/contracts/procedure_mandates.py), [ProcedureMandateEvaluation](src/cruxible_client/contracts/procedure_mandates.py).

**`contracts.procedure_runtime_policy`** — [ProcedureRuntimePolicyFormatError](src/cruxible_client/contracts/procedure_runtime_policy.py), [ProcedureRuntimePolicy](src/cruxible_client/contracts/procedure_runtime_policy.py).

**`contracts.procedures.artifacts`** — [ProcedureFormatError](src/cruxible_client/contracts/procedures/artifacts.py), [ProcedureArtifactV1](src/cruxible_client/contracts/procedures/artifacts.py), [ProcedureOwnedContract](src/cruxible_client/contracts/procedures/artifacts.py), [ProcedureArtifact](src/cruxible_client/contracts/procedures/artifacts.py), [AcceptedProcedure](src/cruxible_client/contracts/procedures/artifacts.py), [ProcedureLawResult](src/cruxible_client/contracts/procedures/artifacts.py).

**`contracts.procedures.authoring`** — [GuardBuilderCommon](src/cruxible_client/contracts/procedures/authoring.py), [AcceptedClaimGuardBuilder](src/cruxible_client/contracts/procedures/authoring.py), [SourceCaptureGuardBuilder](src/cruxible_client/contracts/procedures/authoring.py), [ExhaustGuardBuilder](src/cruxible_client/contracts/procedures/authoring.py), [BuilderSourceMapping](src/cruxible_client/contracts/procedures/authoring.py), [ProcedureGuardExpansion](src/cruxible_client/contracts/procedures/authoring.py).

**`contracts.procedures.closure`** — [ProcedurePinClosureError](src/cruxible_client/contracts/procedures/closure.py), [LineSlotBinding](src/cruxible_client/contracts/procedures/closure.py), [ProviderExtrasEnvironmentPinMap](src/cruxible_client/contracts/procedures/closure.py), [ProviderImplementationClosure](src/cruxible_client/contracts/procedures/closure.py), [ProcedureSlotInterface](src/cruxible_client/contracts/procedures/closure.py), [ClosedProcedurePins](src/cruxible_client/contracts/procedures/closure.py).

**`contracts.procedures.contract_schema`** — [PropertySchema](src/cruxible_client/contracts/procedures/contract_schema.py), [ContractSchema](src/cruxible_client/contracts/procedures/contract_schema.py).

**`contracts.procedures.contracts`** — [ProcedureContractValidationError](src/cruxible_client/contracts/procedures/contracts.py), [ProcedureContractItemBudgetExceeded](src/cruxible_client/contracts/procedures/contracts.py), [ProcedureContractListObservation](src/cruxible_client/contracts/procedures/contracts.py), [ValidatedProcedureContract](src/cruxible_client/contracts/procedures/contracts.py), [OwnedProcedureContractValidator](src/cruxible_client/contracts/procedures/contracts.py).

**`contracts.procedures.graph`** — [ProcedureGraphFormatError](src/cruxible_client/contracts/procedures/graph.py), [ProcedureGraphV3](src/cruxible_client/contracts/procedures/graph.py), [ProcedureNodeDigestsV3](src/cruxible_client/contracts/procedures/graph.py).

**`contracts.procedures.line_specs`** — [LineSpecFormatError](src/cruxible_client/contracts/procedures/line_specs.py), [LineSpec](src/cruxible_client/contracts/procedures/line_specs.py), [AcceptedLineSpec](src/cruxible_client/contracts/procedures/line_specs.py), [LineSpecLawResult](src/cruxible_client/contracts/procedures/line_specs.py). LineSpecV1-V5 and their embedded trigger policies (`CadenceTriggerPolicy`, `CaptureLandingTriggerPolicyV1/V2`, `WindowCloseTriggerPolicyV1/V2`, `ManualTriggerPolicy`) parse retained history only; a live Line is v6 and Triggers aim at it.

**`contracts.procedures.measurements`** — [ProcedureMeasurementExpectation](src/cruxible_client/contracts/procedures/measurements.py), [AcceptedQueryProcedureMeasurement](src/cruxible_client/contracts/procedures/measurements.py), [ClaimAttestationProcedureMeasurement](src/cruxible_client/contracts/procedures/measurements.py), [ClaimStatementProcedureMeasurement](src/cruxible_client/contracts/procedures/measurements.py), [ProcedureMeasurementSituationShape](src/cruxible_client/contracts/procedures/measurements.py), [ProcedureMeasurementReviewTrigger](src/cruxible_client/contracts/procedures/measurements.py), [ProcedureMeasurementDeclaration](src/cruxible_client/contracts/procedures/measurements.py).

**`contracts.procedures.models`** — [ProcedurePinSlot](src/cruxible_client/contracts/procedures/models.py), [ProcedurePinSlotRef](src/cruxible_client/contracts/procedures/models.py), [ProcedureBudget](src/cruxible_client/contracts/procedures/models.py), [ProcedureHardCaps](src/cruxible_client/contracts/procedures/models.py), [PredicateOperand](src/cruxible_client/contracts/procedures/models.py), [GuardPredicate](src/cruxible_client/contracts/procedures/models.py), [StateTapNodeV3](src/cruxible_client/contracts/procedures/models.py), [SourceNodeV3](src/cruxible_client/contracts/procedures/models.py), [SourceNode](src/cruxible_client/contracts/procedures/models.py), [ExhaustTapNode](src/cruxible_client/contracts/procedures/models.py), [ProviderNodeV3](src/cruxible_client/contracts/procedures/models.py), [ProviderNode](src/cruxible_client/contracts/procedures/models.py), [CallNode](src/cruxible_client/contracts/procedures/models.py), [TransformAdapterSpec](src/cruxible_client/contracts/procedures/models.py), [TransformShapeItemsSpec](src/cruxible_client/contracts/procedures/models.py), [TransformFilterItemsSpec](src/cruxible_client/contracts/procedures/models.py), [TransformDedupeItemsSpec](src/cruxible_client/contracts/procedures/models.py), [TransformJoinItemsSpec](src/cruxible_client/contracts/procedures/models.py), [TransformAggregateItemsSpec](src/cruxible_client/contracts/procedures/models.py), [TransformNode](src/cruxible_client/contracts/procedures/models.py), [GuardNode](src/cruxible_client/contracts/procedures/models.py), [ProjectNode](src/cruxible_client/contracts/procedures/models.py), [RepeatBodyNodeV3](src/cruxible_client/contracts/procedures/models.py), [RepeatNodeV3](src/cruxible_client/contracts/procedures/models.py), [RepeatBodyNodeV4](src/cruxible_client/contracts/procedures/models.py), [RepeatNodeV4](src/cruxible_client/contracts/procedures/models.py), [RepeatBodyNode](src/cruxible_client/contracts/procedures/models.py), [RepeatNode](src/cruxible_client/contracts/procedures/models.py), [CaptureEgressNodeV3](src/cruxible_client/contracts/procedures/models.py), [InboxEgressNode](src/cruxible_client/contracts/procedures/models.py), [ProposeChangeSetNodeV3](src/cruxible_client/contracts/procedures/models.py), [HaltNode](src/cruxible_client/contracts/procedures/models.py), [ProcedureDefinitionV3](src/cruxible_client/contracts/procedures/models.py), [ProcedureDefinitionV4](src/cruxible_client/contracts/procedures/models.py), [ProcedureDefinitionV5](src/cruxible_client/contracts/procedures/models.py).

**`contracts.procedures.pin_expectations`** — [PinExpectation](src/cruxible_client/contracts/procedures/pin_expectations.py).

**`contracts.procedures.proposal_items`** — [ProcedureClaimProposalItemV1](src/cruxible_client/contracts/procedures/proposal_items.py).

**`contracts.procedures.readings`** — [ProcedureMeasurementEligibility](src/cruxible_client/contracts/procedures/readings.py), [ProcedureMeasurementResolutionSummary](src/cruxible_client/contracts/procedures/readings.py), [ProcedureReadingSummary](src/cruxible_client/contracts/procedures/readings.py), [ProcedureMeasurementRow](src/cruxible_client/contracts/procedures/readings.py), [ProcedureMeasureRequest](src/cruxible_client/contracts/procedures/readings.py), [ProcedureMeasureResult](src/cruxible_client/contracts/procedures/readings.py), [ProcedureMeasurementContractStatus](src/cruxible_client/contracts/procedures/readings.py), [ProcedureReadingsRequest](src/cruxible_client/contracts/procedures/readings.py), [ProcedureReadingsResult](src/cruxible_client/contracts/procedures/readings.py).

**`contracts.procedures.results`** — [ProcedureJournalCoordinate](src/cruxible_client/contracts/procedures/results.py), [ProcedureBudgetRefusalDetail](src/cruxible_client/contracts/procedures/results.py), [ProcedureAdmissionRefusal](src/cruxible_client/contracts/procedures/results.py), [ProcedureNodeRefusal](src/cruxible_client/contracts/procedures/results.py), [ProcedureOperationalFailure](src/cruxible_client/contracts/procedures/results.py), [ProcedureInternalFailure](src/cruxible_client/contracts/procedures/results.py), [ProcedureRunAttribution](src/cruxible_client/contracts/procedures/results.py), [ProcedurePendingSuccessor](src/cruxible_client/contracts/procedures/results.py), [ProcedureRunReceiptV2](src/cruxible_client/contracts/procedures/results.py), [ProcedureBudgetExceededDetail](src/cruxible_client/contracts/procedures/results.py), [ProcedureBudgetExhausted](src/cruxible_client/contracts/procedures/results.py), [ProcedureHaltTerminal](src/cruxible_client/contracts/procedures/results.py), [ProcedureBudgetBoundaryObservation](src/cruxible_client/contracts/procedures/results.py), [ProcedureRunBudgetDeclaredV1](src/cruxible_client/contracts/procedures/results.py), [ProcedureRunBudgetObserved](src/cruxible_client/contracts/procedures/results.py), [ProcedureRunBudgetV1](src/cruxible_client/contracts/procedures/results.py), [ProcedureRunReceiptV3](src/cruxible_client/contracts/procedures/results.py), [ProcedureRunNodePinSet](src/cruxible_client/contracts/procedures/results.py), [ProcedureReplayInputProjection](src/cruxible_client/contracts/procedures/results.py), [ProcedureProviderBindingV1](src/cruxible_client/contracts/procedures/results.py), [ProviderBucketClassificationPlan](src/cruxible_client/contracts/procedures/results.py), [ProcedureProviderBinding](src/cruxible_client/contracts/procedures/results.py), [ProcedureSelectionDecision](src/cruxible_client/contracts/procedures/results.py), [ProcedureAdmissionMaterialMember](src/cruxible_client/contracts/procedures/results.py), [ProcedureAdmissionMaterialManifest](src/cruxible_client/contracts/procedures/results.py), [ProcedureAcquisitionPlan](src/cruxible_client/contracts/procedures/results.py), [ProcedureSourceObservation](src/cruxible_client/contracts/procedures/results.py), [ProcedureSourceCaptureAssociation](src/cruxible_client/contracts/procedures/results.py), [ProcedureTerminalEgressChild](src/cruxible_client/contracts/procedures/results.py), [ProcedureTerminalEgress](src/cruxible_client/contracts/procedures/results.py), [ProcedureRunBudgetDeclared](src/cruxible_client/contracts/procedures/results.py), [ProcedureRunBudget](src/cruxible_client/contracts/procedures/results.py), [ProcedureRunReceiptV4](src/cruxible_client/contracts/procedures/results.py), [ProcedureRunReceiptV5](src/cruxible_client/contracts/procedures/results.py), [ProcedureRunReceipt](src/cruxible_client/contracts/procedures/results.py).

**`contracts.procedures.windows`** — [CaptureEventSelector](src/cruxible_client/contracts/procedures/windows.py), [TriggerEventReference](src/cruxible_client/contracts/procedures/windows.py), [FixedWindow](src/cruxible_client/contracts/procedures/windows.py), [CaptureEventWindow](src/cruxible_client/contracts/procedures/windows.py), [BoundObservationWindow](src/cruxible_client/contracts/procedures/windows.py), [LineTriggerBinding](src/cruxible_client/contracts/procedures/windows.py).

**`contracts.projection`** — [AcceptedProjectionCoordinate](src/cruxible_client/contracts/projection.py), [AcceptedCoordinate](src/cruxible_client/contracts/projection.py), [CandidateGenerationProjectionCoordinate](src/cruxible_client/contracts/projection.py), [ProvisionalProjectionCoordinate](src/cruxible_client/contracts/projection.py).

**`contracts.projection_extensions`** — [ProjectionFactDeclaration](src/cruxible_client/contracts/projection_extensions.py), [ProjectionFact](src/cruxible_client/contracts/projection_extensions.py), [ProjectionExtensionRegistry](src/cruxible_client/contracts/projection_extensions.py).

**`contracts.proposal_models`** — [AuthenticatedActor](src/cruxible_client/contracts/proposal_models.py), [ProposalReceiveLimits](src/cruxible_client/contracts/proposal_models.py), [ProposalAdmissionRequest](src/cruxible_client/contracts/proposal_models.py), [ProposalWithdrawalRecord](src/cruxible_client/contracts/proposal_models.py), [ProposalAdmissionRecord](src/cruxible_client/contracts/proposal_models.py), [ProposalEvaluationRecord](src/cruxible_client/contracts/proposal_models.py), [ProposalResult](src/cruxible_client/contracts/proposal_models.py), [ProposalTransportProtocol](src/cruxible_client/contracts/proposal_models.py).

**`contracts.provider_contracts`** — [ProviderOperationContract](src/cruxible_client/contracts/provider_contracts.py).

**`contracts.provider_execution`** — [ProviderSecretReference](src/cruxible_client/contracts/provider_execution.py), [ProviderSecretBindingIdentity](src/cruxible_client/contracts/provider_execution.py), [ProviderSecretReceiptReference](src/cruxible_client/contracts/provider_execution.py), [ProviderSecretResolutionPlan](src/cruxible_client/contracts/provider_execution.py), [ProviderBudgetTranslation](src/cruxible_client/contracts/provider_execution.py), [ProviderEgressObservation](src/cruxible_client/contracts/provider_execution.py), [VerifiedProviderBinding](src/cruxible_client/contracts/provider_execution.py), [ProviderExternalOccurrencePlan](src/cruxible_client/contracts/provider_execution.py), [ProviderInvocationOutcome](src/cruxible_client/contracts/provider_execution.py), [ProviderInvocationReceipt](src/cruxible_client/contracts/provider_execution.py), [ProviderInvocationOutputDigest](src/cruxible_client/contracts/provider_execution.py), [ProcedureDerivedSourceRequest](src/cruxible_client/contracts/provider_execution.py), [ProviderInvocationStarted](src/cruxible_client/contracts/provider_execution.py), [ProviderInvocationCompleted](src/cruxible_client/contracts/provider_execution.py).

**`contracts.provider_installation`** — [ProviderWheelObject](src/cruxible_client/contracts/provider_installation.py), [ProviderInstallRequest](src/cruxible_client/contracts/provider_installation.py), [ProviderOperationReadiness](src/cruxible_client/contracts/provider_installation.py), [ProviderInstallResult](src/cruxible_client/contracts/provider_installation.py), [ProviderPackageSummary](src/cruxible_client/contracts/provider_installation.py), [ProviderCatalog](src/cruxible_client/contracts/provider_installation.py).

**`contracts.provider_interfaces`** — [ProviderInterfaceFormatError](src/cruxible_client/contracts/provider_interfaces.py), [ProviderBucketClass](src/cruxible_client/contracts/provider_interfaces.py), [ProviderBucketDimension](src/cruxible_client/contracts/provider_interfaces.py), [ProviderBucketVocabulary](src/cruxible_client/contracts/provider_interfaces.py), [ProviderBucketConformanceFixture](src/cruxible_client/contracts/provider_interfaces.py), [ProviderBucketConformanceFixtureProof](src/cruxible_client/contracts/provider_interfaces.py), [ProviderInterfaceRegistrationV1](src/cruxible_client/contracts/provider_interfaces.py), [ProviderClassifierCode](src/cruxible_client/contracts/provider_interfaces.py), [ProviderInterfaceRegistration](src/cruxible_client/contracts/provider_interfaces.py), [AcceptedProviderInterfaceRegistration](src/cruxible_client/contracts/provider_interfaces.py), [ProviderInterfaceLawResult](src/cruxible_client/contracts/provider_interfaces.py), [ProviderBucketClassifierInstallationResult](src/cruxible_client/contracts/provider_interfaces.py), [ProviderBucketClassifierInstallation](src/cruxible_client/contracts/provider_interfaces.py).

**`contracts.providers`** — [ProviderFormatError](src/cruxible_client/contracts/providers.py), [ProviderSigningKey](src/cruxible_client/contracts/providers.py), [ProviderV1](src/cruxible_client/contracts/providers.py), [ProviderDistributionRef](src/cruxible_client/contracts/providers.py), [ProviderImplementationManifestV1](src/cruxible_client/contracts/providers.py), [ProviderRuntimeManifestV1](src/cruxible_client/contracts/providers.py), [ProviderImplementationManifest](src/cruxible_client/contracts/providers.py), [ProviderRuntimeManifest](src/cruxible_client/contracts/providers.py), [ProviderDistributionPin](src/cruxible_client/contracts/providers.py), [ProviderLocalDistributionPin](src/cruxible_client/contracts/providers.py), [ProviderImageProvenance](src/cruxible_client/contracts/providers.py), [ProviderContainerBackendPin](src/cruxible_client/contracts/providers.py), [ProviderLocalEnvBackendPin](src/cruxible_client/contracts/providers.py), [ProviderRuntimeArtifactPayloadV1](src/cruxible_client/contracts/providers.py), [ProviderRuntimeArtifactPayload](src/cruxible_client/contracts/providers.py), [ProviderLocalMaterializationReference](src/cruxible_client/contracts/providers.py), [ProviderContainerMaterializationReference](src/cruxible_client/contracts/providers.py), [ProviderImplementationRecord](src/cruxible_client/contracts/providers.py), [ProviderV2](src/cruxible_client/contracts/providers.py), [Provider](src/cruxible_client/contracts/providers.py), [AcceptedProvider](src/cruxible_client/contracts/providers.py), [ProviderLawResult](src/cruxible_client/contracts/providers.py).

**`contracts.query.definitions`** — [QueryDefinitionFormatError](src/cruxible_client/contracts/query/definitions.py), [QueryEvaluationPolicy](src/cruxible_client/contracts/query/definitions.py), [QueryDefinition](src/cruxible_client/contracts/query/definitions.py), [QueryDefinitionSpec](src/cruxible_client/contracts/query/definitions.py), [AcceptedQueryDefinition](src/cruxible_client/contracts/query/definitions.py), [QueryDefinitionLawResult](src/cruxible_client/contracts/query/definitions.py).

**`contracts.query.grammar`** — [QueryReferenceInventory](src/cruxible_client/contracts/query/grammar.py), [QueryLiteralRef](src/cruxible_client/contracts/query/grammar.py), [QueryParameterRef](src/cruxible_client/contracts/query/grammar.py), [QueryClaimValueRef](src/cruxible_client/contracts/query/grammar.py), [QuerySubjectFieldRef](src/cruxible_client/contracts/query/grammar.py), [QueryEvaluationTimeRef](src/cruxible_client/contracts/query/grammar.py), [QueryComparisonFilter](src/cruxible_client/contracts/query/grammar.py), [QueryMembershipFilter](src/cruxible_client/contracts/query/grammar.py), [QueryClaimPresenceFilter](src/cruxible_client/contracts/query/grammar.py), [QueryConjunctionFilter](src/cruxible_client/contracts/query/grammar.py), [QueryDisjunctionFilter](src/cruxible_client/contracts/query/grammar.py), [QueryNegationFilter](src/cruxible_client/contracts/query/grammar.py), [QueryParameterDeclaration](src/cruxible_client/contracts/query/grammar.py), [QueryEntry](src/cruxible_client/contracts/query/grammar.py), [QueryArtifactsEntry](src/cruxible_client/contracts/query/grammar.py), [QueryTraversalStep](src/cruxible_client/contracts/query/grammar.py), [QueryOrdering](src/cruxible_client/contracts/query/grammar.py), [QueryProjectionField](src/cruxible_client/contracts/query/grammar.py), [QueryProjection](src/cruxible_client/contracts/query/grammar.py), [QueryInclude](src/cruxible_client/contracts/query/grammar.py), [QueryBudgets](src/cruxible_client/contracts/query/grammar.py).

**`contracts.query.results`** — [QueryArtifactDefinition](src/cruxible_client/contracts/query/results.py).

**`contracts.repairs`** — [RepairOperation](src/cruxible_client/contracts/repairs.py), [HandEditInstruction](src/cruxible_client/contracts/repairs.py), [HandEditRepair](src/cruxible_client/contracts/repairs.py), [ServedRepairEnvelope](src/cruxible_client/contracts/repairs.py).

**`contracts.resolution_contracts`** — [ClaimVersionReference](src/cruxible_client/contracts/resolution_contracts.py), [ResolutionContract](src/cruxible_client/contracts/resolution_contracts.py), [ResolutionContractReference](src/cruxible_client/contracts/resolution_contracts.py), [InvestigationBinding](src/cruxible_client/contracts/resolution_contracts.py), [ResolutionContractsRequest](src/cruxible_client/contracts/resolution_contracts.py), [ResolutionContractView](src/cruxible_client/contracts/resolution_contracts.py), [ResolutionContractsResult](src/cruxible_client/contracts/resolution_contracts.py).

**`contracts.resolution_rules`** — [PredictionEqualityRule](src/cruxible_client/contracts/resolution_rules.py), [PredictionThresholdRule](src/cruxible_client/contracts/resolution_rules.py), [PredictionPresenceRule](src/cruxible_client/contracts/resolution_rules.py), [PredictionObservationSelector](src/cruxible_client/contracts/resolution_rules.py).

**`contracts.semantic`** — [SemanticSelector](src/cruxible_client/contracts/semantic.py), [SemanticAddress](src/cruxible_client/contracts/semantic.py), [ContentSpan](src/cruxible_client/contracts/semantic.py), [SourceMapping](src/cruxible_client/contracts/semantic.py).

**`contracts.source_catalog`** — [SourceCatalogEntry](src/cruxible_client/contracts/source_catalog.py), [ProcedureProjectionCatalogEntry](src/cruxible_client/contracts/source_catalog.py), [SourceCatalog](src/cruxible_client/contracts/source_catalog.py), [ResolvedSourceInput](src/cruxible_client/contracts/source_catalog.py), [CompiledSourceDocument](src/cruxible_client/contracts/source_catalog.py), [SourceCompilationManifest](src/cruxible_client/contracts/source_catalog.py), [SourceCompilationBundle](src/cruxible_client/contracts/source_catalog.py), [SourceAlignment](src/cruxible_client/contracts/source_catalog.py).

**`contracts.source_references`** — [ProvisionalSemanticReadCoordinate](src/cruxible_client/contracts/source_references.py), [CandidateGenerationReadCoordinate](src/cruxible_client/contracts/source_references.py), [LedgerSourceReference](src/cruxible_client/contracts/source_references.py), [CasSourceReference](src/cruxible_client/contracts/source_references.py), [ExternalSourceReference](src/cruxible_client/contracts/source_references.py), [SourceSchemaRegistry](src/cruxible_client/contracts/source_references.py), [EvidenceCommitment](src/cruxible_client/contracts/source_references.py), [CoverageDescriptor](src/cruxible_client/contracts/source_references.py), [SourceHandle](src/cruxible_client/contracts/source_references.py), [BodyAccessResult](src/cruxible_client/contracts/source_references.py), [SourceDereferenceResult](src/cruxible_client/contracts/source_references.py), [OpenSourceRequest](src/cruxible_client/contracts/source_references.py).

**`contracts.subjects`** — [SubjectShell](src/cruxible_client/contracts/subjects.py), [AcceptedSubject](src/cruxible_client/contracts/subjects.py), [SubjectLawResult](src/cruxible_client/contracts/subjects.py).

**`contracts.triggers`** — [Trigger](src/cruxible_client/contracts/triggers.py), [CadenceSchedule](src/cruxible_client/contracts/triggers.py), [CronSchedule](src/cruxible_client/contracts/triggers.py), [CaptureLandingSchedule](src/cruxible_client/contracts/triggers.py), [WindowCloseSchedule](src/cruxible_client/contracts/triggers.py), [LineTarget](src/cruxible_client/contracts/triggers.py), [ActionTarget](src/cruxible_client/contracts/triggers.py), [InternalActionSpec](src/cruxible_client/contracts/triggers.py), [INTERNAL_ACTIONS](src/cruxible_client/contracts/triggers.py), [AcceptedTrigger](src/cruxible_client/contracts/triggers.py), [TriggerLawResult](src/cruxible_client/contracts/triggers.py), [TriggerFormatError](src/cruxible_client/contracts/triggers.py).

**`contracts.types`** — [StrictModel](src/cruxible_client/contracts/types.py), [PrincipalRecord](src/cruxible_client/contracts/types.py), [TrustRoot](src/cruxible_client/contracts/types.py), [StorageLayout](src/cruxible_client/contracts/types.py), [CompilerCoordinate](src/cruxible_client/contracts/types.py), [GenesisCoordinate](src/cruxible_client/contracts/types.py), [GenerationDescriptor](src/cruxible_client/contracts/types.py), [AuthorityMatrix](src/cruxible_client/contracts/types.py), [Decommission](src/cruxible_client/contracts/types.py), [Descriptor](src/cruxible_client/contracts/types.py), [PrincipalInspection](src/cruxible_client/contracts/types.py), [Inspection](src/cruxible_client/contracts/types.py).

**`contracts.workspace_advertisement`** — [WorkspaceAdvertisement](src/cruxible_client/contracts/workspace_advertisement.py).

**`contracts.workspace_file`** — [WorkspaceFileSourceRequest](src/cruxible_client/contracts/workspace_file.py), [SourceReadReceipt](src/cruxible_client/contracts/workspace_file.py).

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
from cruxible_client import Cruxible

with Cruxible.connect(
    target="http://localhost:8000",  # Or unix:/path/to/daemon.sock
    instance="your-instance-id",
    workspace=Path.cwd(),
) as pb:
    world = cx.world()
    print(world.coordinate)
```

Accepted reads on `pb` use the current head by default. Each request resolves
one coordinate on the daemon; pagination retains that coordinate. `cx.coordinate`
reports the last observed coordinate without performing I/O. Typed references
carry explicit coordinates and remain pinned. Mixed-coordinate Claim batches
refuse rather than silently moving a reference.

Use `snapshot = cx.at(coordinate)` (or `Cruxible.connect(..., at=coordinate)`)
for a fixed context. `snapshot.world()` and its lazy reads stay at that coordinate.
`cx.world()` returns an independent snapshot that remains readable after the live
client advances. `refresh()` on a pinned context refreshes that same snapshot.
Borrowed contexts share the original connection's lifetime; closing one does not
close the original transport.

`cx.accept(proposal_id)` requests acceptance and returns its exact coordinate;
it does not export a workspace floor or approve the proposal. For exact readback,
even if another writer has advanced the head again:

```python
receipt = cx.accept(proposal_id)
if receipt.accepted_coordinate is not None:
    accepted = cx.at(receipt.accepted_coordinate)
    claims = accepted.claim_views(claim_ids)
```

Drafts retain their observed vocabulary/reference coordinate; admission still
checks current state and reports stale inputs. Operational queues, signing and
write admission retain their current authority/evidence checks; a pinned reading
context does not rewind operational state.

World attributes return live Claim contenders rather than silently selecting a
scalar. Use `world.prefetch(subjects=(...), predicates=(...))` for bounded reads
of known selections, then inspect each Claim's value and verdict; when only
values and verdicts are needed, `world.values(subjects=(...), predicates=(...))`
returns them through `query` without full Claim views, overturned and refused
contenders included. To find something by name across kinds, grep the exported
floor under `.cruxible/floor/` and pass the reference you find to `cx.get(...)`.
A returned
Claim's `subject` path can be passed directly to `cx.claim(subject=...)` or a
changeset Claim writer when revising it. Acceptance and
evidential support are distinct: an accepted Claim may remain unsupported under
its evidence policy.

File-backed authoring requires a declared `.cruxible/sources.yaml` catalog in the
workspace. Supplying a body with `self_source` is an explicit self-assertion, not
an observation of an independent source. The SDK's `derived_by()` method currently
returns a typed unavailable refusal; it is not a supported derivation writer.

### Review and approve an exact candidate

Save `intent.intent_id` after preparation. After a process interruption, reopen
the daemon-owned work through the same instance and authenticated actor:

```python
intent = cx.resume_intent(saved_intent_id)
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
proposal = cx.proposal(submitted.status().proposal_id)
reviewed = proposal.review()
review = reviewed.details  # Inspect all members, evidence, governance and provenance.
# After deciding to approve this exact candidate:
approval = proposal.approve(signer=configured_signer, reviewed=reviewed)
# After a separate decision to accept:
receipt = cx.accept(proposal.proposal_id)
world = cx.world()
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
from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.procedures.contract_schema import PropertySchema
from cruxible_client.contracts.procedures.models import (
    ProcedureBudget,
    ProcedureHardCaps,
)

empty = CarriedContractInput(name="empty", fields={})
count = CarriedContractInput(
    name="count", fields={"count": PropertySchema(type="int")},
)
duration = CanonicalDuration(microseconds=1_000_000)
blueprint = Sequence(
    [
        Project("seed", fields={"count": 1}, contract_out=count),
        Project("result", fields={"count": Previous("count")}, contract_out=count),
    ],
    name="count-example",
    contract_in=empty,
    contract_out=count,
    budget=ProcedureBudget(
        wall_clock=duration, max_provider_calls=1, max_capture_bytes=1024,
    ),
    hard_caps=ProcedureHardCaps(
        max_wall_clock=duration, max_provider_calls=1, max_capture_bytes=1024,
        max_items=100, max_repeat_attempts=1,
    ),
)
preview = blueprint.preview()
assert preview.ready_for_prepare
print(preview.model_dump_json(indent=2))
definition = blueprint.build()
```

With an existing `pb`, `cx.procedure(definition=blueprint).prepare()` creates and
preflights an authoring intent. Submission, review, approval where required,
and acceptance remain separate operations. To include related definitions,
use `cx.changes(rationale=...).procedure(definition=blueprint)` before preparing
the changeset.
