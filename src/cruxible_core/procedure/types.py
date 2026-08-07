"""Schema and persisted types for governed procedures."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from cruxible_core.config.schema import (
    AssertSpec,
    ContractReference,
    PropertyType,
    WorkflowStepSchema,
    reject_reserved_property_equality_condition_keys,
    workflow_step_kind,
)
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.evidence import EvidenceRef
from cruxible_core.primitives import new_id
from cruxible_core.procedure.graph_format import (
    DEFINITION_FORMAT_V1,
    coerce_present_declared_format,
    definition_format_version,
    register_v2_step_type,
)
from cruxible_core.procedure.guards import GuardSpec
from cruxible_core.receipt.types import Receipt
from cruxible_core.temporal import utc_now

ProcedureStatus = Literal["pending", "live", "rejected", "retired", "withdrawn"]
"""Lifecycle states. ``withdrawn`` is the author's own retraction of a pending
proposal, kept distinct from the reviewer verdict ``rejected`` so the record
says which one happened. Neither is live, so neither holds a name."""
ProcedureTier = Literal["governed_write", "graph_write", "admin"]
ProcedureRunStatus = Literal["started", "finalized"]
ProcedureRunVerdict = Literal["succeeded", "failed", "refused", "budget_exceeded"]
ProcedureRefusalReason = Literal[
    "procedure_not_live",
    "definition_digest_changed",
    "tier_not_permitted",
    "preflight_refused",
    "precondition_evaluation_failed",
    "precondition_unsatisfied",
]
"""Stable, low-cardinality classification recorded on a ``refused`` run.

Deliberately a bucket, not the refusal message: the message carries procedure
ids and content digests, so a most-frequent-reason aggregate over messages
would degenerate to "every refusal is unique". The full message stays on the
run's receipt; this is only what the read surface can count."""

MAX_PROCEDURE_EVIDENCE_BYTES = 256 * 1024
"""Maximum canonical JSON bytes retained for one typed procedure output."""

PROCEDURE_EVIDENCE_HEAD_BYTES = 4096
"""Bounded UTF-8 head retained for an oversized procedure output."""

MAX_PROCEDURE_STEPS = 100
"""Maximum stored step definitions, counting repeat containers and nested steps once."""

MAX_PROCEDURE_EXPANDED_STEPS = 500
"""Maximum steps after expanding every repeat to its declared maximum attempts."""

MAX_PROCEDURE_EXPANDED_PROVIDER_CALLS = 250
"""Maximum provider calls after expanding every repeat to its maximum attempts."""

MAX_PROCEDURE_REPEAT_ATTEMPTS = 25
"""Maximum attempts accepted by one bounded repeat step."""

MAX_PROCEDURE_BRANCH_NODES = 12
"""Maximum guard nodes in one definition (R11).

Paths are exponential in branch count, and two things consume paths: the
reviewer's enumeration (§3.1 analysis 7) and the reviewer's head. Every
CORRECTNESS analysis here is ``O(V+E)`` and needs no such ceiling -- this one
bounds what the display and the human have to absorb, and it is why a
definition can never present a reviewer with a set of behaviours nobody can
read.
"""

MAX_PROCEDURE_ENUMERATED_PATHS = 64
"""Display cap on enumerated control paths (§3.3).

Never consulted by a correctness check. A truncated enumeration says so; no
refusal, no analysis and no ceiling is derived from it.
"""

_TOP_LEVEL_STEP_KINDS = frozenset(
    {
        "query",
        "provider",
        "assert",
        "assert_not_truncated",
        "assert_count",
        "assert_exists",
        "shape_items",
        "join_items",
        "filter_items",
        "aggregate_items",
        "dedupe_items",
    }
)
_NESTED_STEP_KINDS = _TOP_LEVEL_STEP_KINDS - {"query"}

_GRAPH_NODE_BODY_KEYS = frozenset(
    {"guard", "on_true", "on_false", "next", "step", "project", "propose_group_from"}
)
"""Keys that identify a graph node or a step wrapper, refused inside a repeat body."""

ABORT_TARGET = "$abort"
"""The one control-edge target that is not a step id: terminate with the message."""


def _require_top_level_procedure_kind(step: WorkflowStepSchema) -> WorkflowStepSchema:
    kind = workflow_step_kind(step)
    if kind not in _TOP_LEVEL_STEP_KINDS:
        raise ValueError(
            f"procedure steps may only use {sorted(_TOP_LEVEL_STEP_KINDS)}; "
            f"found disallowed kind '{kind}'"
        )
    return step


