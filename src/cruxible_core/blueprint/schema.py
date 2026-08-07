"""Pydantic models for the procedure-blueprint document format.

A blueprint is a *container* around objects core already understands: its
contracts are :class:`~cruxible_core.config.schema.ContractSchema` bodies, its
query-slot defaults are :class:`~cruxible_core.config.schema.NamedQuerySchema`
bodies, and its procedures are
:class:`~cruxible_core.procedure.types.ProcedureDefinition` bodies. The
blueprint layer adds identity, dependencies, slots (the swap points), and the
digest -- it never re-implements the object schemas.

Phase 1 scope (RFC "Procedure Blueprints" v0.3, wi-038):

* ``invocation: manual`` procedures are the executable slice.
* ``triggers`` and ``pipelines`` are *parsed and validated* so publishers can
  author the whole artifact and catalogs/visualizers can read it, but lowering
  refuses them -- core has no trigger runtime (wi-034).
* There is no installer here. Lowering produces the artifacts an installer
  would submit; applying them is wi-043.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from cruxible_core.blueprint.errors import BillingMode, BlueprintIssue
from cruxible_core.config.schema import (
    BUILTIN_CONTRACTS,
    ContractSchema,
    NamedQuerySchema,
    WorkflowStepSchema,
)
from cruxible_core.procedure.types import (
    ProcedureDefinition,
    ProcedureRepeatStepSchema,
    unwrap_procedure_step,
)

BLUEPRINT_FORMAT_VERSION = "1"
"""Format generation folded into the digest preimage.

A future incompatible format change bumps this, so a v1 document and a v2
document can never collide on a digest even if their canonical bodies match.
"""

TRIGGER_WORK_ITEM = "wi-034"
"""Work item that owns the trigger/pipeline runtime the format already names."""

INSTALLER_WORK_ITEM = "wi-043"
"""Work item that owns the installer consuming :mod:`.lowering` output."""

InvocationMode = Literal["manual", "triggered"]
ProvenanceOrigin = Literal["agent-authored", "curated", "hybrid"]
TriggerKind = Literal["artifact", "webhook", "schedule"]
EnumOrdering = Literal["low_to_high", "high_to_low"]

KNOWN_OUTCOME_METRICS = ("brier", "log_loss", "precision_recall", "accuracy")
"""Metrics the provable-ness ladder (RFC §6) can score today.

Declaring the hook publishes nothing; it opts a slot into scoring once the
outcome-forcing primitive lands.
"""

PROVENANCE_EVIDENCE_KINDS = ("receipt", "eval", "artifact", "url")
"""Ref kinds accepted in ``provenance.evidence``.

