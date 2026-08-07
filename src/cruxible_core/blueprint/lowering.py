"""Lower a blueprint into the artifacts an installer submits.

Phase 1 stops at the artifacts. This module produces:

* a **config-overlay fragment** -- the blueprint's contracts plus its
  query-slot defaults as named queries, keyed by their installed names; and
* a list of **ProcedureDefinitions** with slot references resolved to concrete
  provider names and installed query names.

Applying either one is the installer's job (wi-043): overlay composition,
propose/accept, binding records, and install receipts all live there. Nothing in
this module touches config, state, or governance.

Slot matching is **nominal** in phase 1: a bound provider's declared contract
names must equal the slot's. That is deliberately strict -- it never binds
across mismatched types -- but it means a structurally identical provider under
a different contract name cannot bind. Structural (width-subtyping) matching is
the recommended follow-up; it needs a contract-compatibility relation core does
not have yet.

Resolution is **fail-closed**. ``candidates`` is not a hint: it is the catalog
lowering checks against, and a binding naming a provider that is not in it is
refused. Lowering emits a ``ResolvedSlotBinding`` -- a document that later
governance reads as "this provider satisfies this interface" -- so it may only
emit one for a provider whose declared contracts, billing modes, and
capabilities it has actually seen and checked.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from cruxible_core.blueprint.errors import (
    BlueprintBindingError,
    BlueprintIssue,
    BlueprintSlotCandidate,
    BlueprintUnsupportedError,
    BlueprintValidationError,
)
from cruxible_core.blueprint.schema import (
    INSTALLER_WORK_ITEM,
    TRIGGER_WORK_ITEM,
    Blueprint,
    ComputeSlot,
)
from cruxible_core.config.schema import ContractSchema, NamedQuerySchema
from cruxible_core.procedure.types import ProcedureDefinition

ProviderCandidate = BlueprintSlotCandidate


class ConfigOverlayFragment(BaseModel):
    """The additive config objects an install would compose onto the instance."""

    contracts: dict[str, ContractSchema] = Field(default_factory=dict)
    named_queries: dict[str, NamedQuerySchema] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    def as_config_dict(self) -> dict[str, Any]:
        """Return the fragment as a plain, composable config-shaped mapping."""
        return {
            "contracts": {
                name: contract.model_dump(mode="json", by_alias=True, exclude_none=True)
                for name, contract in self.contracts.items()
            },
            "named_queries": {
                name: query.model_dump(mode="json", by_alias=True, exclude_none=True)
                for name, query in self.named_queries.items()
            },
        }


class ResolvedSlotBinding(BaseModel):
    """One compute slot resolved to a concrete provider name."""

    slot: str
    provider: str
    contract_in: str
    contract_out: str

    model_config = ConfigDict(extra="forbid", frozen=True)


class LoweredBlueprint(BaseModel):
    """Everything an installer needs, and nothing it would have to re-derive."""

    blueprint_id: str
    publisher: str
    version: str
    digest: str | None = None
    overlay: ConfigOverlayFragment
    procedures: list[ProcedureDefinition]
    slot_bindings: list[ResolvedSlotBinding]
    query_slot_installs: dict[str, str]

    model_config = ConfigDict(extra="forbid")

    @property
    def coordinate(self) -> str:
        return f"{self.publisher}/{self.blueprint_id}@{self.version}"


def lower_blueprint(
    blueprint: Blueprint,
    *,
    bindings: Mapping[str, str] | None = None,
    candidates: Iterable[ProviderCandidate] = (),
    digest: str | None = None,
) -> LoweredBlueprint:
    """Lower ``blueprint`` against a caller-supplied slot->provider binding map.

    ``candidates`` is the provider catalog every binding is checked against. A
    binding naming a provider absent from it is refused: an unchecked provider
    cannot be certified as satisfying the slot. The catalog also explains
    failures -- an unbindable slot reports the candidates that *nearly* matched
    and why each one was rejected.
    """
    _refuse_unsupported(blueprint)
    binding_map = dict(bindings or {})
    catalog = list(candidates)
    _refuse_unknown_binding_targets(blueprint, binding_map)

    referenced = _referenced_slots(blueprint)
    resolved = _resolve_slots(blueprint, binding_map, catalog, referenced)
    query_installs = {
        slot_name: slot.installed_name(slot_name)
        for slot_name, slot in blueprint.query_slots.items()
    }
    provider_map = {binding.slot: binding.provider for binding in resolved}

    procedures = [
        _rewrite_definition(body.definition, provider_map, query_installs)
        for body in blueprint.procedures
    ]
    overlay = ConfigOverlayFragment(
        contracts=dict(blueprint.contracts),
        named_queries={
            query_installs[slot_name]: slot.default
            for slot_name, slot in blueprint.query_slots.items()
        },
    )
    return LoweredBlueprint(
        blueprint_id=blueprint.id,
        publisher=blueprint.publisher,
        version=blueprint.version,
        digest=digest,
        overlay=overlay,
        procedures=procedures,
        slot_bindings=resolved,
        query_slot_installs=query_installs,
    )


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def _refuse_unsupported(blueprint: Blueprint) -> None:
    if blueprint.triggers:
        raise BlueprintUnsupportedError(
            "triggers",
            work_item=TRIGGER_WORK_ITEM,
            detail=(
                f"Declared triggers: {sorted(blueprint.triggers)}. No trigger, webhook, or "
                "schedule surface exists in core: procedures start only through the explicit "
                "run service. Remove the triggers block to lower the manual procedures, or "
                f"wait for {TRIGGER_WORK_ITEM}."
            ),
        )
    if blueprint.pipelines:
        raise BlueprintUnsupportedError(
            "pipelines",
            work_item=TRIGGER_WORK_ITEM,
            detail=(
                f"Declared pipelines: {[body.name for body in blueprint.pipelines]}. Pipelines "
                "are trigger-invoked by definition and have no entry point to lower onto. "
                "Move a pipeline under 'procedures' to run it manually."
            ),
        )
    if blueprint.invocation != "manual":
        raise BlueprintUnsupportedError(
            f"invocation: {blueprint.invocation}",
            work_item=TRIGGER_WORK_ITEM,
            detail="Only 'manual' invocation lowers today.",
        )
    for index, body in enumerate(blueprint.procedures):
        mode = blueprint.procedure_invocation(body)
        if mode != "manual":
            raise BlueprintUnsupportedError(
                f"invocation: {mode}",
                work_item=TRIGGER_WORK_ITEM,
                detail=(
                    f"procedures[{index}] ('{body.name}') declares invocation '{mode}'. "
                    "Only 'manual' invocation lowers today."
                ),
            )


def _refuse_unknown_binding_targets(blueprint: Blueprint, binding_map: Mapping[str, str]) -> None:
    issues: list[BlueprintIssue] = []
    for slot_name, provider in binding_map.items():
        if slot_name not in blueprint.slots:
            issues.append(
                BlueprintIssue(
                    path=f"bindings.{slot_name}",
                    message=f"'{slot_name}' is not a compute slot declared by this blueprint",
                    expected="one of: "
                    + (", ".join(sorted(blueprint.slots)) or "(no compute slots declared)"),
                )
            )
        elif not provider or not provider.strip():
            issues.append(
                BlueprintIssue(
                    path=f"bindings.{slot_name}",
                    message="provider name must be a non-empty string",
                    expected="the name of a provider registered in the target config",
                )
            )
    if issues:
        raise BlueprintValidationError(
            f"Binding map for blueprint '{blueprint.coordinate}' is invalid", issues
        )


# ---------------------------------------------------------------------------
# Slot resolution
# ---------------------------------------------------------------------------


def _referenced_slots(blueprint: Blueprint) -> set[str]:
    referenced: set[str] = set()
    for body in blueprint.procedures:
        referenced.update(body.referenced_provider_names())
    return referenced


def _resolve_slots(
    blueprint: Blueprint,
    binding_map: Mapping[str, str],
    catalog: Sequence[ProviderCandidate],
    referenced: set[str],
) -> list[ResolvedSlotBinding]:
    by_name = {candidate.name: candidate for candidate in catalog}
    resolved: list[ResolvedSlotBinding] = []
    for slot_name in sorted(blueprint.slots):
        slot = blueprint.slots[slot_name]
        needed = slot.required or slot_name in referenced
        provider = binding_map.get(slot_name)
        if provider is None:
            if not needed:
                continue
            raise BlueprintBindingError(
                slot_name,
                contract_in=slot.contract_in,
                contract_out=slot.contract_out,
                reason=(
                    "no binding was supplied for a required slot"
                    if slot.required
                    else "no binding was supplied, and a procedure step references the slot"
                ),
                near_matches=_near_matches(slot, catalog),
            )
        candidate = by_name.get(provider)
        if candidate is None:
            raise BlueprintBindingError(
                slot_name,
                contract_in=slot.contract_in,
                contract_out=slot.contract_out,
                reason=(
                    f"bound provider '{provider}' is not among the {len(catalog)} candidate "
                    "provider(s) offered to the binder, so nothing declares what it accepts "
                    "or returns; lowering will not certify a provider it has never seen"
                ),
                issues=[
                    BlueprintIssue(
                        path=f"bindings.{slot_name}",
                        message=(f"provider '{provider}' is absent from the candidate catalog"),
                        expected=_expected_provider_names(catalog),
                    )
                ],
                near_matches=_near_matches(slot, catalog),
            )
        issues = _compatibility_issues(slot_name, slot, candidate)
        if issues:
            raise BlueprintBindingError(
                slot_name,
                contract_in=slot.contract_in,
                contract_out=slot.contract_out,
                reason=(
                    f"bound provider '{provider}' does not satisfy the slot interface: "
                    + "; ".join(_reasons(slot, candidate))
                ),
                issues=issues,
                near_matches=_near_matches(slot, catalog),
            )
        resolved.append(
            ResolvedSlotBinding(
                slot=slot_name,
                provider=provider,
                contract_in=slot.contract_in,
                contract_out=slot.contract_out,
            )
        )
    return resolved


def _expected_provider_names(catalog: Sequence[ProviderCandidate]) -> str:
    if not catalog:
        return (
            "one of the candidate provider names, but candidates=[] was passed: "
            "offer the provider catalog to lower a binding"
        )
    return "one of: " + ", ".join(sorted(candidate.name for candidate in catalog))


def _contract_mismatches(slot: ComputeSlot, candidate: ProviderCandidate) -> list[str]:
    mismatches: list[str] = []
    if candidate.contract_in != slot.contract_in:
        mismatches.append(
            f"contract_in is '{candidate.contract_in}', slot requires '{slot.contract_in}'"
        )
    if candidate.contract_out != slot.contract_out:
        mismatches.append(
            f"contract_out is '{candidate.contract_out}', slot requires '{slot.contract_out}'"
        )
    return mismatches


def _metadata_mismatches(slot: ComputeSlot, candidate: ProviderCandidate) -> list[str]:
    """Return billing and capability violations, as short prose reasons.

    ``billing`` is always constrained -- the schema requires at least one mode
    -- so a candidate declaring none can never intersect it. ``capabilities``
    constrains only when the slot names tags; then the candidate must claim all
    of them.
    """
    mismatches: list[str] = []
    if slot.billing and not (set(candidate.billing) & set(slot.billing)):
        mismatches.append(
            f"billing modes {list(candidate.billing)} do not intersect the slot's "
            f"{list(slot.billing)}"
        )
    missing = [tag for tag in slot.capabilities if tag not in set(candidate.capabilities)]
    if missing:
        mismatches.append(
            f"missing required capabilities {missing} (candidate claims "
            f"{list(candidate.capabilities)})"
        )
    return mismatches


def _reasons(slot: ComputeSlot, candidate: ProviderCandidate) -> list[str]:
    return _contract_mismatches(slot, candidate) + _metadata_mismatches(slot, candidate)


def _compatibility_issues(
    slot_name: str, slot: ComputeSlot, candidate: ProviderCandidate
) -> list[BlueprintIssue]:
    """Return one field-pathed issue per constraint the candidate violates."""
    issues: list[BlueprintIssue] = []
    if candidate.contract_in != slot.contract_in:
        issues.append(
            BlueprintIssue(
                path=f"slots.{slot_name}.contract_in",
                message=(
                    f"bound provider '{candidate.name}' declares "
                    f"contract_in='{candidate.contract_in}'"
                ),
                expected=f"contract_in='{slot.contract_in}' (names must match exactly)",
            )
        )
    if candidate.contract_out != slot.contract_out:
        issues.append(
            BlueprintIssue(
                path=f"slots.{slot_name}.contract_out",
                message=(
                    f"bound provider '{candidate.name}' declares "
                    f"contract_out='{candidate.contract_out}'"
                ),
                expected=f"contract_out='{slot.contract_out}' (names must match exactly)",
            )
        )
    if slot.billing and not (set(candidate.billing) & set(slot.billing)):
        issues.append(
            BlueprintIssue(
                path=f"slots.{slot_name}.billing",
                message=(
                    f"bound provider '{candidate.name}' declares billing modes "
                    f"{list(candidate.billing)}"
                ),
                expected=(
                    "at least one of: " + ", ".join(slot.billing)
                    if candidate.billing
                    else (
                        "at least one of: "
                        + ", ".join(slot.billing)
                        + " -- the candidate declares none, and an undeclared billing mode "
                        "is not a wildcard"
                    )
                ),
            )
        )
    missing = [tag for tag in slot.capabilities if tag not in set(candidate.capabilities)]
    if missing:
        issues.append(
            BlueprintIssue(
                path=f"slots.{slot_name}.capabilities",
                message=(
                    f"bound provider '{candidate.name}' does not claim {missing}; it claims "
                    f"{list(candidate.capabilities)}"
                ),
                expected="every tag in: " + ", ".join(slot.capabilities),
            )
        )
    return issues


def _near_matches(
    slot: ComputeSlot, catalog: Sequence[ProviderCandidate]
) -> list[tuple[ProviderCandidate, str]]:
    """Return candidates sharing at least one contract name, best match first.

    Phase-1 near-match is nominal: same ``contract_in`` and/or ``contract_out``
    name. It exists so an unbindable slot names the providers a human should
    look at, which is the pilot's discoverability lesson. A candidate whose
    contracts match exactly may still be unbindable on billing or capabilities,
    and says so rather than being advertised as ready to bind.
    """
    exact: list[tuple[ProviderCandidate, str]] = []
    input_only: list[tuple[ProviderCandidate, str]] = []
    output_only: list[tuple[ProviderCandidate, str]] = []
    for candidate in sorted(catalog, key=lambda item: item.name):
        mismatches = _contract_mismatches(slot, candidate)
        if not mismatches:
            metadata = _metadata_mismatches(slot, candidate)
            why = (
                "contracts match exactly, but " + "; ".join(metadata)
                if metadata
                else "contracts match exactly; bind it explicitly"
            )
            exact.append((candidate, why))
        elif candidate.contract_in == slot.contract_in:
            input_only.append((candidate, mismatches[0]))
        elif candidate.contract_out == slot.contract_out:
            output_only.append((candidate, mismatches[0]))
    return exact + input_only + output_only


# ---------------------------------------------------------------------------
# Procedure rewriting
# ---------------------------------------------------------------------------


def _rewrite_definition(
    definition: ProcedureDefinition,
    provider_map: Mapping[str, str],
    query_installs: Mapping[str, str],
) -> ProcedureDefinition:
    """Return the definition with slot references resolved to config names."""
    data = definition.model_dump(mode="json", by_alias=True, exclude_none=True)
    for step in data.get("steps", []):
        _rewrite_step(step, provider_map, query_installs)
        repeat = step.get("repeat")
        if isinstance(repeat, dict):
            for nested in repeat.get("steps", []):
                _rewrite_step(nested, provider_map, query_installs)
    try:
        return ProcedureDefinition.model_validate(data)
    except Exception as exc:  # pragma: no cover - defensive; inputs are pre-validated
        raise BlueprintValidationError(
            f"Lowered procedure '{definition.name}' no longer validates after slot "
            f"resolution: {exc}. This is a lowering bug, not a document error "
            f"(report against {INSTALLER_WORK_ITEM}'s upstream)."
        ) from None


def _rewrite_step(
    step: dict[str, Any],
    provider_map: Mapping[str, str],
    query_installs: Mapping[str, str],
) -> None:
    provider = step.get("provider")
    if isinstance(provider, str) and provider in provider_map:
        step["provider"] = provider_map[provider]
    query = step.get("query")
    if isinstance(query, str) and query in query_installs:
        step["query"] = query_installs[query]