ProcedureInnerStep = Annotated[
    WorkflowStepSchema, AfterValidator(_require_top_level_procedure_kind)
]
"""A workflow step RESTRICTED to the ruled procedure subset, refused at PARSE.

This is the type-system refusal, not a downstream compile check. The top-level
whitelist iterates ``self.steps`` and tests ``isinstance(step,
WorkflowStepSchema)``; a step WRAPPED in a flow node is not a
``WorkflowStepSchema`` instance, so it would be skipped entirely -- admitting
``apply_all``, ``propose_relationship_group`` or any other excluded kind
through ``step:``. Typing the wrapped slot closes that hole where it opens,
and nothing downstream has to remember to check.
"""


class ProcedureBudget(BaseModel):
    """Required hard bounds for a procedure invocation."""

    wall_clock_s: float = Field(gt=0, le=600)
    max_provider_calls: int = Field(ge=0)

    model_config = ConfigDict(extra="forbid")


class ProcedureRepeatSpec(BaseModel):
    """One statically bounded procedure repeat body.

    A repeat executes at most 25 attempts. Its nested body deliberately excludes
    queries and repeat itself: only provider calls, assert-family invariants, and
    item-shaping steps are accepted. ``until`` uses the existing assert condition
    shape and is evaluated against the current attempt's outputs.
    """

    max_attempts: int = Field(ge=1, le=MAX_PROCEDURE_REPEAT_ATTEMPTS)
    until: AssertSpec
    steps: list[WorkflowStepSchema] = Field(min_length=1)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def refuse_graph_nodes_in_body(cls, data: Any) -> Any:
        """R17 -- a repeat body holds plain steps, never graph nodes or wrappers.

        Branching inside a bounded loop body is out of scope, and admitting it
        would break two things at once: the attempt-isolation contract (the
        handler swaps ``step_outputs`` per attempt), and the local-digest
        reasoning that treats a repeat body as the node's own CONTENT. That
        treatment is only unambiguous while nested steps carry no control
        targets, so the control-target exclusion is a no-op inside a body.
        """
        if not isinstance(data, dict):
            return data
        steps = data.get("steps")
        if not isinstance(steps, list):
            return data
        for step in steps:
            if not isinstance(step, dict):
                continue
            found = sorted(_GRAPH_NODE_BODY_KEYS.intersection(step))
            if found:
                raise ValueError(
                    "repeat bodies may not contain graph nodes or step wrappers; "
                    f"found {found}. Branch outside the loop instead."
                )
        return data

    @model_validator(mode="after")
    def validate_nested_step_subset(self) -> ProcedureRepeatSpec:
        disallowed = sorted(
            {
                workflow_step_kind(step)
                for step in self.steps
                if workflow_step_kind(step) not in _NESTED_STEP_KINDS
            }
        )
        if disallowed:
            allowed = ", ".join(sorted(_NESTED_STEP_KINDS))
            raise ValueError(
                "repeat nested steps may only use provider, assert-family, and "
                f"item-shaping kinds ({allowed}); found {disallowed}"
            )
        _validate_unique_step_ids(self.steps, context="repeat nested steps")
        nested_aliases = {step.as_ for step in self.steps if step.as_ is not None}
        for ref in _workflow_references([self.until.left, self.until.right]):
            if not ref.startswith("$steps."):
                raise ValueError(
                    "repeat.until may reference only current-attempt '$steps.<alias>' outputs"
                )
            alias = ref[len("$steps.") :].split(".", 1)[0].split("[", 1)[0]
            if alias not in nested_aliases:
                raise ValueError(
                    f"repeat.until reference '{ref}' does not name a current-attempt "
                    "nested step alias"
                )
        return self


class ProcedureRepeatStepSchema(BaseModel):
    """Top-level procedure repeat step with an explicit output alias."""

    id: str
    repeat: ProcedureRepeatSpec
    as_: str = Field(alias="as")

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class ProcedureGuardStepSchema(BaseModel):
    """A predicate with two labelled successors. Procedure-only.

    Graph control lives on procedure-only schemas and adds ZERO fields to any
    shared model. The four shared assert specs and ``WorkflowStepSchema`` are
    the CONFIGURED-WORKFLOW grammar, compiled by the ordinary workflow path,
    which would parse branch fields and then silently ignore them. A configured
    workflow cannot parse a guard node because guard nodes are not members of
    ``WorkflowStepSchema`` -- the type system is the refusal, so none is
    written.
    """

    id: str
    guard: GuardSpec
    on_true: str | None = None
    """Step id, or ``None`` for fallthrough to the next step in list order."""
    on_false: str | None = None
    """Step id or ``"$abort"``; ``None`` means ``"$abort"``."""
    message: str

    model_config = ConfigDict(extra="forbid")