RFC §3 spells evidence as a bare comment (``[receipt refs, eval refs]``), which
makes an empty list indistinguishable from an unparseable one. Phase 1 pins a
minimal ``<kind>:<value>`` grammar so a catalog can tell a claim from a blank.
"""

# Mirrors config.compact's explicit-query escape hatch. Blueprint query-slot
# defaults must be authored in the explicit engine schema, because compact
# expansion resolves against the *deployed* ontology index (entity primary
# keys, relationship directions) that a portable document cannot carry.
_EXPLICIT_QUERY_MARKER = "explicit"
_COMPACT_ONLY_QUERY_KEYS = (
    "traverse",
    "traverse_all",
    "bound",
    "order",
    "as",
    "max_depth",
    "direction",
)

_SEMVER_RE = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_CATALOG_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")
_SLOT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_LOCAL_CONTRACT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_STATE_REF_PART_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_VERSION_RANGE_CHARS = frozenset("><=~^*, ")


def _issue(path: str, message: str, expected: str | None = None) -> BlueprintIssue:
    return BlueprintIssue(path=path, message=message, expected=expected)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


class BlueprintProvenance(BaseModel):
    """Where a blueprint came from, and what backs the claim."""

    origin: ProvenanceOrigin
    evidence: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_evidence_refs(self) -> BlueprintProvenance:
        kinds = ", ".join(PROVENANCE_EVIDENCE_KINDS)
        for ref in self.evidence:
            kind, sep, value = ref.partition(":")
            if not sep or not value.strip() or kind not in PROVENANCE_EVIDENCE_KINDS:
                raise ValueError(
                    f"provenance evidence ref '{ref}' must be '<kind>:<value>' "
                    f"with kind one of: {kinds}"
                )
        return self


class BlueprintMetadata(BaseModel):
    """Catalog identity for one blueprint."""

    id: str
    version: str
    publisher: str
    description: str | None = None
    provenance: BlueprintProvenance | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("id", "publisher")
    @classmethod
    def validate_catalog_identity(cls, value: str, info: ValidationInfo) -> str:
        if not _CATALOG_ID_RE.fullmatch(value):
            raise ValueError(
                f"blueprint {info.field_name} '{value}' must match {_CATALOG_ID_RE.pattern} "
                "(lowercase catalog identity, e.g. 'kev-triage')"
            )
        return value

    @field_validator("version")
    @classmethod
    def validate_semver(cls, value: str) -> str:
        if not _SEMVER_RE.fullmatch(value):
            raise ValueError(
                f"blueprint version '{value}' must be semver MAJOR.MINOR.PATCH "
                "(e.g. '1.0.0'); quote it in YAML so it stays a string"
            )
        return value

    @property
    def contract_namespace(self) -> str:
        """Return the mandatory prefix for every blueprint-declared contract."""
        return f"{self.publisher}.{self.id}."


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


class ReferenceStateDependency(BaseModel):
    """A published reference state this blueprint reads through.

    RFC §3 shows ``{alias: kev-reference, version: ">=2026.30"}``. Version
    *ranges* do not parse in core today -- ``kits/state_refs.py`` accepts
    ``alias`` or ``alias@release`` with both parts in ``[A-Za-z0-9._-]+`` --
    so phase 1 accepts exact refs only and refuses ranges by name.
    """

    state_ref: str | None = None
    alias: str | None = None
    version: str | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_ref(self) -> ReferenceStateDependency:
        if (self.state_ref is None) == (self.alias is None):
            raise ValueError(
                "reference-state dependency must declare exactly one of "
                "'state_ref' (e.g. 'kev-reference@2026.30') or 'alias'"
            )
        raw = self.state_ref if self.state_ref is not None else self.alias
        assert raw is not None
        alias, sep, release = raw.partition("@")
        if not alias.strip() or (sep and not release.strip()):
            raise ValueError(f"reference-state ref '{raw}' must be 'alias' or 'alias@release'")
        for label, part in (("alias", alias), ("release", release)):
            if part and not _STATE_REF_PART_RE.fullmatch(part):
                raise ValueError(
                    f"reference-state {label} '{part}' must match {_STATE_REF_PART_RE.pattern}"
                )
        if self.version is not None:
            if set(self.version) & _VERSION_RANGE_CHARS:
                raise ValueError(
                    f"reference-state version '{self.version}' looks like a range; "
                    "version ranges do not parse in core today (RFC §11.6). Pin an "
                    "exact release: state_ref: '<alias>@<release>'"
                )
            if not _STATE_REF_PART_RE.fullmatch(self.version):
                raise ValueError(
                    f"reference-state version '{self.version}' must match "
                    f"{_STATE_REF_PART_RE.pattern}"
                )
            if release:
                raise ValueError(
                    "declare the release once: use either 'alias@release' or a separate "
                    "'version', not both"
                )
        return self

    @property
    def resolved_ref(self) -> str:
        """Return the exact ``alias`` or ``alias@release`` core can resolve."""
        raw = self.state_ref if self.state_ref is not None else self.alias
        assert raw is not None
        if self.version is not None:
            return f"{raw}@{self.version}"
        return raw


class EnumDependency(BaseModel):
    """An instance-owned enum the blueprint's queries order by."""

    name: str
    ordered: EnumOrdering | None = None

    model_config = ConfigDict(extra="forbid")


class KitDependency(BaseModel):
    """An ontology overlay the blueprint composes with.

    The common case for a kit-composing blueprint is *not* a published
    reference state but a kit someone else maintains; RFC §3's dependency block
    named only the former.
    """

    kit_id: str
    min_version: str | None = None

    model_config = ConfigDict(extra="forbid")


