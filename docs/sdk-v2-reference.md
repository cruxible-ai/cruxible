# Cruxible Python SDK v2 reference — proposal

**Status: proposed; none of the new APIs in this document is implemented.**
“v2” identifies this SDK proposal, not a package release or graph-format number.
The [current SDK reference](../packages/cruxible-client/README.md) describes the
implemented public surface at Playbill `7e57b184a`, checked on 2026-09-21.

This is a reference for the proposed SDK: signatures, arguments, values,
semantics, validation, errors, and availability. It contains no implementation
schedule. The agreed authoring direction is contract-derived records and field
access across inputs, outputs, queries, state, and child calls. The spellings
below specify that direction; availability remains proposed. Explicitly
unsettled entries at the end are not supported constructs.

The main addition is retained Python source for authoring Procedures. The source
is compiled into a governed graph; the daemon does not run an arbitrary Python
function as the Procedure. The existing typed ontology, authoring lifecycle,
provider contracts, and execution surfaces remain in use.

## Contents

- [API coverage and availability](#api-coverage-and-availability)
- [Types and notation](#types-and-notation)
- [Contract-derived records](#contract-derived-records)
- [Typed host execution](#typed-host-execution)
- [Lifecycle and execution boundary](#lifecycle-and-execution-boundary)
- [Procedure declaration](#procedure-declaration)
- [ProcedureBlueprint](#procedureblueprint)
- [Bindings and provider selection](#bindings-and-provider-selection)
- [Python source language](#python-source-language)
- [Typed state access](#typed-state-access)
- [Named queries](#named-queries)
- [Provider calls and acquisition](#provider-calls-and-acquisition)
- [Guards and value selection](#guards-and-value-selection)
- [Claim candidates](#claim-candidates)
- [Returns and terminal operations](#returns-and-terminal-operations)
- [Nested Procedure calls](#nested-procedure-calls)
- [Parallel execution and bounded repetition](#parallel-execution-and-bounded-repetition)
- [Preview and diagnostics](#preview-and-diagnostics)
- [Source identity and review](#source-identity-and-review)
- [Complete examples](#complete-examples)
- [Unsettled API details](#unsettled-api-details)

## API coverage and availability

The proposed SDK is the implemented SDK plus the additions and extensions below.
The unchanged API is specified by the linked reference sections, including every
signature, default, returned handle, input model, and HTTP-client operation.
This document does not silently remove an existing operation.

| Existing surface | v2 contract |
|---|---|
| [Connection and state contexts](../packages/cruxible-client/README.md#connection-and-state-context) | Unchanged: instance selection, credentials, snapshots, refresh, connection ownership. |
| [Knowledge authoring](../packages/cruxible-client/README.md#knowledge-authoring) and [changesets](../packages/cruxible-client/README.md#api-changesetdraft) | Same governed definitions and lifecycle. Structured Claim literals gain contract-derived record construction; existing scalar/enum vocabulary remains. |
| [Reads and discovery](../packages/cruxible-client/README.md#reads-and-discovery) | Same query evaluator and discovery/read operations. Typed query binding, parameters, results, and receipts wrap those contracts. |
| [World and typed values](../packages/cruxible-client/README.md#world-and-typed-values) | Existing host reads remain. Compiled source receives a symbolic view of this same ontology, specified below. |
| [Drafts, intents, proposals, approvals](../packages/cruxible-client/README.md#drafts-intents-proposals-and-approvals) | Same prepare, submit, review, approve, and accept distinctions. |
| [Evidence and operational work](../packages/cruxible-client/README.md#evidence-predictions-and-operational-work) | Same capture reads, attestations, predictions, settlement, worklists, and curation. |
| [Source selectors](../packages/cruxible-client/README.md#source-selection) | Existing host file selectors remain; they are not arbitrary filesystem access inside a Procedure. |
| [Procedure composition](../packages/cruxible-client/README.md#procedure-composition-and-execution) | `Sequence` and `ProcedureInput` remain valid. Source authoring is an additional frontend. |
| [Procedure entry points](../packages/cruxible-client/README.md#procedure-entry-points) | `pb.procedure(...)` additionally accepts a source blueprint. Typed input records and run outcomes extend the accepted Procedure handle. `.run(...)` and `pb.run_line(...)` keep their existing authority boundaries. |
| [Projections and workspace](../packages/cruxible-client/README.md#projections-and-workspace) | Unchanged governed blocks, query backings, repin/sync, and portable packages. |
| [Signing](../packages/cruxible-client/README.md#signing-capabilities) | Unchanged explicit signing capabilities. A decorated function grants no signing authority. |
| [Lower-level client](../packages/cruxible-client/README.md#lower-level-http-client) | Existing endpoints remain. A retained Procedure can be invoked without importing its authoring module. |

### New and extended public names

Proposed new import location: `cruxible_client.authoring.source`. This module
**does not exist today**. Types described as symbolic are compiler values, not
ordinary Python objects whose methods execute during authoring.

| Name | Role | Status in this proposal |
|---|---|---|
| `procedure` | Decorate a literal Procedure definition. | Signature proposed below. |
| `ProcedureBlueprint` | Immutable source and binding selection; preview/build operations. | Proposed host object. |
| `ProcedureWorld`, `ProcedureSubject`, `ClaimSelection[T]`, `ProcedureClaim[T]` | Typed, admitted-state expressions using the existing ontology. | Proposed symbolic interfaces; not a second state store. |
| `query`, `ProcedureQueryResult` | Named-query state tap and structured result. | Proposed source intrinsic and result adapter. |
| `call` | Invoke a contracted provider operation. | Proposed source intrinsic over Call semantics. |
| `source`, `AcquisitionResult` | Acquire an observation through a Source interface. | Proposed source intrinsic and contract-derived result. |
| `require` | Explicit refusal condition. | Proposed source intrinsic over Guard semantics. |
| `claim_candidate`, `ClaimCandidate` | Construct a governed Claim candidate without submitting it. | Proposed source counterpart of the existing Claim authoring contract. |
| `emit_capture`, `propose_change_set`, `halt` | Terminal return expressions. | Proposed source syntax over existing terminal categories. |
| `invoke`, `InvocationOutcome[T]` | Invoke an exact accepted child Procedure. | Proposed extension; not served by today's executor. |
| `parallel` | Concurrent independent branches with a join. | Reserved sketch; no final callable signature or default failure policy. |
| `ProcedurePreview`, `CompositionDiagnostic`, `ProcedureCompositionError` | Existing inspection/error types extended with source information. | Extend these types; do not introduce a competing preview API. |
| `Playbill.procedure(definition=...)` | Consume a `ProcedureBlueprint` as well as the existing input forms. | Proposed overload; returns the existing `ProcedureDraft`. |
| `Contract.value`, `bindings.<slot>.input`, `bindings.<query>.parameters` | Construct schema-defined records. | Proposed contract-derived constructors; not executable user helpers. |
| `Playbill.query_binding`, typed `Procedure.input/run` | Resolve query schemas and construct host invocation values. | Proposed adapters over existing definition reads and execution services. |

Unlisted Python functions are not implicitly allowed inside source. In
particular, ordinary SDK calls such as `pb.accept(...)` or `pb.capture(...)`
are host operations, not executable Procedure intrinsics.

## Types and notation

Signatures below use `text` blocks because they specify a proposed language/API,
not runnable declarations in the current SDK. Generic `T`, `I`, and `O` denote
schema-checked value shapes. They do not authorize arbitrary Python classes.

| Name used below | Definition |
|---|---|
| `Contract` | Existing `CarriedContractInput \| AcceptedReferenceInput`, also used by `Sequence`. An accepted reference resolves at the authoring base. |
| `CanonicalValue` | A value representable by the receiving Cruxible contract. No arbitrary object serialization, float coercion, or custom `__dict__` traversal. |
| `Value[T]` | Symbolic Procedure value checked against shape `T`. At execution its actual value must satisfy that contract. |
| `ClaimValue[P]` | Value admitted by the selected predicate `P`: its typed scalar/enum, structured literal record, Subject reference, or exact-content value. Predicate ownership and existing object-kind rules remain authoritative. |
| `Record[S]` | Immutable host value constructed under exact schema `S`. Inside compiled source the corresponding constructor produces `Value[Record[S]]`. Schema identity accompanies authoring checks; wire values retain their existing canonical encoding. |
| `QueryParameters[P]` | A record from the QueryDefinition's parameter declarations, including required/default/type rules. |
| `QueryBinding[P, R]` | Read-only accepted query reference plus its parameter and result schema view. It does not store another query definition or execute a query. |
| `ProviderBinding` | Existing discovery result: Provider and interface identities, exact interface/implementation digests, and declared effect class. The proposed typed handle resolves the selected interface's input/output schemas at the same context. |
| `QueryRef`, `ProcedureRef`, `SubjectRef`, `ClaimTypeRef`, `CaptureRef` | Existing typed references with their identity/version/coordinate assertions. |
| `BindingValue` | `ProviderBinding \| QueryBinding \| QueryRef \| ProcedureRef`. `QueryBinding` is a schema-resolved view of a `QueryRef`. Each slot has one required reference kind determined by its use. |
| `BindingSlot[T]` | Symbolic `bindings.<name>` reference whose selected host binding must have type `T`; not an object the author constructs inside the body. |
| `ProcedureBudgetV3`, `ProcedureHardCapsV3` | Existing explicit budget/cap models. A nested invocation does not reset their effective limits. |
| `TerminalReturn[O]` | Symbolic instruction to end this invocation, carrying a declared result and/or terminal effect. Not a successful runtime receipt by itself. |
| `SourceSpan` | Proposed location data: filename, start/end line, start/end column. Convention proposed: one-based lines, zero-based UTF-8 byte columns, exclusive end. |

Typed Subject/literal wrappers serialize through the existing canonical SDK
rules. A Procedure input/output contract remains explicit; annotations on a
Python function alone do not define a new accepted contract.

No `Any` fallback is implied for unknown bindings or schema fields. A missing
contract is an unresolved obligation, not permission to defer arbitrary shape
inference until a successful run.

## Contract-derived records

Across the Procedure, query, and typed knowledge surfaces specified here,
every schema-defined record has a constructor and typed field access derived
from its existing contract. Do not redeclare an accepted interface or ontology
as an unrelated Python model. The same contract governs source checking, host
values, actual runtime data, and canonical serialization.

### Constructors and owners

| Expression | Schema owner | Result |
|---|---|---|
| `Contract.value(**fields)` | Carried Contract or accepted Contract resolved at the authoring base | `Record[O]` in host code, `Value[Record[O]]` in source. Used for plain returns and terminal `result=`. |
| `bindings.provider_slot.input(**fields)` | Selected ProviderInterface input contract | Typed Call/Source request record. |
| `bindings.child_slot.input(**fields)` | Exact child Procedure input contract | Typed child invocation record. |
| `bindings.query_slot.parameters(**fields)` | Exact QueryDefinition parameter declarations | Typed query parameters. |
| `provider_binding.input(**fields)` | Schema-resolved host provider handle | Same input constructor in ordinary SDK authoring code. |
| `accepted_procedure.input(**fields)` | Selected accepted Procedure input contract | Host invocation record. |
| `query_binding.parameters(**fields)` | Selected accepted QueryDefinition | Host query parameters. |
| `world.claim_type(predicate).value(**fields)` | Object-valued literal schema of the accepted ClaimType | Typed structured Claim value, retaining predicate ownership. Existing scalar constructors and enum members stay available. |

A constructor accepts declared keyword fields; the schema supplies optional
defaults. Missing required fields, unknown closed-record fields, wrong scalar
types, invalid enum values, or incompatible references are localized errors.
Optional, nullable, and absent have their declared distinct meanings. Boolean
does not silently become integer. Dynamic `**kwargs` expansion inside source is
unsupported; ordinary host Python may assemble keyword arguments before calling
a validated constructor.

A nested declared record uses its parent's field schema, e.g.
`bindings.converter.input.source(kind="inline", ...)`, then
`bindings.converter.input(source=document, ...)`. The constructor namespace
derives nesting from `item_fields` or the existing declared JSON schema.
Collections contain typed items under those same schemas. It never turns an
unconstrained JSON object into a closed record by inspecting one example.

Where a schema name conflicts with Python syntax or a constructor's members,
generated stubs supply a deterministic escaped name and retain its exact wire
name mapping. An ambiguous mapping is a schema-resolution diagnostic, never a
silent rename or a fallback to arbitrary attribute lookup.

Constructors within a Procedure are recognized declarative expressions. The
compiler lowers construction and field selection into the existing graph's
data construction/projection operations and applies receiving-contract checks.
It does not execute arbitrary Pydantic classes, validators, or Python methods.
A value from another schema must satisfy the existing compatibility rules;
similar field names do not remove exact pin and predicate-ownership checks.

For record-shaped inputs and outputs, the source surface requires these
constructors or an already typed compatible value. Plain dictionary literals
are not an alternate way to bypass declared record construction. Dictionaries
remain valid for explicitly open JSON and genuinely typed maps such as
`Mapping[ClaimRef, Disposition]`. Contract definition maps such as
`fields: Mapping[str, PropertySchema]` remain typed schema data.

### Resolution and editor types

Today's `ProviderBinding` contains identities/digests, not the input/output
schemas. The proposed handle resolves those schemas through the existing
accepted interface read. Query and Procedure handles likewise use their exact
accepted definitions. Bindings not yet supplied stay visibly unresolved during
preview; prepare/build require resolution. Never fetch an unrelated latest
schema to validate a historical or already bound definition.

Runtime-derived schema discovery does not itself give Python editors static
field knowledge. Generate read-only types/stubs from these exact schemas,
building on the existing World stub surface. Stubs identify their schema
versions/coordinate; they neither pin execution nor replace daemon validation.
Unrepresentable schema constructs receive explicit diagnostics rather than an
`Any` escape hatch.

Wire JSON, CLI/MCP payloads, retained digests, and receipt semantics keep their
existing canonical definitions. Typed SDK adapters validate/serialize those
same records; they do not establish a second payload format.

## Typed host execution

These are proposed v2 signatures. Today's `Procedure.run(**inputs)` and
`ProcedureRun.result: CanonicalValue` are documented in the current reference.
The v2 record argument replaces unstructured keyword payload assembly in this
high-level API. The typed query binding and parameter record likewise replace
bare-name/dictionary assembly on the proposed high-level query path. These are
explicit SDK signature changes; shared HTTP execution operations and their
canonical payload schemas remain unchanged.

```text
Procedure[I, O].input(**fields) -> Record[I]

Procedure[I, O].run(
    *,
    input: Record[I],
    at: AcceptedCoordinate | None = None,
    resolution_contract: ResolutionContractReferenceV1 | None = None,
    trigger_event: TriggerEventReferenceV1 | None = None,
) -> ProcedureRun[O]

Playbill.query_binding(query: str | QueryRef) -> QueryBinding[P, R]

Playbill.run_query(
    query: QueryBinding[P, R],
    *,
    parameters: QueryParameters[P] | None = None,
    budgets: QueryBudgetsV1 | None = None,
) -> ProcedureQueryResult[R]
```

`query_binding` reads the existing accepted QueryDefinition at the SDK context;
it does not evaluate it. Its `ref` retains the existing `QueryRef`. Passing a
query reference into a source blueprint is also supported: binding resolves
the same parameter/result schemas. Parameters may be omitted only when the
definition has no unsupplied required parameters.

The accepted Procedure handle resolves invocation contracts before constructing
an input record. A live head change must not silently replace those contracts:
normal coordinate/version checks still apply. Pure host execution example:

```python
# PROPOSED typed host API; accepted Procedure already exists.
assessment = pb.accepted_procedure("security.assess_asset")
run = assessment.run(
    input=assessment.input(asset_id="web-01", policy_id="default"),
)
if run.succeeded:
    print(run.result.priority)
```

| `ProcedureRun[O]` property | Meaning |
|---|---|
| `run_id`, `coordinate` | Existing run identity and accepted execution context. |
| `status` | Existing typed lifecycle status, including nonterminal states. |
| `succeeded` | True only when successful output satisfying `O` is available. Pending/refused/halted/failed runs do not satisfy it. |
| `result` | Contract-derived successful value `O`, available only on success. Access otherwise raises an explicit result-unavailable error. |
| `outcome` | Typed lifecycle/result variant exposing available failure/refusal details and terminal outcome under existing run contracts. |
| `receipt` | Existing receipt digest/reference. Receipt reads use the corresponding typed receipt contract. |

Child `InvocationOutcome[O]` follows the same successful-value and terminal
availability rules, while preserving its distinct parent/child provenance.
A capture terminal, submitted proposal, successful no-change result, refusal,
and accepted generation remain different records. No wrapper converts one into
another.

`pb.run_line(...)` retains its existing occurrence inputs and lane authority.
Line parameters are validated against the pinned Procedure input contract when
authored/admitted; they are not replaced by arbitrary invocation input supplied
to `run_line`. The returned run resolves `O` from that exact admitted Procedure.

## Lifecycle and execution boundary

| Operation | Reads or effects | Result |
|---|---|---|
| Import a host module | Ordinary host Python may run, as with any Python module. The Procedure body itself is not executed by the decorator. | Local blueprint. |
| Select a provider/query/Procedure | Existing SDK discovery and accepted-state reads. | Typed exact selection. |
| Bind a blueprint | Local immutable selection; no installation, invocation, or acceptance. | New blueprint. |
| Preview | Parse and inspect source. An explicitly supplied World may resolve schema through its pinned SDK context; no provider call or governed write. | Structured preview with errors and pending checks. |
| Build | Require successful compilation and required binding/schema resolution. | Existing authoring input extended with retained source association. |
| Prepare | Existing durable intent/preflight operation; daemon validates the submitted definition and source binding. | Existing `Intent`. |
| Submit/review/approve/accept | Existing distinct governance operations. | Proposal, approval, accepted generation. |
| Run | Existing admission, permissions, effective budgets, provider deployments, and run-lane restrictions. | Existing run state/receipt with the declared outcome. |

A successful preview proves neither runtime input validity nor permission nor
provider availability. Definitions are resolved at the authoring base; execution
binds the admitted run context. These are different coordinates with different
purposes. The accepted definition and its pins cannot drift to newer provider,
query, or child Procedure definitions merely because they exist at run time.

## Procedure declaration

### `procedure`

```text
procedure(
    *,
    name: str,
    input: Contract,
    output: Contract,
    budget: ProcedureBudgetV3,
    hard_caps: ProcedureHardCapsV3,
    terminal_capability: Literal[1, 2, 3] = 1,
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"] = "snapshot",
    acquisition_policy: str | None = None,
    description: str | None = None,
) -> decorator producing ProcedureBlueprint
```

| Argument | Required/default | Meaning and validation |
|---|---|---|
| `name` | Required | Procedure identity name under the existing naming rules; not derived from a mutable filename. |
| `input` | Required | Invocation contract. The `request` expression exposes precisely these fields. |
| `output` | Required | Contract for successful returned values. Every successful reachable return must satisfy it. |
| `budget` | Required | Declared resource budget, using the current model. No hidden unlimited default. |
| `hard_caps` | Required | Declared Procedure ceilings; effective admission policy can be stricter. |
| `terminal_capability` | `1` | Existing numeric capability contract. The graph and chosen run lane must agree; this field does not grant authority. |
| `activation_policy` | `"snapshot"` | Existing lifecycle behavior; values retain their current meanings. |
| `acquisition_policy` | `None` | Accepted acquisition policy when needed by Source operations. Missing required policy prevents readiness. |
| `description` | `None` | Optional human description retained with the definition. |

**Function form:** `def name(request[, world][, bindings]): ...`. `request`
is required, including for an explicitly empty input contract. Optional `world`
and `bindings` are compiler contexts, not caller-provided mutable runtime inputs.
Keyword-only/default/variadic parameters on this source function are outside the
proposed subset. The host decorator arguments supply metadata and contracts;
source compilation does not execute arbitrary decorator argument expressions in
the daemon.

**Returns:** a `ProcedureBlueprint`; no run, draft, or proposal is created.
Calling the resulting object as an ordinary function is an error directing the
author to preview/build or to invoke an accepted Procedure.

**Errors:** unavailable source, unsupported signature/body, invalid metadata,
incompatible contracts, or unsupported capture of host values must be diagnosed.
Parse/lowering errors belong in the preview where source is available; failure
to obtain any source must identify that fact. Interactive definitions without
recoverable source are not silently serialized as executable closures.

An undecorated helper defined by the author is not automatically compiled when
called from a Procedure. Use a documented intrinsic, a contracted provider, or
an explicitly accepted child Procedure.

## ProcedureBlueprint

### Readable properties

These are proposed readonly properties. Exact metadata is inherited from the
decorator; reading a property has no network or execution effect.

| Property | Type | Meaning |
|---|---|---|
| `name` | `str` | Declared Procedure name. |
| `source` | `str` | Authored source retained for review, including comments. |
| `filename` | `str` | Diagnostic/source-map label, not a daemon filesystem permission. |
| `contract_in`, `contract_out` | `Contract` | Declared invocation and successful-result contracts. |
| `budget`, `hard_caps` | Existing models | Declared limits. |
| `terminal_capability` | `Literal[1, 2, 3]` | Declared terminal capability. |
| `activation_policy` | Existing literal union | Declared activation policy. |
| `acquisition_policy`, `description` | `str \| None` | Optional definition metadata. |
| `bindings` | Readonly mapping of slot name to `BindingValue` | Explicit selections currently supplied; an absent slot remains unbound. |

### `bind`

```text
bind(**bindings: BindingValue) -> ProcedureBlueprint
```

Returns a new blueprint with the named selections. Unmentioned selections are
preserved. Supplying a known slot again explicitly replaces that selection on
the new blueprint; it does not alter an accepted Procedure or earlier blueprint.
Unknown slots, wrong reference kinds, or incompatible multiple uses of a slot
are errors. Bindings are data, never live Python callables.

Binding can occur before complete schema resolution; final preview/build must
reject a selected interface whose contracts or effects do not satisfy its uses.
A reusable unbound blueprint may be inspected, but cannot be prepared as though
its unresolved slots were executable.

### `preview`

```text
preview(*, world: World | None = None) -> ProcedurePreview
```

Produces a structured report without executing the body. `world` supplies an
explicit pinned ontology/definition context; it is not a captured live dataset.
No provider is called and no accepted state is written. A supplied World may
perform normal schema reads. Without one, anything requiring accepted schema
resolution remains visibly pending; purely carried contracts can still be
checked locally.

`ready_for_prepare` is false when required source, contract, or binding checks
are unresolved or erroneous. Runtime authority/availability checks still remain
pending even when this flag is true. The proposed additions to `ProcedurePreview`
are specified under [Preview and diagnostics](#preview-and-diagnostics).

### `build`

```text
build(*, world: World | None = None) -> ProcedureInput
```

Uses the same inspection/compilation rules as preview. Returns the shared
Procedure authoring input with its retained source association when all required
static checks pass. Otherwise raises `ProcedureCompositionError` carrying the
preview. It does not return a partial executable graph after a compilation error.

The source-association field's serialization is not finalized; this signature
promises one shared authoring input, not an additional publication channel.

### `Playbill.procedure` overload

```text
pb.procedure(
    *,
    definition: ProcedureInput | Sequence | ProcedureBlueprint,
) -> ProcedureDraft
```

Extends the existing method. A blueprint uses the Playbill context to resolve
required accepted references and compiles into the same authoring contract.
`ProcedureDraft.prepare()` returns the existing intent. The daemon checks the
source/graph relationship; client compilation does not authorize a mismatch.

There is no `blueprint.accept()` or implicit install-and-run operation.

## Bindings and provider selection

`bindings.<name>` names a slot in source. The compiler derives the slot kind
from its uses and requires them to be consistent.

| Source use | Required binding | Selection operation in host code |
|---|---|---|
| `call(bindings.normalize, ...)` | `ProviderBinding` for the compatible Call interface | `pb.provider_binding(interface, provider=...)` |
| `source(bindings.fetch, ...)` | `ProviderBinding` for a Source-compatible acquisition interface | Same accepted provider discovery operation. |
| `query(bindings.exposures, ...)` | `QueryBinding` or `QueryRef` | `pb.query_binding(name_or_ref)` wraps the existing definition read; a supplied `QueryRef` is resolved at binding. |
| `invoke(bindings.observer, ...)` | `ProcedureRef` | `pb.accepted_procedure(name).ref` |

Provider bindings carry exact accepted interface and implementation identifiers.
Their proposed typed view additionally resolves the interface schemas at the
same accepted context. They do not carry credentials or an executable Python object. Runtime deployment
and credentials stay daemon-side. The actual input and output of a provider are
validated at execution even if static compatibility passed.

```python
# Existing provider discovery; source-blueprint .bind is PROPOSED.
fetch = pb.provider_binding("web.fetch", provider="web")
bound = observe_http.bind(fetch=fetch)
preview = bound.preview(world=pb.world())
intent = pb.procedure(definition=bound).prepare()
```

The illustrative names must exist in the chosen instance. Discovery must select
exactly one provider; there is no arbitrary first-match resolution. Built-in
routing, projection, and proposal submission do not need provider bindings.

Installing a package, accepting its Provider/ProviderInterface definitions,
configuring deployment, and granting effect authority remain existing operator
operations. Neither `.bind()` nor source syntax performs these steps implicitly.

## Python source language

This section enumerates the proposed subset. Python is the readable source
notation, not a guarantee that all valid Python programs compile. Host setup can
use ordinary Python; only the retained literal definition is the Procedure.

### Statements

| Construct | Proposed support and exact boundary |
|---|---|
| Function declaration | One literal Procedure function with the recognized parameters and explicit decorator metadata. |
| Local assignment | Bind a named canonical/symbolic value. No mutation of state or arbitrary objects. Reusing a name across mutually exclusive arms is allowed when the join has a well-typed selected value. |
| Reassignment in one straight-line scope | Not part of the initial specified subset; use distinct names. A compiler must not silently reinterpret Python mutation. |
| `if`, `elif`, `else` | Conditional graph routing. Both arms are compiled/validated; only the selected arm executes its runtime operations. |
| `require(...)` statement | Explicit Guard refusal; code/message required. |
| `return value` | Successful pure completion under the output contract. |
| `return emit_capture(...)` / `return propose_change_set(...)` | Governed terminal completion. |
| `return halt(...)` | Explicit halt without a successful output. |
| Return inside a conditional arm | Supported by the proposal; surviving paths continue, terminated paths do not. |
| Function docstring and comments | Retained for review; not executed. |
| Other bare expression statements | Refused unless explicitly recognized. Accidentally discarding a terminal or provider expression is not accepted as a silent no-op. |
| `for`, `while`, comprehensions, generators | Not supported by this source proposal; bounded Repeat has no chosen source spelling yet. |
| `with` | No general context managers. `parallel` remains the reserved, unsettled form described below. |
| `try`, `except`, `finally`, `raise`, `assert` | Refused. Use explicit outcomes and `require`; Python exception control flow is not an implicit graph recovery policy. |
| `async`, `await`, `yield` | Refused. No user event loop or Python coroutine runtime. |
| Nested functions, lambdas, classes, decorators in the body | Refused; no executable code values or closures. |
| Imports, `global`, `nonlocal`, `del` | Refused inside Procedure source. |
| Attribute/item assignment and augmented assignment | Refused; no hidden mutable runtime state. |

### Expressions

| Construct | Proposed support and exact boundary |
|---|---|
| `request.field` and nested declared fields | Checked against the invocation contract. Unknown fields refuse. |
| Ontology namespace/Subject/predicate access | Checked against the explicit World schema, described below. |
| Attribute access on provider/child results | Allowed only for declared fields, with availability checks. |
| Canonical text, integer, Boolean, null literals | Allowed when the receiving schema permits them. Boolean is not silently coerced to integer. |
| Float, bytes, complex, set, arbitrary object literals | Not implicitly canonical; refused unless a future explicit constructor is specified. |
| Contract-derived constructors | Construct closed records using `Contract.value`, a slot's `input`/`parameters` constructor, or a structured ClaimType value constructor. Nested schemas use nested constructors. |
| Lists/tuples | Bounded collections of values conforming to the declared item schema; tuples serialize only where the contract expects a canonical sequence. |
| Dictionary literals | Only explicitly open JSON or typed maps. Closed-record inputs, outputs, and query parameters require a contract-derived constructor or compatible typed value. |
| Typed ontology literal constants | Existing ClaimType values, checked against their predicate. |
| `==`, `!=`, `<`, `<=`, `>`, `>=` | Typed comparisons where the underlying canonical values and predicate contract support the relation. No arbitrary Python rich-comparison methods. |
| `and`, `or`, `not` in Boolean conditions | Preserve left-to-right short-circuit semantics. A skipped operand cannot fail due to an unavailable value. |
| Bare condition | A declared Boolean is allowed; generic Python truthiness of a list, result object, or Subject is refused. |
| `a or b` as operand-valued data selection | Not implied by Boolean support; use explicit branches. |
| Chained comparisons | Not specified; write explicit Boolean comparisons rather than assume Python evaluation details. |
| Membership, `is`, slicing, arbitrary indexing | Not generally specified. Ontology Subject lookup is an explicitly supported indexing form. |
| Arithmetic, bit operations, concatenation, f-strings | Not generally supported. Use supported contracted transforms/providers when necessary. |
| Conditional expression (`x if c else y`) | Not specified; statement branches have the explicit value-selection rule below. |
| Calls | Only documented intrinsics, contract-derived constructors, and ontology constructors/selections. No ambient Python builtins or arbitrary imported helper calls. |

Unknown or unsupported syntax yields a local diagnostic. Constant conditions do
not allow unsupported code to hide in an unreachable arm. Python comparison
syntax does not make stale, conflicting, or absent evidence true by default.

## Typed state access

`world` inside source is a `ProcedureWorld`, a symbolic view of the same ontology
exposed by the existing host `World`. It is not a separately declared Python
ontology, a copied snapshot of all Subjects, or an input callers can replace.

### `ProcedureWorld` and `ProcedureSubject`

| Expression | Result | Validation |
|---|---|---|
| `world.security.remediation` | Accepted kind namespace | Namespace must be unambiguous in the selected schema. |
| `world.security.remediation[request.action_id]` | `ProcedureSubject` | Lookup of the identified Subject in that kind; absent/wrong-kind address refuses when resolved. |
| `world.kind(kind_name)[subject_id]` | Same symbolic Subject lookup | Explicit kind name avoids attribute-name ambiguity. Kind selection must be resolvable for compilation. |
| `world.claim_type(predicate_name)` | Existing typed ClaimType vocabulary | Exact accepted definition; not a newly synthesized schema. |
| `world.claim_type(predicate_name).value(**fields)` | Structured literal record | Object-valued literal schema must declare those fields. Existing scalar/enum constructors keep their meanings. |
| `subject.asset` | `ClaimSelection[T]` | Predicate belongs to the Subject's accepted kind; `T` follows its object schema. |
| `subject[predicate_name]` | Explicit predicate selection | Canonical name resolves attribute collisions. |
| `world.security.work.verification.verified` | Accepted typed literal value | `verified` must be a permitted literal of that ClaimType. |

New `.one()`/`.all()` selectors apply to compiled `ClaimSelection`; they are **not
methods on today's host tuple**. Host World reads continue to return tuples of
`ClaimView` unless separately extended in a later proposal.

### `ClaimSelection.one`

```text
one() -> Value[ProcedureClaim[T]]
```

Select exactly one live Claim for the Subject/predicate at the admitted context.
Zero or multiple results refuse. Incomplete reads refuse. This returns the
selected Claim, not a Boolean about filter specificity. `claim.value` is its
typed value; multiple live Claims can remain even for a precise Subject/predicate
address. No first/newest/best Claim is silently chosen. Cardinality-one ontology metadata does not remove
competing live Claims. One selected Claim is not automatically a supported,
current, or otherwise policy-preferred Claim; check its verdict where needed.

### `ClaimSelection.all`

```text
all(*, limit: int) -> Value[tuple[ProcedureClaim[T], ...]]
```

`limit` is required and positive. Returns the complete bounded selection,
retaining contenders. More results than the bound, or another incomplete read,
refuses instead of returning an apparently complete prefix. An empty complete
selection is valid. The proposal does not specify iteration in arbitrary Python;
use named queries/contracted transforms for population operations.

### `ProcedureClaim[T]`

Retains the semantic fields of the existing Claim view, with a statically known
value shape. It is a selection result, not a mutable Claim builder.

| Field | Meaning |
|---|---|
| `claim_id`, `revision` | Exact selected Claim identity and revision. |
| `subject`, `predicate`, `qualifier`, `role` | Statement address and role. |
| `object_kind` | Literal, Subject, or exact-content object category. |
| `value` | Typed value under the accepted predicate. Subject-valued Claims preserve canonical Subject identity. |
| `lifecycle_state`, `verdict` | Retained lifecycle/assessment information; not conflated with selection cardinality. |
| `captures` | Evidence references exposed by the existing view. |

### Read timing and dependencies

Selectors may depend on invocation input or other admission-resolvable state.
They cannot depend on a value first produced by a provider or child runtime
operation. Such a selector receives an unsupported-dependency diagnostic; the
compiler must not quietly read a newer live state later in execution.

State taps are bound at admission. A field inside a branch does not promise a
lazy database read only when that branch executes. Preview must show those
admission dependencies and any branch-related availability constraints.

There is no `state_input(...)`, no implicit `resolved(...)`, and no automatic
loading of every asset before selecting the first. For a known action, write the
specific Subject/predicate reads needed. For joins and populations, use a query.

## Named queries

### `query`

```text
query(
    definition: BindingSlot[QueryBinding[P, R] | QueryRef],
    *,
    parameters: Value[QueryParameters[P]] | None = None,
    budgets: QueryBudgetsV1 | None = None,
) -> Value[ProcedureQueryResult[R]]
```

| Argument | Default | Meaning |
|---|---|---|
| `definition` | Required | Accepted QueryDefinition binding. |
| `parameters` | `None` | Construct with `bindings.<query>.parameters(**fields)`. Omission is valid only when no required parameter remains unsupplied. |
| `budgets` | `None` | Existing query-budget defaults/ceilings plus effective run policy. |

Uses the existing named-query StateTap/evaluator, not a new query engine.
Parameters remain admission-resolvable. Query parameter declarations supply
types and required/default rules. Source and host constructors reject unknown
fields and incompatible values; runtime validation still checks actual data.

### `ProcedureQueryResult[R]`

A typed adapter over the existing query response. Today's `PlaybillQueryRun`
exposes `result` and `receipt` as dictionaries; it does not expose the convenience
properties below.

| Field | Meaning |
|---|---|
| `coordinate` | Exact accepted query context. |
| `definition_path`, `definition_digest` | Exact query definition used. |
| `result` | Typed existing query-result envelope, including verdict, rows, conflicts, truncation, refusal, parameters, budgets, and evaluation time. |
| `receipt` | Typed existing query execution receipt, including result binding. |
| `completed` | Derived from the existing result verdict. |
| `truncated` | Derived from existing clipping metadata. Completion alone does not establish completeness. |
| `has_conflicts` | Whether the existing result conflict collection is nonempty. |
| `result.truncation.returned_result_count` | Existing returned-row count. It is a population count only after appropriate completion, completeness, visibility, deduplication, and conflict checks. |

A many-cardinality query must currently use `surface_conflicts`. A Procedure
that requires an unambiguous result must check `has_conflicts`; it cannot change
that QueryDefinition to `refuse_on_conflict`. Zero visible results does not
establish absence of unknown or excluded facts.

### Typed rows and projections

Row types follow the QueryDefinition's declared result shape:

| Result shape | Typed surface |
|---|---|
| `subject` | Existing result Subject identity, bound identities, read Claims, and declared projected fields. |
| `relation_claim` | Existing relation Claim record plus declared projection/binding metadata. |
| `path` | Existing path, binding, and relation records with declared projected fields. |
| `artifact_definition` | Existing typed artifact definitions, discriminated by artifact kind/version. Retain the current completeness requirement on the `artifact_definitions` listing convenience. |

For a declared projection, `row.fields.<projection_name>` accesses the typed
value from the existing projected-field collection. Its type follows the
projection expression and pinned ClaimType/Subject field. Keep declared
optionality, multiplicity, and conflict information; a possibly absent or
ambiguous value cannot silently become a scalar. Without a declared projection,
do not invent arbitrary domain fields on a Subject row. Nested includes retain
their own completeness metadata.

Host code may iterate typed result rows normally. This does not enable arbitrary
Python loops or indexing inside compiled source. The assessment example uses
the existing returned-count field and therefore needs no invented aggregation
provider or unrestricted list operation.

## Provider calls and acquisition

### `call`

```text
call(
    interface: BindingSlot[ProviderBinding],
    *,
    input: Value[I],
    effect_policy: str | None = None,
) -> Value[O]
```

`interface` and `input` are required. `I` and `O` come from the selected accepted
interface contracts. Construct record input with `bindings.<slot>.input(...)`;
the output exposes only fields declared by `O`, including typed nested records
where a nested schema exists. `effect_policy` uses the existing Call policy reference;
`None` does not excuse a missing policy for an effect that requires one.

**Effects:** invokes the verified installed provider at runtime, under admission
and effective permissions. Does not run at preview/build. Its output is the
contracted value, not an `InvocationOutcome`; provider failure follows the
existing node/run failure behavior.

**Validation:** exact interface/implementation binding, compatible input/output,
allowed effects, runtime data validation, and effective resource limits. A
provider-written arbitrary Python function is not a valid binding until it goes
through normal package registration and installation.

### `source`

```text
source(
    interface: BindingSlot[ProviderBinding],
    *,
    request: Value[I],
    capture_contract: str,
) -> Value[AcquisitionResult[O]]
```

All three arguments are required. The selected interface must support acquisition;
its request contract determines allowed fields. Construct the request with
`bindings.<slot>.input(...)`. The adapter exposes the actual declared acquisition
output contract; it does not assign a universal web-specific shape to all Sources. `capture_contract` references the
accepted contract constraining the acquired observation. Procedure acquisition
policy and daemon/provider limits still apply.

**Effects:** fetches/observes through the provider and retains the acquisition
material according to existing Source behavior. It does not author or accept
Claims, and it is not by itself the capture terminal for the Procedure.

**Result:** a contract-derived acquisition value. For the web acquisition shape,
fields may include `retrieved.final_url` and `retrieved.body_sha256`. Those are
web-contract fields, not a promise that every provider has the same payload.
A Source result cannot be arbitrarily relabeled as independent verified evidence.

**Errors:** incompatible acquisition interface, invalid request, missing policy,
failed acquisition, exceeded effective byte/time limits, unverifiable capture,
or output contract mismatch. A failed observation is not a negative hypothesis
or a successful empty response.

### Pure projection and transformation

Constructing a typed result with `OutputContract.value(...)` or selecting declared
fields expresses pure projection. It does not need a provider. Provider fields
declared only as open JSON remain JSON; their observed payload is not enough to
infer a closed type or permit arbitrary nested attribute access. The existing `Transform` step remains
available in the current Sequence/ProcedureInput API. A general source intrinsic
for every transform specification has not been selected; arbitrary Python
arithmetic is not an implicit replacement. Use a contracted Call when it is the
intended domain computation, rather than introducing a provider for a simple
supported equality comparison.

## Guards and value selection

### `require`

```text
require(condition: Value[bool], *, code: str, message: str) -> None
```

All arguments are required; there is no hidden refusal-code default. True
continues. False produces the specified explicit refusal. Invalid or unavailable
operands are not silently treated as false. `code` and `message` are explicit
source constants for the refusal, validated under the diagnostic contract.

### Conditional routing

`if condition` chooses an arm. False by itself is not a refusal. Each condition
must be Boolean or a supported typed comparison. `elif` tests are evaluated only
after preceding conditions fail. `and`/`or` preserve Python short-circuit behavior;
an eager all-operands predicate cannot be substituted where it changes behavior.

### Joining branch values

```python
# PROPOSED source fragment.
if observation.release == target_claim.value:
    verification = verification_type.verified
else:
    verification = verification_type.failed
return VerificationOutput.value(verification=verification)
```

The post-branch value is the value from the selected arm. Both producers must be
compatible with the receiving output contract. `VerificationOutput` in this
fragment is the declared carried/accepted output contract, with the two shown
verification literals in its enum. Reading an unproduced value is
an error unless every path lacking it has already terminated.

A name assigned on only one continuing arm is not assigned `None` automatically.
A select is not a database resolution policy and does not choose among competing
Claims. Preview must expose branch producers, the join, and the selected value's
type. This selection capability is new; today's graph control convergence alone
does not supply it.

## Claim candidates

### `claim_candidate`

This proposed intrinsic reuses the public Claim authoring vocabulary. It does
not introduce a separate untyped evidence list or a second Claim contract.
`P` below is the selected predicate; it determines the value schema.

```text
claim_candidate(
    *,
    subject: ProcedureSubject | SubjectRef,
    predicate: ClaimTypeRef | WorldClaimType,
    value: Value[ClaimValue[P]] | ClaimValue[P],
    role: ClaimRole | str,
    rationale: str,
    supported_by: CaptureRef | None = None,
    copied_from: CaptureRef | None = None,
    self_source: str | None = None,
    qualifier: str | None = None,
    effective_period: EffectivePeriod | None = None,
    revises: ClaimRef | str | None = None,
    dispositions: Mapping[ClaimRef | str, Disposition] | None = None,
    basis: tuple[Value[ProcedureClaim], ...] = (),
) -> Value[ClaimCandidate]
```

| Arguments | Contract |
|---|---|
| `subject`, `predicate`, `value` | Required typed statement. The predicate's object kind/schema and Subject-kind restrictions apply. |
| `role`, `rationale` | Required explicit role and supplied rationale. A role not admitted by the ClaimType is refused. |
| `supported_by`, `copied_from`, `self_source` | Exactly one is required, as in existing host Claim authoring. No implicit source from the fact that a Procedure ran. |
| `qualifier`, `effective_period` | Optional statement qualification and applicability period. |
| `revises`, `dispositions` | Existing lineage and contender-handling meanings. Do not automatically supersede every earlier Claim. |
| `basis` | Proposed exact Claim dependencies used by this conclusion. Default empty; supplied Claims retain their exact versions rather than only current values. |

**Returns:** a symbolic candidate description; it has not been submitted,
approved, or accepted. It is consumed by `propose_change_set`.

For structured literal values, use the accepted ClaimType's `value(**fields)`
constructor. Scalar values and enum members use the existing typed vocabulary.
The candidate itself remains a typed `ClaimCandidate`; its canonical wire
representation is not another authoring payload the caller assembles.

**Validation:** reuse existing value/evidence/coordinate/contender rules. Compiled
source uses captured evidence references, not arbitrary host file objects. A
copied capture cannot be upgraded to independent support by changing an argument.
A changed target Claim is not covered by an earlier verification's exact basis.

`basis` is a proposed addition, not an existing helper. Its lowering into existing
dependency records must preserve that meaning; the retained field encoding is
not selected by this document. Source authoring of new Subjects/ClaimTypes and
non-Claim candidate kinds can continue through the existing authoring API; a
separate intrinsic for every changeset operation is not specified here.

## Returns and terminal operations

| Return form | Outcome | Authority / effect |
|---|---|---|
| `return value` | Success with the declared output value. | Pure completion; no artificial capture/proposal is needed. |
| `return emit_capture(...)` | Successful capture terminal plus declared result. | Registers/retains evidence through existing authorized terminal machinery. |
| `return propose_change_set(...)` | Proposal terminal plus declared result when submission succeeds. | Submits a governed proposal. Does not approve or accept it. |
| `return halt(reason)` | Explicit halt without successful output. | Does not manufacture an output satisfying the declared success contract. |

A terminal expression must be returned and must end that path. No later step on
the same path runs. An alternate arm may end differently. Fall-through without a
return is an error unless the output and explicit completion form permit it;
there is no accidental Python `None` success.

The chosen terminal capability and execution lane must permit every reachable
terminal. Current direct Procedure runs do not supply the accepted Line lane's
capture/proposal authority. Source syntax does not remove that restriction.
`PostInbox` and `MandateSettlement` have no source frontend specified here; their
existence in other contracts is not a promise of SDK support.

### `emit_capture`

```text
emit_capture(
    value: Value[EvidenceInput],
    *,
    capture_contract: str,
    result: Value[O],
) -> TerminalReturn[O]
```

All arguments are required. `EvidenceInput` denotes the terminal's supported,
typed evidence input contract, including compatible verified acquisition results.
It is not arbitrary canonical data. The capture contract's registration rules
and existing evidence validation determine compatibility. `capture_contract` governs terminal output registration. `result` is
the Procedure's successful return value and must satisfy its output contract.
Construct record results with the Procedure's `OutputContract.value(...)`.
The successful result is distinct from the typed registered capture and terminal receipt.

Acquisition and output capture contracts have different roles. This operation
does not perform another network fetch or convert unverified material into
verified evidence. Capability/policy/input failures are explicit. A child using
this terminal finishes the child invocation; an authorized parent may continue
from the child's returned evidence/outcome.

### `propose_change_set`

```text
propose_change_set(
    *,
    candidates: tuple[Value[ClaimCandidate], ...] | list[Value[ClaimCandidate]],
    result: Value[O],
) -> TerminalReturn[O]
```

Both arguments are required. Construct record results with the declared
`OutputContract.value(...)` or pass an already compatible typed value.
Candidates must satisfy existing changeset rules;
the source wrapper does not bypass preflight, source validation, competing Claim
dispositions, or governance. The result describes the computation under its
bindings; it does not assert that the proposal was accepted. Existing no-change
and refusal outcomes must remain distinguishable in the terminal record rather
than being disguised as acceptance.

### `halt`

```text
halt(reason: str) -> TerminalReturn[NoSuccessfulValue]
```

An explicit reason is required. Ends this invocation without a successful output.
It is distinguishable from `require` refusal and from provider/runtime failure.
Previously completed external effects or retained evidence are not rolled back
by this expression.

## Nested Procedure calls

### `invoke`

```text
invoke(
    procedure: BindingSlot[ProcedureRef],
    *,
    input: Value[I],
) -> Value[InvocationOutcome[O]]
```

Both arguments are required. The exact accepted child supplies contracts `I`
and `O`. Use `bindings.<child>.input(...)` for record input; successful output
retains the child's declared typed fields. This is a proposed execution extension, not syntax for a feature already
served by the current executor.

| Property | Required behavior |
|---|---|
| Version | Exact child pin; a newer child never silently replaces it. |
| State | Child state taps use the parent admission context. |
| Authority | Inherited/constrained by the admitted actor and run lane. No grant from nesting. |
| Budgets | Child work consumes shared effective limits; it cannot reset the budget by invoking again. |
| Effects | Child effects and terminal capability included in admission requirements. |
| Provenance | Parent/child occurrence relationship and exact definitions remain inspectable. |
| Completion | Child terminal completes the child; parent sees a typed outcome and may continue. |
| Recursion | Cyclic invocation and unbounded recursive depth are not part of this proposal. |
| Dispatch | The child is an accepted binding, not arbitrary code or a name discovered in provider output. |

### `InvocationOutcome[O]`

| Field | Proposed type / availability |
|---|---|
| `status` | Distinguishes success, halt, refusal, and failure; exact enum spellings remain unsettled. |
| `succeeded` | `bool`; true only when a valid successful output exists. |
| `value` | `O`, available only on success. Compiler requires control-flow proof or an explicit guard before reading. |
| `terminal` | Typed terminal result when the child's declared outcome provides one. A pure child does not gain a capture here. |
| `terminal.capture` | Verified capture reference only when the child contract guarantees capture-terminal success. |
| `receipt` | Nested execution receipt/reference preserving exact child identity and outcome. Exact transport wrapper remains unsettled. |

A successful child with mixed possible terminal kinds may still require an
additional terminal-kind check before `.terminal.capture` is available. The
examples assume a child whose every successful path emits a capture. No success
output exists for a halted/refused/failed child.

```python
# PROPOSED source fragment.
observed = invoke(
    bindings.observer,
    input=bindings.observer.input(asset=asset_claim.value),
)
if not observed.succeeded:
    return halt("The observation did not succeed.")
release = observed.value.release
```

This does not choose general retry/cancellation/recovery semantics. Child-outcome
handling cannot authorize ignoring an effect failure that the existing admission
or run contract treats as fatal. Such outcome rules must be explicit before
serving this API; ordinary Python exceptions are not the escape route.

## Parallel execution and bounded repetition

### `parallel` — reserved form

```python
# PROPOSED SKETCH, not a callable contract with settled options.
with parallel():
    inventory = invoke(
        bindings.inventory,
        input=bindings.inventory.input(asset=request.asset),
    )
    advisory = invoke(
        bindings.advisory,
        input=bindings.advisory.input(asset=request.asset),
    )
```

The intended meaning is actual concurrent independent branches. A sequential
executor must not advertise this as parallel execution. Ordinary `if` arms are
conditional alternatives, not parallel branches.

| Aspect | Specified meaning / unresolved part |
|---|---|
| Dependencies | Branches cannot read siblings' unfinished outputs or mutate shared locals. |
| Result association | Declared branch identities, not completion order. |
| Limits | Shared effective budget and authority, not independent unlimited copies. |
| Join | Required, but all/any/partial-success API is not yet chosen. |
| Cancellation | No default promised. Must account for already-started effects. |
| Failure | Exact parent outcome and availability of successful sibling outputs remain unsettled. |
| Rollback | Never implied for external effects or already-retained evidence. |

Consequently this reference deliberately gives no final `parallel(...)`
signature, Boolean success guarantee, or readiness claim. A blueprint containing
an unsupported/unsettled form must refuse clearly, not run it sequentially.

### Bounded repetition

The existing graph-authoring Repeat form remains documented in the current
contracts. Source syntax for Repeat is undecided; Python `for`/`while`, recursion,
retry, sleeping, and scheduling are not interchangeable with bounded Repeat.
No arbitrary Python fallback is introduced to cover those missing spellings.

## Preview and diagnostics

### Extended `ProcedurePreview`

Reuse the current type and its existing fields:
`name`, `ready_for_prepare`, `contracts`, `contract_in`, `contract_out`,
`terminal_capability`, `acquisition_policy`, `nodes`, `edges`, `providers`,
`terminals`, `returns`, `budget`, `hard_caps`, `errors`, and `pending_checks`.
Its normal structured serialization remains the inspection surface. The SDK
objects are typed: contract references use existing contract-reference variants;
`nodes` use node-kind variants over the graph contracts; dependencies, branch
results, return paths, and pending checks have explicit records. Diagnostics'
expected/actual values use typed contract/construct descriptions. Existing typed
edge/provider maps remain maps. Do not fill new fixed-shape fields with `Any`
dictionaries merely because their serialized preview is JSON.

Proposed additional fields:

| Field | Proposed content |
|---|---|
| `source` | Authored text, source identity, diagnostic filename, selected source-language rule identifier. |
| `source_map` | Node/expression identities mapped to `SourceSpan` values. Line numbers are locations, not persistent node identities. |
| `state_dependencies` | Subject/predicate or named-query selections, parameter dependencies, admitted-context requirements, completeness/cardinality conditions. |
| `binding_requirements` | Every slot, kind, expected contract/effects, supplied selection or explicit unresolved status. |
| `branch_values` | Conditions, producers, joins, selected output types, and any path on which a value is unavailable. |
| `return_paths` | Reachable pure/capture/proposal/halt paths, output shape, required terminal capability. |
| `children` | Exact child selections, contracts, state/authority/budget obligations when nested calls are present. |
| `concurrency` | Proposed branch/join information only when the parallel contract is defined and supported. |

Static errors and pending runtime checks are distinct. A provider not installed
at execution time is not proved installed by a structurally valid blueprint.
A preview should identify what the author must supply, what the daemon must
validate at prepare, and what necessarily remains a runtime check.

### Extended `CompositionDiagnostic`

Keep existing `step`, `code`, and `message`. Proposed additions:

| Field | Meaning |
|---|---|
| `span` | Narrowest useful source location of the problem. |
| `related_spans` | Other relevant producers, consumers, assignments, or contract uses. |
| `expected`, `actual` | Structured contract/shape or construct details where applicable. |
| `hint` | A concrete supported expression or action, without suggesting arbitrary-code execution. |

### Errors by stage

| Stage / condition | Observable behavior |
|---|---|
| Source unavailable | Explicit source-availability error; no fabricated retained source or opaque pickled closure. |
| Invalid Python | Parser location and syntax error. |
| Valid unsupported Python | Construct name/location and supported alternative; no partial executable graph. |
| Unknown ontology field | Kind/predicate/schema context and source span. |
| Missing/wrong binding | Slot, required kind/contracts, and selected value, if any. |
| Incompatible provider connection | Producer/consumer spans and exact contract mismatch. |
| Unsafe branch output | Use location plus paths that do not produce it. |
| Runtime-derived state selector | Selector and runtime dependency that prevents admission binding. |
| Incomplete field/query result | Cardinality/completeness failure distinguished from a false business condition. |
| Invalid actual provider data | Existing typed execution refusal/error with node/source mapping. |
| Unauthorized effect or terminal | Existing admission/run-lane policy failure; not a Python syntax error. |
| Compiler/daemon rule mismatch | Explicit compatibility refusal; do not recompile historical data under newer semantics. |
| Internal compiler failure | Internal failure classification retained; do not disguise it as unsupported user syntax. |

`preview()` reports static diagnostics. `build()` raises the existing
`ProcedureCompositionError` with that preview on a static failure. Normal
transport, authorization, and governed refusal handling stays as specified in
the current SDK. Exact new diagnostic-code strings are not minted by this
proposal; once published they must be stable public identifiers.

## Source identity and review

The authored source, executable definition, selected compiler rules, and their
verifiable association belong to the accepted Procedure version. Source-only
edits, including comments, must remain reviewable even when executable graph
behavior is unchanged. Graph identity and retained-source identity need not be
the same digest.

Host setup is not retained executable authority. For example, reading an
instance, assembling contracts, or choosing a provider in a host script does not
mean the daemon may execute that setup script during verification. The retained
source and explicit bindings must suffice for non-executing verification.

A graph produced from Sequence or another authoring surface can have a canonical
rendered source view. That view must be labeled as a rendering; it cannot claim
to recover comments or the original source structure. The public accessor and
source-storage encoding remain unsettled. Historical source and graphs retain
their original verification rules.

## Complete examples

These are complete behavioral scenarios for the proposed source frontend, not
programs executable on today's SDK. They state the starting accepted state,
every input/output schema, external binding, invocation lane, and expected
effect. They do not bootstrap an empty instance or imply that governance and
provider installation happen on import. Existing contract constructors in the
setup can be validated today; source compilation and nested execution remain
proposed.

### Accepted fixture and shared contracts

| Required accepted definition/state | Content and producer |
|---|---|
| `security.asset/web-01` | `security.asset.internet_facing = True`, a supported Boolean Claim. |
| `security.triage_policy/default` | `security.triage_policy.urgent_threshold = 2`, a supported positive-integer Claim. |
| `security.exposure/exp-a` and `exp-b` | Each has a supported `security.exposure.asset` relation to `web-01` and supported/current `security.exposure.status = "open"`. |
| `security.exposure/exp-c` | Same asset relation, with status `"mitigated"`. |
| `security.feed/kev` | Supported `security.feed.url` Claim identifying the configured feed; supported `security.feed.expected_digest` Claim containing the canonical body digest from a previously retained and accepted baseline observation. |
| `security.feed.verification` ClaimType | A feed-subject literal enum, `matches` or `differs`, admitting `derivation` and the retained evidence/dependencies used below. No prior verification Claim in this fixture. |
| `web` / `web.fetch` and `docs` / `doc.to_markdown` | Accepted Provider/ProviderInterface definitions and matching installed implementations. Names are fixture selections, not universal built-in defaults. |
| `security.http_response` CaptureContract | Permits the fixture HTTP source/logical source `security.feed`, records acquisition provenance and retained response bytes under explicit byte/retention rules. |
| `security.registered_http_observation` CaptureContract | Permits registration of the verified acquisition result and preserves its original evidence binding. |
| `security.feed_reads` acquisition policy | Permits the configured feed acquisition and its declared Source aliases under effective limits. The parent/child admission includes the child's policy/effects. |
| Capture/proposal Lines | Manually triggered accepted Lines, pinned to the relevant Procedures with parameters below. Capture uses rung 1; proposal uses rung 2 and an applicable live ProcedureMandate. Required credentials/grants remain instance-specific. |

Scalar state fields used with `.one()` each have exactly one live Claim in this
fixture. Its verdict checks are explicit below. Claims and relationships were
accepted before these Procedures run; acquisition alone does not create them.

Shared setup uses current contract classes. The `contract` helper is ordinary
host code outside compiled source:

```python
from cruxible_client.authoring.inputs import CarriedContractInput
from cruxible_client.contracts.procedures.contract_schema import PropertySchema
from cruxible_client.contracts.procedures.models import (
    ProcedureBudgetV3,
    ProcedureHardCapsV3,
)
from cruxible_client.contracts.captures import CanonicalDurationV1


def contract(name: str, **fields: PropertySchema) -> CarriedContractInput:
    return CarriedContractInput(
        name=name,
        fields=fields,
        allow_extra=False,
    )


AssessInput = contract(
    "assess_input",
    asset_id=PropertySchema(type="string"),
    policy_id=PropertySchema(type="string"),
)
AssessOutput = contract(
    "assess_output",
    asset_id=PropertySchema(type="string"),
    exposure_count=PropertySchema(type="int"),
    priority=PropertySchema(
        type="string",
        enum=["none_known", "routine", "urgent"],
    ),
)
ObserveInput = contract(
    "observe_input",
    url=PropertySchema(type="string"),
)
ObserveOutput = contract(
    "observe_output",
    url=PropertySchema(type="string"),
    body_digest=PropertySchema(type="string"),
)
VerifyInput = contract(
    "verify_input",
    feed_id=PropertySchema(type="string"),
)
VerifyOutput = contract(
    "verify_output",
    feed_id=PropertySchema(type="string"),
    verification=PropertySchema(type="string", enum=["matches", "differs"]),
)
ConvertInput = contract(
    "convert_input",
    content_base64=PropertySchema(type="string"),
)
ConvertOutput = contract(
    "convert_output",
    input_bucket=PropertySchema(type="string"),
    document=PropertySchema(type="json"),
    derived=PropertySchema(type="json"),
)

BUDGET = ProcedureBudgetV3(
    wall_clock=CanonicalDurationV1(microseconds=30_000_000),
    max_provider_calls=2,
    max_capture_bytes=8_388_608,
)
CAPS = ProcedureHardCapsV3(
    max_wall_clock=CanonicalDurationV1(microseconds=30_000_000),
    max_provider_calls=2,
    max_capture_bytes=8_388_608,
    max_items=1_000,
    max_repeat_attempts=1,
)
```

The record constructors `.value(...)` used later are proposed extensions to
these contract handles. The carried field declarations above are already real
SDK types; their `fields` map is typed schema data, not an untyped value payload.

Common source imports:

```python
# PROPOSED module: not importable from today's SDK.
from cruxible_client.authoring.source import (
    procedure,
    query,
    require,
    source,
    call,
    invoke,
    emit_capture,
    claim_candidate,
    propose_change_set,
    halt,
)
```

### Assess an asset from state and a named query

The caller chooses an asset and policy. Accepted state supplies internet
exposure, the threshold, and the population of known exposures. The caller
does not precompute `exposure_count`.

This existing query definition selects distinct supported/current open exposure
Subjects for one asset. Its reverse traversal follows exposure-to-asset relation
Claims, then filters the exposure status:

```python
from cruxible_client.authoring.inputs import QueryDefinitionInput
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.query.definitions import (
    QueryDefinitionSpecV1,
    QueryEvaluationPolicyV1,
)
from cruxible_client.contracts.query.grammar import (
    QueryBudgetsV1,
    QueryEntryV1,
    QueryTraversalStepV1,
    QueryParameterRefV1,
    QueryParameterDeclarationV1,
    QueryComparisonFilterV1,
    QueryClaimValueRefV1,
    QueryLiteralRefV1,
)

open_exposures = QueryDefinitionInput(
    kind="query_definition",
    query_definition=QueryDefinitionSpecV1(
        identity=ArtifactIdentity(
            kind="QueryDefinition",
            name="security.open_exposures",
        ),
        description="Distinct known open exposures for one asset.",
        entry=QueryEntryV1(
            binding="asset",
            subject_kinds=("security.asset",),
            subject_id=QueryParameterRefV1(parameter="asset_id"),
        ),
        traversal=(
            QueryTraversalStepV1(
                binding="exposure",
                from_binding="asset",
                predicate="security.exposure.asset",
                direction="reverse",
                target_subject_kinds=("security.exposure",),
            ),
        ),
        where=QueryComparisonFilterV1(
            left=QueryClaimValueRefV1(
                binding="exposure",
                predicate="security.exposure.status",
            ),
            operator="eq",
            right=QueryLiteralRefV1(value="open"),
            value_type="string",
        ),
        result_binding="exposure",
        result_shape="subject",
        result_cardinality="many",
        dedupe="subject",
        parameters=(
            QueryParameterDeclarationV1(name="asset_id", value_type="string"),
        ),
        evaluation_policy=QueryEvaluationPolicyV1(
            visible_verdicts=("supported",),
            visible_currency=("current",),
            conflict_behavior="surface_conflicts",
            result_expiry=CanonicalDurationV1(microseconds=300_000_000),
        ),
        default_budgets=QueryBudgetsV1(
            max_results=1_000,
            max_traversal_depth=1,
        ),
        maximum_budgets=QueryBudgetsV1(
            max_results=1_000,
            max_traversal_depth=1,
        ),
    ),
)
```

Prepare through `pb.query_definition(definition=open_exposures).prepare()`,
submit, and complete normal governance before binding the accepted query.
Required ClaimType pins resolve at the authoring base; this does not bypass
definition admission.

```python
@procedure(
    name="security.assess_asset",
    input=AssessInput,
    output=AssessOutput,
    budget=BUDGET,
    hard_caps=CAPS,
)
def assess_asset(request, world, bindings):
    asset = world.security.asset[request.asset_id]
    policy = world.security.triage_policy[request.policy_id]
    internet_facing = asset.internet_facing.one()
    urgent_threshold = policy.urgent_threshold.one()

    require(
        internet_facing.verdict == "supported"
        and urgent_threshold.verdict == "supported",
        code="unsupported_triage_inputs",
        message="Asset exposure and triage threshold must be supported.",
    )
    require(
        urgent_threshold.value >= 1,
        code="invalid_triage_threshold",
        message="The urgent threshold must be positive.",
    )

    exposures = query(
        bindings.open_exposures,
        parameters=bindings.open_exposures.parameters(
            asset_id=request.asset_id,
        ),
    )
    require(
        exposures.completed and not exposures.truncated,
        code="incomplete_exposure_query",
        message="Triage requires a complete exposure query.",
    )
    require(
        not exposures.has_conflicts,
        code="conflicting_exposure_state",
        message="Resolve exposure conflicts before computing priority.",
    )

    count = exposures.result.truncation.returned_result_count
    if count == 0:
        priority = "none_known"
    elif internet_facing.value and count >= urgent_threshold.value:
        priority = "urgent"
    else:
        priority = "routine"

    return AssessOutput.value(
        asset_id=request.asset_id,
        exposure_count=count,
        priority=priority,
    )
```

The existing count describes returned deduplicated Subjects. Completion and
conflict checks precede its use as the visible population count. No list
iteration, invented aggregation provider, or custom `ExposureRow` is needed.

```python
# After the query is accepted; PROPOSED binding/blueprint adapters.
bound_assessment = assess_asset.bind(
    open_exposures=pb.query_binding("security.open_exposures"),
)
assessment_preview = bound_assessment.preview(world=pb.world())
assessment_intent = pb.procedure(definition=bound_assessment).prepare()

# After separate submission, review, approval as required, and acceptance:
accepted_assessment = pb.accepted_procedure("security.assess_asset")
assessment_run = accepted_assessment.run(
    input=accepted_assessment.input(
        asset_id="web-01",
        policy_id="default",
    ),
)
if assessment_run.succeeded:
    print(assessment_run.result.priority)
    print(assessment_run.result.exposure_count)
```

| Fixture/condition | Expected outcome |
|---|---|
| Starting fixture | Typed result: `asset_id="web-01"`, `exposure_count=2`, `priority="urgent"`. |
| Asset becomes internal | Count 2, priority `routine`. |
| Both open exposures become mitigated | Count 0, priority `none_known`. |
| Required field has zero/multiple live Claims | Cardinality refusal; no arbitrary selection. |
| Query budget clips results | Refusal before a priority result. |
| Query surfaces conflicting exposure evidence | Refusal before a priority result. |

This is a pure successful result plus execution evidence, not an authored
priority Claim. `none_known` means none under the declared visibility policy;
it does not establish that an asset is safe or that the inventory is complete.

### Acquire and register a feed observation

The reusable acquisition Procedure accepts a URL. The parent below obtains
that URL from accepted state. The installed `web.fetch` interface declares the
request fields, including the format enum and byte limit.

```python
@procedure(
    name="security.observe_feed",
    input=ObserveInput,
    output=ObserveOutput,
    budget=BUDGET,
    hard_caps=CAPS,
    terminal_capability=1,
    acquisition_policy="security.feed_reads",
)
def observe_feed(request, bindings):
    fetch_request = bindings.fetch.input(
        url=request.url,
        logical_source="security.feed",
        expected_format="json",
        render=False,
        max_bytes=4_194_304,
    )
    observation = source(
        bindings.fetch,
        request=fetch_request,
        capture_contract="security.http_response",
    )
    return emit_capture(
        observation,
        capture_contract="security.registered_http_observation",
        result=ObserveOutput.value(
            url=observation.retrieved.final_url,
            body_digest=observation.retrieved.body_sha256,
        ),
    )
```

```python
# Existing provider selection; PROPOSED typed blueprint binding.
bound_observer = observe_feed.bind(
    fetch=pb.provider_binding("web.fetch", provider="web"),
)
observer_preview = bound_observer.preview(world=pb.world())
observer_intent = pb.procedure(definition=bound_observer).prepare()
```

After acceptance, standalone execution uses the accepted capture Line whose
parameters supply the URL. Invoke it with `pb.run_line(line_identity_digest)`;
`line_identity_digest` is the exact identity digest of that accepted Line.
Direct Procedure execution does not acquire terminal permission from the
decorator's capability.

A successful run returns a typed URL/body-digest result and separately records
the registered evidence capture and receipt. A failed fetch produces no
successful capture terminal. Request limits are bounded by the effective
Procedure, capture, provider, and instance policies. Capturing a feed does not
parse its entries or author exposure Claims.

### Compare a feed observation with an accepted baseline

This connects state reads, an exact child binding, a successful capture, a
comparison, and a governed proposal. The child was defined above; no unspecified
observer or provider computation supplies a hidden value.

```python
@procedure(
    name="security.verify_feed",
    input=VerifyInput,
    output=VerifyOutput,
    budget=BUDGET,
    hard_caps=CAPS,
    terminal_capability=2,
)
def verify_feed(request, world, bindings):
    feed = world.security.feed[request.feed_id]
    url = feed.url.one()
    expected_digest = feed.expected_digest.one()
    verification_type = world.claim_type("security.feed.verification")

    require(
        url.verdict == "supported"
        and expected_digest.verdict == "supported",
        code="unsupported_feed_baseline",
        message="The feed URL and expected digest must be supported.",
    )
    observed = invoke(
        bindings.observer,
        input=bindings.observer.input(url=url.value),
    )
    if not observed.succeeded:
        return halt("The feed observation did not succeed.")

    if observed.value.body_digest == expected_digest.value:
        verification = verification_type.matches
    else:
        verification = verification_type.differs

    candidate = claim_candidate(
        subject=feed,
        predicate=verification_type,
        value=verification,
        role="derivation",
        rationale="Compared the acquired body digest with the accepted baseline.",
        supported_by=observed.terminal.capture,
        basis=(url, expected_digest),
    )
    return propose_change_set(
        candidates=[candidate],
        result=VerifyOutput.value(
            feed_id=request.feed_id,
            verification=verification,
        ),
    )
```

```python
# The observer must already be accepted at the selected context.
bound_verifier = verify_feed.bind(
    observer=pb.accepted_procedure("security.observe_feed").ref,
)
verifier_preview = bound_verifier.preview(world=pb.world())
verifier_intent = pb.procedure(definition=bound_verifier).prepare()
```

The parent has no independent Source node. Admission includes the child's
pinned acquisition policy and effective effects/budgets; nested execution does
not invent or bypass an acquisition policy on the parent. The accepted proposal
Line binds `feed_id="kev"` and requests rung 2 under its applicable mandate.
Its invocation uses the same `pb.run_line(...)` surface.

| Stage or condition | Expected result/state effect |
|---|---|
| Before run | Accepted URL and baseline digest; no verification Claim. |
| Successful child | Retained observation, registered capture, child output and receipt. |
| Acquired digest equals baseline | Typed `matches` result associated with a submitted verification proposal. |
| Acquired digest differs | Typed `differs` result associated with a submitted verification proposal. This proves a byte comparison, not the truth of every feed statement. |
| Child has an inspectable unsuccessful outcome | Parent halts without proposing a verification Claim. Fatal failures retain existing run failure semantics. |
| Proposal is later accepted | Governed verification Claim with captured evidence and exact URL/baseline dependencies. |
| Baseline is later revised | Earlier verification remains bound to the earlier exact basis; it does not silently verify the new baseline. |

This is a first-verification scenario. A later revision must explicitly name its
predecessor and handle contenders under existing authoring rules; running a new
occurrence does not automatically supersede an earlier Claim. Changing the
baseline or interpreting feed contents is outside this Procedure's purpose.

### Call an installed document converter

This uses the existing `doc.to_markdown` provider interface. The caller really
does supply a document: it is the artifact being transformed, not a hidden
precomputed state conclusion. Its declared `source` input has a nested JSON
schema; `document` and `derived` outputs are declared as open JSON objects.

```python
@procedure(
    name="security.convert_advisory",
    input=ConvertInput,
    output=ConvertOutput,
    budget=BUDGET,
    hard_caps=CAPS,
)
def convert_advisory(request, bindings):
    document = bindings.converter.input.source(
        kind="inline",
        filename="advisory.txt",
        media_type="text/plain",
        content_base64=request.content_base64,
    )
    converted = call(
        bindings.converter,
        input=bindings.converter.input(
            source=document,
            page_count=1,
            scanned="born_digital",
            layout="linear",
        ),
    )
    return ConvertOutput.value(
        input_bucket=converted.input_bucket,
        document=converted.document,
        derived=converted.derived,
    )
```

```python
from base64 import b64encode

bound_converter = convert_advisory.bind(
    converter=pb.provider_binding("doc.to_markdown", provider="docs"),
)
converter_preview = bound_converter.preview(world=pb.world())
converter_intent = pb.procedure(definition=bound_converter).prepare()

# After normal submission and acceptance:
advisory = b"Upgrade the affected service before Friday.\n"
accepted_converter = pb.accepted_procedure("security.convert_advisory")
conversion_run = accepted_converter.run(
    input=accepted_converter.input(
        content_base64=b64encode(advisory).decode("ascii"),
    ),
)
if conversion_run.succeeded:
    print(conversion_run.result.derived)
```

The current implementation returns converted Markdown and metadata in
`derived`. The accepted interface only declares that field as an object, so the
Procedure returns it intact. It must not pretend `derived.text` is a declared
typed field without a more precise accepted interface schema. Ordinary host
code may inspect the open JSON with the corresponding runtime checks.

A successful conversion returns a result and execution evidence. It does not
register a capture, submit a Claim, or accept knowledge.

### Example coverage and expected diagnostics

| Element | Scenario or expected diagnostic |
|---|---|
| Typed state and `.one()` | Assessment and verification select one named Subject's field; missing/ambiguous selections refuse. |
| Query parameters/results | Assessment uses a contract-derived parameter record and the actual returned count after completion/conflict checks. |
| Branch routing and joined values | Assessment priority and verification stance come from the selected branch and satisfy the declared result schema. |
| Source, Call, nested input | Acquisition, conversion, and verification all construct inputs from exact selected contracts. |
| Typed nested record | Converter `source` input comes from its nested interface schema. |
| Capture/proposal/pure terminals | Observation registers evidence; verification submits a proposal; assessment/conversion return values. |
| Child outcome/capture availability | Verification checks success; the child emits a capture on every successful path. A pure child's output cannot be used as a capture. |
| Host result access | `assessment_run.result.priority` follows the admitted output contract; no user-written result dictionary parser. |
| Misspelled `max_btyes` in fetch input | Unknown-field diagnostic at that keyword; accepted field is `max_bytes`. |
| `max_bytes="large"` | String/integer mismatch at the constructor argument. |
| `expected_format="xml"` | Invalid enum value under the selected `web.fetch` contract. |
| Undeclared output field or missing required field | Localized constructor error against the Procedure output schema. |
| Reading an open JSON member as a declared attribute | Schema precision diagnostic; no type inferred from one successful sample. |
| Executing these examples today | Source API unavailable; no claim of source-compiler or nested-runtime validation. |

Parallel joins, arbitrary iteration, recursive invocation, and the unselected
Transform/Repeat source forms are not made executable by these examples.

## Unsettled API details

The reference is explicit about what it cannot yet specify. These are missing
public contract decisions, not an implementation work breakdown.

| Area | Not yet specified |
|---|---|
| Source serialization/access | Retained-source association field, source-storage encoding, and exact read accessor. The identity/review/verification requirements above are fixed in this proposal. |
| Source declaration lookup | Exact accepted source packaging for interactive/non-file definitions. No fallback to daemon Python execution. |
| Schema forms without typed source representation | Unsupported nested schema forms require explicit diagnostics. The existing schema remains authoritative; no handwritten replacement or `Any` fallback. Typed parameter constructors, row envelopes/projections, and conflict/count conveniences are specified above. |
| Transform and Repeat syntax | Complete source forms for these existing node capabilities. Current graph/Sequence paths remain available as documented. |
| Exact Claim `basis` lowering | Encoding that preserves selected Claim versions and dependency meaning using existing records where possible. |
| Child outcome wrapper | Final status enum and nested receipt transport shape, including which failures are inspectable versus fatal. |
| Parallel API | Join, cancellation, sibling failure, result availability, and concurrency-limit arguments/defaults. |
| Diagnostics | Final stable code strings and serialized additions to preview/diagnostic models. |

Unsettled entries must not be advertised as available APIs. Unsupported source
gets an explicit error and locality; it never gets an arbitrary-code escape hatch.