class ProcedureFlowStepSchema(BaseModel):
    """An unconditional successor override on a non-guard node. Procedure-only.

    There is NO outer ``id``: the node's identity is the wrapped step's ``id``,
    exposed as a property so unique-id validation and every downstream analysis
    keep reading ``step.id`` unchanged. A second, independently-settable id
    would be an unconstrained alias for the same node.
    """

    step: ProcedureInnerStep
    next: str

    model_config = ConfigDict(extra="forbid")

    @property
    def id(self) -> str:
        return self.step.id

    @property
    def as_(self) -> str | None:
        """The wrapped step's alias.

        Alias discovery reads ``as_`` off each step; a wrapper that did not
        forward it would make a wrapped step's output invisible to every
        downstream ``$steps.<alias>`` reference.
        """
        return self.step.as_


class ProjectSpec(BaseModel):
    """Assemble one output object from named alias references."""

    fields: dict[str, Any]
    contract_out: ContractReference | None = None
    """Where wi-062's declared and enforced output contract will land.

    Shipping the declaration and its enforcement is that work's own lane; this
    is the SITE, reserved so the node does not have to change shape later.
    """

    model_config = ConfigDict(extra="forbid")


class ProcedureProjectStepSchema(BaseModel):
    """A projection node: the thing ``returns`` NAMES. Procedure-only.

    It is deliberately not a replacement for ``returns``. ``returns`` stays a
    top-level string naming one alias because the observed-output publication
    path reads it with a literal ``$.returns`` json_extract and has no fallback
    by design -- moving, nesting or renaming it makes that join match nothing
    and silently publishes no envelope.
    """

    id: str
    project: ProjectSpec
    as_: str = Field(alias="as")

    model_config = ConfigDict(populate_by_name=True, extra="forbid")


ProcedureStepSchema = (
    WorkflowStepSchema
    | ProcedureRepeatStepSchema
    | ProcedureGuardStepSchema
    | ProcedureFlowStepSchema
    | ProcedureProjectStepSchema
)
"""The procedure step union.

Deliberately UNTAGGED. Discrimination is safe because every member is
``extra="forbid"`` with distinct required fields, and the flow wrapper is
additionally distinguished by having no ``id`` of its own. Converting to a
``Field(discriminator=...)`` union would require a discriminator key in the
wire form and hence a v1 digest change, so a guardrail asserts pairwise unique
identifiability instead.
"""

register_v2_step_type(ProcedureGuardStepSchema)
register_v2_step_type(ProcedureFlowStepSchema)
register_v2_step_type(ProcedureProjectStepSchema)


def unwrap_procedure_step(step: Any) -> Any:
    """Return the wrapped inner step of a flow node, or the step itself."""
    inner = getattr(step, "step", None)
    return step if inner is None else inner


class ProcedureStaticExpansion(BaseModel):
    """Review-visible static upper bounds computed from a procedure body.

    The two expanded counts are longest-PATH maxima (§3.3), and each carries
    the path that realises it. Without the witness a reviewer meeting
    ``expanded_provider_calls=9`` on a branching definition has no way to find
    which arm spends it, and the number reads as a property of the body rather
    than of one execution.

    ``total_steps`` has no witness because it is not a path property: it counts
    STORED step definitions, which is what the 100-step ceiling is about.
    """

    total_steps: int
    expanded_steps: int
    expanded_provider_calls: int
    expanded_steps_path: tuple[str, ...] = ()
    expanded_provider_calls_path: tuple[str, ...] = ()

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProcedureAuthoringWarning(BaseModel):
    """One non-blocking authoring diagnostic, typed (§3.6).

    ``code`` is what a surface can group, filter and act on; the string list it
    ships alongside can only be printed. ``node_ids`` names where the finding
    lives, which a message can only spell out in prose that no caller can
    parse.

    Deliberately NOT scored and deliberately not aggregated. Per
    ``dd-specificity-doctrine`` the warning family is a design razor, never a
    metric: extension cardinality is uncomputable in an open world, and any
    aggregate over these would Goodhart into vacuity.
    """

    code: str
    message: str
    node_ids: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProcedurePrecondition(BaseModel):
    """Optional named-type property-equality authorization condition."""

    entity_type: str | None = None
    condition: dict[str, str | int | float | bool] | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_shape(self) -> ProcedurePrecondition:
        has_entity_type = "entity_type" in self.model_fields_set
        has_condition = "condition" in self.model_fields_set
        if not has_entity_type and not has_condition:
            return self
        if has_condition and (self.entity_type is None or not self.entity_type.strip()):
            raise ValueError("entity_type must be non-empty when condition is present")
        if has_entity_type and not self.condition:
            raise ValueError(
                "condition must declare at least one property=value pair "
                "when entity_type is present"
            )
        assert self.condition is not None
        reject_reserved_property_equality_condition_keys(self.condition)
        return self

    @property
    def is_empty(self) -> bool:
        """Return whether this precondition authorizes every invocation."""
        return self.entity_type is None