class BlueprintDependencies(BaseModel):
    """What the target instance must already have for this blueprint to run."""

    reference_states: list[ReferenceStateDependency] = Field(default_factory=list)
    entity_types: list[str] = Field(default_factory=list)
    relationship_types: list[str] = Field(default_factory=list)
    enums: list[EnumDependency] = Field(default_factory=list)
    kits: list[KitDependency] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------


class OutcomeMetricHook(BaseModel):
    """Opt-in binding to the provable-ness ladder's outcome-scored rung.

    RFC §6 spells the hook as ``{contract: ImpactOutcome, metric: brier}``. The
    scoring machinery kits actually have is an *outcome profile* (a named config
    object anchored to resolutions, with categorical outcome codes), so phase 1
    accepts ``outcome_profile`` as well and requires exactly one target.
    """

    outcome_profile: str | None = None
    contract: str | None = None
    metric: str

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_hook(self) -> OutcomeMetricHook:
        if (self.outcome_profile is None) == (self.contract is None):
            raise ValueError(
                "outcome_metric must declare exactly one of 'outcome_profile' "
                "(preferred; names an existing config outcome profile) or 'contract'"
            )
        if self.metric not in KNOWN_OUTCOME_METRICS:
            allowed = ", ".join(KNOWN_OUTCOME_METRICS)
            raise ValueError(f"outcome_metric metric '{self.metric}' must be one of: {allowed}")
        return self


class ComputeSlot(BaseModel):
    """A swappable compute stage: typed in, typed out, never a data read.

    ``billing`` declares *compatibility constraints* only. Real billing facts
    (pricing, payer, quota, account) live on the install-time binding record and
    its receipts, so they cannot reach the portable digest (RFC §10.3).

    Every constraint here is enforced at lowering against the bound
    :class:`~cruxible_core.blueprint.errors.BlueprintSlotCandidate`: contract
    names must match exactly, the candidate's billing modes must intersect
    ``billing``, and the candidate must claim every tag in ``capabilities``.
    """

    description: str | None = None
    contract_in: str
    contract_out: str
    billing: list[BillingMode] = Field(min_length=1)
    capabilities: list[str] = Field(default_factory=list)
    required: bool = True
    outcome_metric: OutcomeMetricHook | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_slot(self) -> ComputeSlot:
        if len(set(self.billing)) != len(self.billing):
            raise ValueError("billing modes must not repeat")
        for capability in self.capabilities:
            if not _CAPABILITY_RE.fullmatch(capability):
                raise ValueError(
                    f"capability tag '{capability}' must match {_CAPABILITY_RE.pattern}"
                )
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capability tags must not repeat")
        return self


class QuerySlot(BaseModel):
    """A read socket: pinned interface, config-resident implementation.

    ``default`` installs as a named query in the deployed config and is the
    post-install customization point. ``row_contract`` documents the row shape
    the pipeline expects; core cannot enforce it yet, because a query step's
    output is the engine envelope and ``results`` is an opaque list -- so
    ``result_contract`` can only type the envelope today.
    """

    description: str | None = None
    install_as: str | None = None
    param_contract: str
    result_contract: str
    row_contract: str | None = None
    default: NamedQuerySchema

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def strip_explicit_marker(cls, data: Any) -> Any:
        """Accept (and drop) the ``explicit: true`` opt-in on a default body.

        Kit queries are authored in compact configs and carry the marker; the
        marker lets a publisher paste an explicit body in verbatim.
        """
        if not isinstance(data, dict):
            return data
        default = data.get("default")
        if not isinstance(default, dict):
            return data
        present = [key for key in _COMPACT_ONLY_QUERY_KEYS if key in default]
        if present:
            raise ValueError(
                f"query-slot default contains compact-grammar key(s) {sorted(present)}; "
                "blueprint defaults must be authored in the explicit engine schema "
                "(compact expansion needs the deployed ontology index, which a "
                "portable blueprint does not carry)"
            )
        if _EXPLICIT_QUERY_MARKER in default:
            body = dict(data)
            stripped = dict(default)
            stripped.pop(_EXPLICIT_QUERY_MARKER)
            body["default"] = stripped
            return body
        return data

    def installed_name(self, slot_name: str) -> str:
        """Return the config named-query key this slot's default installs as."""
        return self.install_as or slot_name