class ProcedureDefinition(BaseModel):
    """Agent-proposable utility plan constrained to the procedure step subset.

    ``type`` is intentionally absent and forbidden: procedure bodies always use
    utility workflow semantics. Validation caps stored definitions at 100 steps,
    their repeat-expanded execution at 500 steps, and their repeat-expanded
    provider calls at 250. Refusal messages include all three computed counts so
    reviewers can see which bound the definition exceeded.
    """

    name: str
    description: str | None = None
    contract_in: ContractReference = "cruxible.EmptyInput"
    contract_out: ContractReference | None = None
    steps: list[ProcedureStepSchema] = Field(min_length=1)
    returns: str
    precondition: ProcedurePrecondition
    budget: ProcedureBudget
    declared_tier: ProcedureTier = "governed_write"
    evidence_outputs: list[str] | None = None
    graph_format: int | None = None
    """The format discriminator, and the ONLY signal of definition format.

    Legal wire spellings are exactly ABSENT and ``2``; every other value is
    refused at parse. The annotation is left open rather than
    ``Literal[2] | None`` because the refusal has to REPORT the unreadable
    value it met: a Literal collapses "this core cannot read format 3" into a
    generic type error that names no version and offers no remedy.

    ``None`` means format v1; ``2`` means the graph format. It defaults to
    ``None`` so ``exclude_none=True`` drops it from every existing definition
    and no v1 byte moves. It is deliberately explicit rather than inferred:
    procedure steps carry arbitrary ``dict[str, Any]`` payloads, so a valid,
    current v1 definition whose provider input happens to contain ``next`` and
    ``parameters`` would be mis-detected by any content sniffer and routed
    through v2 digest rules.

    It is also the old-reader lock. ``ProcedureDefinition`` is
    ``extra="forbid"``, so a 0.3 core raises ``extra_forbidden`` on this key at
    all three definition parse paths -- the store row reader, the snapshot
    loader, and the state-diff artifact reader -- rather than silently
    mis-executing a definition whose control flow it cannot follow.
    """

    model_config = ConfigDict(extra="forbid")

    @field_validator("graph_format", mode="before")
    @classmethod
    def refuse_non_canonical_format_spellings(cls, value: Any) -> Any:
        """Ahead of coercion, and only when the KEY IS PRESENT.

        Two things this position buys, neither available later. Coercion is the
        fail-open: ``int | None`` is non-strict, so ``"2"`` and ``2.0`` would
        arrive at the value check already looking like a legal ``2``. And a
        before-validator on a defaulted field runs ONLY when the key was
        supplied, which is the only place explicit null can still be told apart
        from absence -- by the time the model exists both are ``None``.
        """
        return coerce_present_declared_format(value)

    @model_validator(mode="after")
    def validate_definition(self) -> ProcedureDefinition:
        if not self.name.strip():
            raise ValueError("procedure name must be non-empty")
        # R13/R14. Refuses an undeclared graph construct at PARSE, so no
        # downstream stage has to remember to look.
        definition_format_version(self)

        # Defence in depth behind ProcedureInnerStep: the whitelist UNWRAPS, so
        # it walks the inner step of every wrapper kind rather than skipping
        # wrapped steps as non-WorkflowStepSchema instances.
        unwrapped = [unwrap_procedure_step(step) for step in self.steps]
        disallowed = sorted(
            {
                workflow_step_kind(step)
                for step in unwrapped
                if isinstance(step, WorkflowStepSchema)
                and workflow_step_kind(step) not in _TOP_LEVEL_STEP_KINDS
            }
        )
        if disallowed:
            allowed = ", ".join(sorted(_TOP_LEVEL_STEP_KINDS | {"repeat"}))
            raise ValueError(
                f"procedure steps may only use {allowed}; found disallowed kinds {disallowed}"
            )
        _validate_unique_step_ids(self.steps, context="procedure steps")
        if self.evidence_outputs is not None:
            if len(set(self.evidence_outputs)) != len(self.evidence_outputs):
                raise ValueError("evidence_outputs must not contain duplicate aliases")
            available_outputs = {
                alias
                for alias in (_step_output_alias(step) for step in self.steps)
                if alias is not None
            }
            unknown = sorted(set(self.evidence_outputs) - available_outputs)
            if unknown:
                raise ValueError(f"evidence_outputs references unknown step aliases: {unknown}")

        branch_nodes = [
            str(step.id) for step in self.steps if isinstance(step, ProcedureGuardStepSchema)
        ]
        if len(branch_nodes) > MAX_PROCEDURE_BRANCH_NODES:  # R11
            raise ValueError(
                f"procedure declares {len(branch_nodes)} guard nodes; the branch-node "
                f"ceiling is {MAX_PROCEDURE_BRANCH_NODES}. Paths grow exponentially in "
                "branch count, and a definition whose behaviours a reviewer cannot "
                "enumerate cannot be reviewed. Split it into procedures that compose."
            )

        expansion = self.static_expansion()
        refusals: list[str] = []
        if expansion.total_steps > MAX_PROCEDURE_STEPS:
            refusals.append(f"total step ceiling is {MAX_PROCEDURE_STEPS}")
        if expansion.expanded_steps > MAX_PROCEDURE_EXPANDED_STEPS:
            refusals.append(f"expanded step ceiling is {MAX_PROCEDURE_EXPANDED_STEPS}")
        if expansion.expanded_provider_calls > MAX_PROCEDURE_EXPANDED_PROVIDER_CALLS:
            refusals.append(
                f"expanded provider-call ceiling is {MAX_PROCEDURE_EXPANDED_PROVIDER_CALLS}"
            )
        if self.budget.max_provider_calls < expansion.expanded_provider_calls:
            # An under-provisioned ceiling is a statically unrunnable definition:
            # the run would abort mid-flight on the budget guard every time. Keep
            # this a refusal, not an authoring warning.
            refusals.append(
                "budget.max_provider_calls must be at least the expanded provider-call count"
            )
        if refusals:
            # Each expanded count names the path that realises it. On a
            # branching definition the bare number leaves an author to guess
            # which arm blew the ceiling, and the guess is wrong as often as
            # not -- the heaviest path is rarely the longest one.
            from cruxible_core.procedure.analysis import format_witness_path

            counts = (
                f"computed total_steps={expansion.total_steps}, "
                f"expanded_steps={expansion.expanded_steps} on path "
                f"{format_witness_path(expansion.expanded_steps_path)}, "
                f"expanded_provider_calls={expansion.expanded_provider_calls} on path "
                f"{format_witness_path(expansion.expanded_provider_calls_path)}, "
                f"declared max_provider_calls={self.budget.max_provider_calls}"
            )
            raise ValueError(f"procedure static expansion refused: {counts}; {'; '.join(refusals)}")
        return self

    def static_expansion(self) -> ProcedureStaticExpansion:
        """Return the maximum statically expanded step/provider counts.

        ``total_steps`` is a SUM over the stored body; the two expanded counts
        are longest-path MAXIMA (§3.3). Summing them under branching would
        charge one execution for work no execution does -- three mutually
        exclusive arms would each pay for the other two, and the budget
        ceiling would refuse a definition whose worst path is well inside it.

        The import is deferred because the analysis layer sits ON TOP of this
        module: it reads the step types and the control edges declared here.
        """
        from cruxible_core.procedure.analysis import worst_case_expansion

        total_steps = 0
        for wrapper in self.steps:
            step = unwrap_procedure_step(wrapper)
            total_steps += (
                1 + len(step.repeat.steps) if isinstance(step, ProcedureRepeatStepSchema) else 1
            )
        expansion = worst_case_expansion(self.steps)
        return ProcedureStaticExpansion(
            total_steps=total_steps,
            expanded_steps=expansion.expanded_steps.count,
            expanded_provider_calls=expansion.expanded_provider_calls.count,
            expanded_steps_path=expansion.expanded_steps.path,
            expanded_provider_calls_path=expansion.expanded_provider_calls.path,
        )

    def referenced_providers(self) -> set[str]:
        """Return every provider referenced at top level or inside repeat."""
        names: set[str] = set()
        for wrapper in self.steps:
            step = unwrap_procedure_step(wrapper)
            if isinstance(step, ProcedureRepeatStepSchema):
                names.update(
                    nested.provider for nested in step.repeat.steps if nested.provider is not None
                )
            elif isinstance(step, WorkflowStepSchema) and step.provider is not None:
                names.add(step.provider)
        return names


class ProcedureRecord(BaseModel):
    """Persisted immutable procedure definition plus governance state."""

    procedure_id: str = Field(default_factory=lambda: new_id("PRC"))
    definition: ProcedureDefinition
    definition_digest: str
    status: ProcedureStatus = "pending"
    version: int = Field(default=1, ge=1)
    supersedes_procedure_id: str | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    proposed_actor_context: GovernedActorContext | None
    proposed_at: datetime = Field(default_factory=utc_now)
    resolved_actor_context: GovernedActorContext | None = None
    resolved_at: datetime | None = None
    retired_actor_context: GovernedActorContext | None = None
    retired_at: datetime | None = None
    reason: str | None = None
    acceptance_config_digest: str | None = None
    acceptance_lock_digest: str | None = None
    definition_format_version: int = DEFINITION_FORMAT_V1
    """Which format the stored definition was parsed under.

    A non-``None`` default is safe HERE and only here: the record is not part
    of the definition digest, so emitting it moves no stored commitment. It is
    the record-level answer to "which verifier proves this row", which a reader
    must not have to re-derive from the definition it is about to trust.
    """

    model_config = ConfigDict(extra="forbid")


class ProcedureTrackRecord(BaseModel):
    """Run-ledger summary attached to procedure read records.

    The verdict buckets are EXHAUSTIVE over ``ProcedureRunVerdict`` plus
    ``in_flight`` for rows that have not been finalized, and the invariant
    below is enforced rather than documented. A partial set of buckets is worse
    than none: a procedure that blows its budget on every invocation would
    otherwise report the same ``runs`` with all-zero outcomes as a procedure
    with N invocations still running, and the dead one would read as busy.
    """

    runs: int = Field(default=0, ge=0)
    succeeded: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    refused: int = Field(default=0, ge=0)
    budget_exceeded: int = Field(default=0, ge=0)
    in_flight: int = Field(default=0, ge=0)
    last_succeeded_at: datetime | None = None
    top_refusal_reason: ProcedureRefusalReason | None = None
    linked_outcomes: None = None

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def validate_buckets_cover_every_run(self) -> ProcedureTrackRecord:
        bucketed = (
            self.succeeded + self.failed + self.refused + self.budget_exceeded + self.in_flight
        )
        if bucketed != self.runs:
            raise ValueError(
                "procedure track-record buckets must cover every run: "
                f"runs={self.runs}, bucketed={bucketed}"
            )
        return self