# ---------------------------------------------------------------------------
# Procedures, pipelines, triggers
# ---------------------------------------------------------------------------


class _ProcedureBody(BaseModel):
    """Shared wrapper carrying one ``ProcedureDefinition`` plus blueprint keys."""

    definition: ProcedureDefinition

    model_config = ConfigDict(extra="forbid")

    @property
    def name(self) -> str:
        return self.definition.name

    def referenced_query_names(self) -> list[str]:
        """Return named-query references, in step order (inline bodies excluded)."""
        names: list[str] = []
        for step in self.definition.steps:
            if isinstance(step, WorkflowStepSchema) and isinstance(step.query, str):
                names.append(step.query)
        return names

    def referenced_provider_names(self) -> list[str]:
        """Return provider references at top level and inside repeats, in step order."""
        names: list[str] = []
        for wrapper in self.definition.steps:
            step = unwrap_procedure_step(wrapper)
            if isinstance(step, ProcedureRepeatStepSchema):
                names.extend(
                    nested.provider for nested in step.repeat.steps if nested.provider is not None
                )
            elif isinstance(step, WorkflowStepSchema) and step.provider is not None:
                names.append(step.provider)
        return names


class BlueprintProcedure(_ProcedureBody):
    """An agent-invoked procedure. ``invocation`` defaults to the document's."""

    invocation: InvocationMode | None = None

    @model_validator(mode="before")
    @classmethod
    def split_body(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "definition" in data:
            return data
        body = deepcopy(data)
        invocation = body.pop("invocation", None)
        return {"invocation": invocation, "definition": body}


class BlueprintPipeline(_ProcedureBody):
    """A trigger-invoked procedure body. Parsed today; not executable (wi-034)."""

    @model_validator(mode="before")
    @classmethod
    def split_body(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "definition" in data:
            return data
        return {"definition": deepcopy(data)}


class TriggerSchema(BaseModel):
    """A declared entry point. Parsed today; there is no trigger runtime."""

    kind: TriggerKind
    pipeline: str
    accepts: list[str] | None = None
    contract_in: str | None = None
    schedule: str | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_kind_fields(self) -> TriggerSchema:
        required: dict[TriggerKind, str] = {
            "artifact": "accepts",
            "webhook": "contract_in",
            "schedule": "schedule",
        }
        needed = required[self.kind]
        if getattr(self, needed) in (None, []):
            raise ValueError(f"'{self.kind}' triggers require '{needed}'")
        for kind, field_name in required.items():
            if kind != self.kind and getattr(self, field_name) not in (None, []):
                raise ValueError(
                    f"'{self.kind}' triggers may not declare '{field_name}' "
                    f"(that field belongs to '{kind}' triggers)"
                )
        return self


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


class Blueprint(BaseModel):
    """One parsed, shape-validated blueprint document.

    Shape validation lives on the models; *cross-reference* validation (do
    contract refs resolve, do provider steps name declared slots) lives in
    :func:`cross_reference_issues`, so an author gets every problem at once
    instead of one per parse.
    """

    blueprint: BlueprintMetadata
    contracts: dict[str, ContractSchema] = Field(default_factory=dict)
    dependencies: BlueprintDependencies = Field(default_factory=BlueprintDependencies)
    query_slots: dict[str, QuerySlot] = Field(default_factory=dict)
    slots: dict[str, ComputeSlot] = Field(default_factory=dict)
    invocation: InvocationMode = "manual"
    triggers: dict[str, TriggerSchema] = Field(default_factory=dict)
    pipelines: list[BlueprintPipeline] = Field(default_factory=list)
    procedures: list[BlueprintProcedure] = Field(default_factory=list)
    install_checks: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @property
    def id(self) -> str:
        return self.blueprint.id

    @property
    def version(self) -> str:
        return self.blueprint.version

    @property
    def publisher(self) -> str:
        return self.blueprint.publisher

    @property
    def coordinate(self) -> str:
        """Return the catalog coordinate ``publisher/id@version``."""
        return f"{self.publisher}/{self.id}@{self.version}"

    def procedure_invocation(self, procedure: BlueprintProcedure) -> InvocationMode:
        """Return the effective invocation mode for one procedure."""
        return procedure.invocation or self.invocation

    def declared_contract_names(self) -> set[str]:
        """Return blueprint-declared plus builtin contract names."""
        return set(self.contracts) | set(BUILTIN_CONTRACTS)


def cross_reference_issues(blueprint: Blueprint) -> list[BlueprintIssue]:
    """Return every cross-reference problem in a shape-valid document.

    Checks, in report order: contract namespacing, contract-reference
    resolution, slot naming, installed-query-name collisions, procedure and
    pipeline provider/query references, and trigger targets.
    """
    issues: list[BlueprintIssue] = []
    namespace = blueprint.blueprint.contract_namespace
    known_contracts = blueprint.declared_contract_names()

    issues.extend(_contract_name_issues(blueprint, namespace))
    issues.extend(_slot_issues(blueprint, known_contracts))
    issues.extend(_query_slot_issues(blueprint, known_contracts))
    issues.extend(_body_issues(blueprint, known_contracts))
    issues.extend(_trigger_issues(blueprint))

    if not blueprint.procedures and not blueprint.pipelines:
        issues.append(
            _issue(
                "procedures",
                "blueprint declares no procedures and no pipelines, so it installs nothing",
                "at least one entry under 'procedures' or 'pipelines'",
            )
        )
    for index, check in enumerate(blueprint.install_checks):
        if not check.strip():
            issues.append(
                _issue(f"install_checks[{index}]", "install check name must be non-empty")
            )
    return issues


def _contract_name_issues(blueprint: Blueprint, namespace: str) -> list[BlueprintIssue]:
    issues: list[BlueprintIssue] = []
    for name in blueprint.contracts:
        path = f"contracts.{name}"
        if not name.startswith(namespace):
            issues.append(
                _issue(
                    path,
                    f"contract name '{name}' is not fully qualified for this blueprint",
                    f"'{namespace}<LocalName>' (RFC §10.1: authored fully qualified, "
                    "refuse on collision)",
                )
            )
            continue
        local = name[len(namespace) :]
        if not _LOCAL_CONTRACT_RE.fullmatch(local):
            issues.append(
                _issue(
                    path,
                    f"local contract name '{local}' is not a single valid segment",
                    f"'{namespace}<LocalName>' where LocalName matches "
                    f"{_LOCAL_CONTRACT_RE.pattern}",
                )
            )
    return issues


def _contract_ref_issue(path: str, ref: str, known: set[str]) -> BlueprintIssue | None:
    if ref in known:
        return None
    return _issue(
        path,
        f"contract reference '{ref}' is not declared by this blueprint and is not a builtin",
        "one of: " + ", ".join(sorted(known)),
    )


def _slot_issues(blueprint: Blueprint, known_contracts: set[str]) -> list[BlueprintIssue]:
    issues: list[BlueprintIssue] = []
    for slot_name, slot in blueprint.slots.items():
        path = f"slots.{slot_name}"
        if not _SLOT_NAME_RE.fullmatch(slot_name):
            issues.append(
                _issue(
                    path,
                    f"slot name '{slot_name}' is not a valid identifier",
                    f"a name matching {_SLOT_NAME_RE.pattern}",
                )
            )
        for field_name in ("contract_in", "contract_out"):
            issue = _contract_ref_issue(
                f"{path}.{field_name}", getattr(slot, field_name), known_contracts
            )
            if issue is not None:
                issues.append(issue)
    return issues


def _query_slot_issues(blueprint: Blueprint, known_contracts: set[str]) -> list[BlueprintIssue]:
    issues: list[BlueprintIssue] = []
    installed: dict[str, str] = {}
    for slot_name, slot in blueprint.query_slots.items():
        path = f"query_slots.{slot_name}"
        if not _SLOT_NAME_RE.fullmatch(slot_name):
            issues.append(
                _issue(
                    path,
                    f"query slot name '{slot_name}' is not a valid identifier",
                    f"a name matching {_SLOT_NAME_RE.pattern}",
                )
            )
        if slot_name in blueprint.slots:
            issues.append(
                _issue(
                    path,
                    f"'{slot_name}' is declared as both a query slot and a compute slot",
                    "distinct names: procedure steps resolve slot references by name",
                )
            )
        for field_name in ("param_contract", "result_contract", "row_contract"):
            ref = getattr(slot, field_name)
            if ref is None:
                continue
            issue = _contract_ref_issue(f"{path}.{field_name}", ref, known_contracts)
            if issue is not None:
                issues.append(issue)
        installed_name = slot.installed_name(slot_name)
        if not _SLOT_NAME_RE.fullmatch(installed_name):
            issues.append(
                _issue(
                    f"{path}.install_as",
                    f"installed query name '{installed_name}' is not a valid identifier",
                    f"a name matching {_SLOT_NAME_RE.pattern}",
                )
            )
        if installed_name in installed:
            issues.append(
                _issue(
                    f"{path}.install_as",
                    f"installed query name '{installed_name}' collides with query slot "
                    f"'{installed[installed_name]}'",
                    "a unique installed name per query slot (composition refuses "
                    "keyed-map redefinition)",
                )
            )
        else:
            installed[installed_name] = slot_name
    return issues


def _body_issues(blueprint: Blueprint, known_contracts: set[str]) -> list[BlueprintIssue]:
    issues: list[BlueprintIssue] = []
    groups: list[tuple[str, list[BlueprintProcedure] | list[BlueprintPipeline]]] = [
        ("procedures", blueprint.procedures),
        ("pipelines", blueprint.pipelines),
    ]
    seen: dict[str, str] = {}
    for section, bodies in groups:
        for index, body in enumerate(bodies):
            path = f"{section}[{index}]"
            name = body.name
            if name in seen:
                issues.append(
                    _issue(
                        f"{path}.name",
                        f"procedure name '{name}' is already declared at {seen[name]}",
                        "one live definition per name is core law; rename or merge",
                    )
                )
            else:
                seen[name] = path
            definition = body.definition
            for field_name in ("contract_in", "contract_out"):
                ref = getattr(definition, field_name)
                if not isinstance(ref, str):
                    continue
                issue = _contract_ref_issue(f"{path}.{field_name}", ref, known_contracts)
                if issue is not None:
                    issues.append(issue)
            for provider in body.referenced_provider_names():
                if provider not in blueprint.slots:
                    issues.append(
                        _issue(
                            f"{path}.steps",
                            f"provider step references '{provider}', which is not a "
                            "declared compute slot",
                            "one of the declared slots: "
                            + (", ".join(sorted(blueprint.slots)) or "(none declared)")
                            + " — blueprint procedures name slots, never concrete providers",
                        )
                    )
            for query_name in body.referenced_query_names():
                if query_name not in blueprint.query_slots:
                    issues.append(
                        _issue(
                            f"{path}.steps",
                            f"query step references named query '{query_name}', which is "
                            "not a declared query slot",
                            "a declared query slot ("
                            + (", ".join(sorted(blueprint.query_slots)) or "none declared")
                            + "), or an inline query body for internal plumbing reads",
                        )
                    )
    return issues


def _trigger_issues(blueprint: Blueprint) -> list[BlueprintIssue]:
    issues: list[BlueprintIssue] = []
    pipeline_names = {pipeline.name for pipeline in blueprint.pipelines}
    for trigger_name, trigger in blueprint.triggers.items():
        path = f"triggers.{trigger_name}"
        if trigger.pipeline not in pipeline_names:
            issues.append(
                _issue(
                    f"{path}.pipeline",
                    f"trigger targets pipeline '{trigger.pipeline}', which is not declared",
                    "one of: " + (", ".join(sorted(pipeline_names)) or "(no pipelines declared)"),
                )
            )
        if trigger.contract_in is not None:
            issue = _contract_ref_issue(
                f"{path}.contract_in", trigger.contract_in, blueprint.declared_contract_names()
            )
            if issue is not None:
                issues.append(issue)
    return issues