class ProcedureReadRecord(ProcedureRecord):
    """Procedure definition and governance state with its run-ledger summary."""

    track_record: ProcedureTrackRecord = Field(default_factory=ProcedureTrackRecord)

    @model_serializer(mode="wrap")
    def serialize_with_whole_track_record(
        self,
        handler: SerializerFunctionWrapHandler,
        info: SerializationInfo,
    ) -> dict[str, Any]:
        """Emit the track record whole, whatever the caller's dump options.

        Every read surface dumps procedures with ``exclude_none=True`` (the
        definition is full of optional keys nobody wants echoed as nulls), which
        would silently drop ``last_succeeded_at``/``top_refusal_reason``/
        ``linked_outcomes`` from the block and leave each surface to patch the
        shape back in by hand. The block is a fixed-shape summary: a missing key
        and a null key mean different things to a reader, so it is serialized
        here once instead of in every caller.
        """
        payload = dict(handler(self))
        payload["track_record"] = self.track_record.model_dump(
            mode="json" if info.mode_is_json() else "python"
        )
        return payload


def procedure_record_from_payload(payload: Any) -> ProcedureRecord:
    """Parse a procedure record from any surface's representation.

    Read surfaces carry ``track_record`` and lifecycle-transition surfaces do
    not, and both reach clients as plain dicts over HTTP and as models in
    process. Validating every payload as the bare :class:`ProcedureRecord`
    (``extra="forbid"``) rejects every read payload, so the record type is
    chosen from the payload itself.
    """
    if isinstance(payload, ProcedureRecord):
        return payload
    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="python")
    if not isinstance(payload, dict):
        raise TypeError(
            f"procedure payload must be a mapping or model, got {type(payload).__name__}"
        )
    record_type = ProcedureReadRecord if "track_record" in payload else ProcedureRecord
    return record_type.model_validate(payload)


class ProcedureContractFieldSchema(BaseModel):
    """Resolved construction hint for one procedure input field.

    ``required`` answers the only question a caller building an invocation has:
    must I supply this key? A field carrying a ``default`` is therefore *not*
    required -- contract validation fills the default before it ever checks
    whether the field was optional.
    """

    name: str
    type: PropertyType
    required: bool
    default: Any | None = None
    enum: list[Any] | None = None
    enum_ref: str | None = None
    description: str | None = None
    json_schema: dict[str, Any] | None = None
    """Nested JSON Schema a ``json``-typed field is additionally validated against.

    Without it a ``json`` field reads as "any JSON here" while the runtime still
    rejects payloads that miss the nested shape -- the exact gap this surface
    exists to close.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProcedureContractSchema(BaseModel):
    """Resolved ``contract_in`` shape one invocation payload must satisfy.

    ``allow_extra`` is part of the shape, not decoration: it is the only thing
    separating ``cruxible.EmptyInput`` (no fields, nothing else accepted) from
    ``cruxible.JsonObject`` (no declared fields, any object accepted). Without
    it both render as an empty field list and a caller cannot tell whether the
    procedure takes arbitrary input or none at all.

    ``input_example`` is the worked payload the field list otherwise leaves the
    caller to invent: every key they must supply, filled with a type-appropriate
    value. It is ``None`` only when the contract accepts no payload at all.
    """

    description: str | None = None
    fields: list[ProcedureContractFieldSchema]
    allow_extra: bool
    input_example: dict[str, Any] | None = None

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProcedureGetResult(BaseModel):
    """One procedure record plus its currently resolved input field schema.

    ``procedure`` is the read record, not the bare persisted one: the get
    surface carries the run-ledger ``track_record`` block, and annotating the
    base class here would serialize it away.
    """

    procedure: ProcedureReadRecord
    contract_in_schema: ProcedureContractSchema | None

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProcedureBudgetSpent(BaseModel):
    """Budget accounting persisted for one procedure invocation."""

    wall_clock_s: float = Field(default=0, ge=0)
    provider_calls: int = Field(default=0, ge=0)

    model_config = ConfigDict(extra="forbid")


class ProcedureRun(BaseModel):
    """Crash-visible procedure invocation record; execution lands after Stage A."""

    run_id: str = Field(default_factory=lambda: new_id("PRN"))
    procedure_id: str
    definition_digest: str
    status: ProcedureRunStatus = "started"
    verdict: ProcedureRunVerdict | None = None
    budget_spent: ProcedureBudgetSpent = Field(default_factory=ProcedureBudgetSpent)
    receipt_id: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    finalized_at: datetime | None = None
    refusal_reason: ProcedureRefusalReason | None = None
    """Bucket for a ``refused`` run. Null on every other verdict, and null on
    runs finalized before the column existed."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_status_and_verdict(self) -> ProcedureRun:
        if self.status == "started" and self.verdict is not None:
            raise ValueError("started procedure runs must have a null verdict")
        if self.status == "finalized" and self.verdict is None:
            raise ValueError("finalized procedure runs require a verdict")
        if self.refusal_reason is not None and self.verdict != "refused":
            raise ValueError("only refused procedure runs carry a refusal reason")
        return self


class ProcedureTransitionResult(BaseModel):
    """Service result for one receipted procedure lifecycle transition."""

    action: Literal["propose", "accept", "reject", "retire", "withdraw"]
    procedure: ProcedureRecord
    receipt_id: str | None = None
    warnings: list[str] = Field(default_factory=list)
    """DEPRECATED, removed in 0.5.0. Use ``typed_warnings``.

    Dual-emitted per ``dd-deprecation-policy`` class (3): the two lists carry
    the same findings in the same order, and the strings are DERIVED from the
    typed warnings rather than built beside them, so they cannot drift.

    No per-call deprecation notice is emitted. This is an output field, always
    populated, and nothing can observe whether a caller read it -- a notice on
    every propose would be noise on a surface the caller never asked for. The
    registry entry and the DEPRECATIONS.md row carry the schedule.
    """
    typed_warnings: list[ProcedureAuthoringWarning] = Field(default_factory=list)
    """The same findings as ``warnings``, with a code and the nodes involved."""


class ProcedureExecutionResult(BaseModel):
    """Successful result of one finalized procedure invocation."""

    procedure: ProcedureRecord
    run: ProcedureRun
    output: Any
    receipt: Receipt
    step_outputs: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)


class ProcedureEvidenceArtifact(BaseModel):
    """Chunkless digest-addressed typed JSON persisted from one run output."""

    artifact_id: str
    content_digest: str
    byte_count: int = Field(ge=0)
    payload: Any | None = None
    truncated_head: str | None = None
    oversized: bool = False
    created_at: datetime = Field(default_factory=utc_now)

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def validate_storage_shape(self) -> ProcedureEvidenceArtifact:
        if self.oversized:
            if self.payload is not None or self.truncated_head is None:
                raise ValueError("oversized procedure evidence stores only a truncated head")
        elif self.truncated_head is not None:
            raise ValueError("non-oversized procedure evidence cannot have a truncated head")
        return self


def compute_procedure_definition_digest(definition: ProcedureDefinition) -> str:
    """Return the stable content digest of one validated definition.

    The signature is UNCHANGED: one argument, no instance, no lock, no pins.
    That is what lets all five existing call sites -- propose, both run
    preflights, transition, and the store's round-trip verification -- keep
    working untouched while the dispatcher underneath grows a second format.

    The import is deferred because the digest layer sits ON TOP of this module:
    it reads the step types and the control graph declared here.
    """
    from cruxible_core.procedure.digest import compute_definition_digest

    return compute_definition_digest(definition)


def _step_output_alias(step: Any) -> str | None:
    """Return the alias a node publishes, or ``None`` when it publishes nothing.

    A guard publishes nothing: it is a decision point, not a producer.
    """
    if isinstance(step, ProcedureGuardStepSchema):
        return None
    inner = unwrap_procedure_step(step)
    alias = getattr(inner, "as_", None)
    return alias or str(inner.id)


def _validate_unique_step_ids(
    steps: list[WorkflowStepSchema] | list[ProcedureStepSchema],
    *,
    context: str,
) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for step in steps:
        if step.id in seen:
            duplicates.add(step.id)
        seen.add(step.id)
    if duplicates:
        raise ValueError(f"{context} contain duplicate step id(s): {sorted(duplicates)}")


def _workflow_references(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.startswith("$") else []
    if isinstance(value, dict):
        return [ref for item in value.values() for ref in _workflow_references(item)]
    if isinstance(value, list):
        return [ref for item in value for ref in _workflow_references(item)]
    return []


__all__ = [
    "MAX_PROCEDURE_BRANCH_NODES",
    "MAX_PROCEDURE_ENUMERATED_PATHS",
    "MAX_PROCEDURE_EXPANDED_PROVIDER_CALLS",
    "MAX_PROCEDURE_EXPANDED_STEPS",
    "MAX_PROCEDURE_EVIDENCE_BYTES",
    "MAX_PROCEDURE_REPEAT_ATTEMPTS",
    "MAX_PROCEDURE_STEPS",
    "PROCEDURE_EVIDENCE_HEAD_BYTES",
    "ProcedureAuthoringWarning",
    "ProcedureBudget",
    "ProcedureBudgetSpent",
    "ProcedureDefinition",
    "ProcedureContractFieldSchema",
    "ProcedureContractSchema",
    "ProcedureEvidenceArtifact",
    "ProcedureExecutionResult",
    "ProcedureFlowStepSchema",
    "ProcedureGetResult",
    "ProcedureGuardStepSchema",
    "ProcedureInnerStep",
    "ProcedureProjectStepSchema",
    "ProcedurePrecondition",
    "ProcedureReadRecord",
    "ProcedureRecord",
    "ProcedureRefusalReason",
    "ProcedureRepeatSpec",
    "ProcedureRepeatStepSchema",
    "ProcedureRun",
    "ProcedureRunStatus",
    "ProcedureRunVerdict",
    "ProcedureStaticExpansion",
    "ProcedureStatus",
    "ProcedureStepSchema",
    "ProcedureTier",
    "ProcedureTrackRecord",
    "ProcedureTransitionResult",
    "ProjectSpec",
    "compute_procedure_definition_digest",
    "procedure_record_from_payload",
    "unwrap_procedure_step",
]
