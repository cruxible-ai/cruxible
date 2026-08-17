"""Deterministic conservative graph-v3 guard builders.

Builders are authoring inputs only.  Their output is a complete
``ProcedureDefinitionV3`` plus exact envelope pins and expansion evidence;
accepted artifacts never retain executable shorthand or a runtime profile
lookup.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_core.playbill.artifacts import ArtifactPin
from cruxible_core.playbill.canonical import (
    ArtifactDigest,
    Sha256Value,
    normalize_canonical,
    typed_digest,
)
from cruxible_core.playbill.procedures.graph import compute_procedure_definition_digest_v3
from cruxible_core.playbill.procedures.models import (
    ExhaustTapNodeV3,
    GuardNodeV3,
    GuardPredicateV1,
    PredicateOperandV1,
    ProcedureBudgetV3,
    ProcedureDefinitionV3,
    ProcedureHardCapsV3,
    ProjectNodeV3,
    SourceNodeV3,
    StateTapNodeV3,
)

_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


class _StrictBuilderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _pin_key(pin: ArtifactPin) -> tuple[bytes, bytes, bytes]:
    return (
        pin.role.encode("utf-8"),
        pin.target.qualified.encode("utf-8"),
        pin.artifact_digest.encode("ascii"),
    )


class GuardBuilderCommonV1(_StrictBuilderModel):
    name: str
    contract_in: ArtifactPin
    contract_out: ArtifactPin
    observed_path: tuple[str, ...]
    expected_value: None | bool | int | str
    operator: Literal[
        "eq",
        "ne",
        "gt",
        "gte",
        "lt",
        "lte",
        "before",
        "on_or_before",
        "after",
        "on_or_after",
    ] = "eq"
    refusal_code: str
    refusal_message: str
    budget: ProcedureBudgetV3
    hard_caps: ProcedureHardCapsV3
    terminal_capability: Literal[1, 2, 3] = 1
    authoring_source_digest: str

    @field_validator("observed_path")
    @classmethod
    def _observed_path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not _FIELD_RE.fullmatch(item) for item in value):
            raise ValueError("guard observed_path must be a nonempty canonical field path")
        return value

    @field_validator("expected_value", mode="before")
    @classmethod
    def _expected_value(cls, value: object) -> None | bool | int | str:
        normalized = normalize_canonical(value)
        if normalized is not None and not isinstance(normalized, bool | int | str):
            raise ValueError("guard expected_value must be one canonical scalar")
        return normalized

    @field_validator("authoring_source_digest")
    @classmethod
    def _source_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _contracts(self) -> "GuardBuilderCommonV1":
        if self.contract_in.role != "contract-in" or self.contract_in.target.kind != "Contract":
            raise ValueError("guard builder contract_in must be an exact Contract pin")
        if self.contract_out.role != "contract-out" or self.contract_out.target.kind != "Contract":
            raise ValueError("guard builder contract_out must be an exact Contract pin")
        return self


class AcceptedClaimGuardBuilderV1(GuardBuilderCommonV1):
    tag: Literal["playbill-accepted-claim-guard-builder-v1"] = (
        "playbill-accepted-claim-guard-builder-v1"
    )
    read_scope: Literal["local"] = "local"
    query: ArtifactPin
    claim_type: ArtifactPin
    parameters: object = {}

    @field_validator("parameters", mode="before")
    @classmethod
    def _parameters(cls, value: object) -> object:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _pins(self) -> "AcceptedClaimGuardBuilderV1":
        if self.query.role != "query" or self.query.target.kind != "QueryDefinition":
            raise ValueError("accepted-Claim builder requires an exact QueryDefinition pin")
        if self.claim_type.role != "claim-type" or self.claim_type.target.kind != "ClaimType":
            raise ValueError("accepted-Claim builder requires an exact ClaimType pin")
        return self


class SourceCaptureGuardBuilderV1(GuardBuilderCommonV1):
    tag: Literal["playbill-source-capture-guard-builder-v1"] = (
        "playbill-source-capture-guard-builder-v1"
    )
    capture_contract: ArtifactPin
    provider: ArtifactPin
    acquisition_policy: ArtifactPin
    request: object

    @field_validator("request", mode="before")
    @classmethod
    def _request(cls, value: object) -> object:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _pins(self) -> "SourceCaptureGuardBuilderV1":
        expected = (
            (self.capture_contract, "capture-contract", "CaptureContract"),
            (self.provider, "provider", "Provider"),
            (self.acquisition_policy, "acquisition-policy", "SourceAcquisitionPolicy"),
        )
        for pin, role, kind in expected:
            if pin.role != role or pin.target.kind != kind:
                raise ValueError(f"source guard builder requires exact {role} {kind} pin")
        return self


class ExhaustGuardBuilderV1(GuardBuilderCommonV1):
    tag: Literal["playbill-exhaust-guard-builder-v1"] = "playbill-exhaust-guard-builder-v1"
    reducer_or_query: ArtifactPin
    acquisition_policy: ArtifactPin
    journal_identity: str

    @model_validator(mode="after")
    def _pins(self) -> "ExhaustGuardBuilderV1":
        if self.reducer_or_query.role not in {"query", "reducer"} or (
            self.reducer_or_query.target.kind not in {"QueryDefinition", "Reducer"}
        ):
            raise ValueError("exhaust guard builder requires an exact query or reducer pin")
        if (
            self.acquisition_policy.role != "acquisition-policy"
            or self.acquisition_policy.target.kind != "SourceAcquisitionPolicy"
        ):
            raise ValueError("exhaust guard builder requires an exact SourceAcquisitionPolicy pin")
        return self


class BuilderSourceMappingV1(_StrictBuilderModel):
    tag: Literal["playbill-procedure-builder-source-map-v1"] = (
        "playbill-procedure-builder-source-map-v1"
    )
    node_id: str
    compact_field: str


class ProcedureGuardExpansionV1(_StrictBuilderModel):
    tag: Literal["playbill-procedure-guard-expansion-v1"] = "playbill-procedure-guard-expansion-v1"
    builder_kind: Literal["accepted_claim", "source_capture", "exhaust"]
    authoring_source_digest: str
    compiler_rule_digest: str
    definition: ProcedureDefinitionV3
    definition_digest: str
    envelope_pins: tuple[ArtifactPin, ...]
    required_acquisition_policy: ArtifactPin | None
    source_mappings: tuple[BuilderSourceMappingV1, ...]
    expanded_output_digest: str

    @field_validator(
        "authoring_source_digest",
        "compiler_rule_digest",
        "definition_digest",
        "expanded_output_digest",
    )
    @classmethod
    def _digests(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _reproduce(self) -> "ProcedureGuardExpansionV1":
        definition_digest = compute_procedure_definition_digest_v3(self.definition).tagged
        if self.definition_digest != definition_digest:
            raise ValueError("guard expansion definition digest does not reproduce")
        expected_output = typed_digest(
            ArtifactDigest,
            "playbill-procedure-guard-expanded-output-v1",
            {
                "definition": self.definition.model_dump(mode="json", by_alias=True),
                "envelope_pins": [pin.model_dump(mode="json") for pin in self.envelope_pins],
                "required_acquisition_policy": (
                    None
                    if self.required_acquisition_policy is None
                    else self.required_acquisition_policy.model_dump(mode="json")
                ),
                "source_mappings": [
                    mapping.model_dump(mode="json") for mapping in self.source_mappings
                ],
            },
        ).tagged
        if self.expanded_output_digest != expected_output:
            raise ValueError("guard expansion output digest does not reproduce")
        return self


GuardBuilderV1 = AcceptedClaimGuardBuilderV1 | SourceCaptureGuardBuilderV1 | ExhaustGuardBuilderV1


def _guard_and_project(
    spec: GuardBuilderCommonV1,
    *,
    input_node: StateTapNodeV3 | SourceNodeV3 | ExhaustTapNodeV3,
) -> tuple[StateTapNodeV3 | SourceNodeV3 | ExhaustTapNodeV3 | GuardNodeV3 | ProjectNodeV3, ...]:
    return (
        input_node,
        GuardNodeV3(
            node_id="guard",
            predicate=GuardPredicateV1(
                left=PredicateOperandV1(
                    kind="step",
                    alias="observed",
                    path=spec.observed_path,
                ),
                operator=spec.operator,
                right=PredicateOperandV1(kind="literal", value=spec.expected_value),
            ),
            on_false="$abort",
            refusal_code=spec.refusal_code,
            message=spec.refusal_message,
        ),
        ProjectNodeV3.model_validate(
            {
                "node_id": "project",
                "fields": {"value": "$steps.observed"},
                "contract_out": spec.contract_out,
                "as": "result",
            }
        ),
    )


def _expansion(
    spec: GuardBuilderV1,
    *,
    builder_kind: Literal["accepted_claim", "source_capture", "exhaust"],
    input_node: StateTapNodeV3 | SourceNodeV3 | ExhaustTapNodeV3,
    dependency_pins: tuple[ArtifactPin, ...],
    acquisition_policy: ArtifactPin | None,
    compiler_rule_digest: str,
) -> ProcedureGuardExpansionV1:
    Sha256Value.from_tagged(compiler_rule_digest)
    definition = ProcedureDefinitionV3(
        name=spec.name,
        description=f"Deterministically expanded {builder_kind} guard.",
        contract_in=spec.contract_in,
        contract_out=spec.contract_out,
        nodes=_guard_and_project(spec, input_node=input_node),
        returns="result",
        budget=spec.budget,
        hard_caps=spec.hard_caps,
        terminal_capability=spec.terminal_capability,
        annotations={
            "authoring_source_digest": spec.authoring_source_digest,
            "builder_kind": builder_kind,
            "builder_version": 1,
            "compiler_rule_digest": compiler_rule_digest,
        },
    )
    envelope_pins = tuple(
        sorted(
            {spec.contract_in, spec.contract_out, *dependency_pins},
            key=_pin_key,
        )
    )
    mappings = (
        BuilderSourceMappingV1(node_id="input", compact_field="input_plane"),
        BuilderSourceMappingV1(node_id="guard", compact_field="predicate"),
        BuilderSourceMappingV1(node_id="project", compact_field="output"),
    )
    definition_digest = compute_procedure_definition_digest_v3(definition).tagged
    output_digest = typed_digest(
        ArtifactDigest,
        "playbill-procedure-guard-expanded-output-v1",
        {
            "definition": definition.model_dump(mode="json", by_alias=True),
            "envelope_pins": [pin.model_dump(mode="json") for pin in envelope_pins],
            "required_acquisition_policy": (
                None if acquisition_policy is None else acquisition_policy.model_dump(mode="json")
            ),
            "source_mappings": [mapping.model_dump(mode="json") for mapping in mappings],
        },
    ).tagged
    return ProcedureGuardExpansionV1(
        builder_kind=builder_kind,
        authoring_source_digest=spec.authoring_source_digest,
        compiler_rule_digest=compiler_rule_digest,
        definition=definition,
        definition_digest=definition_digest,
        envelope_pins=envelope_pins,
        required_acquisition_policy=acquisition_policy,
        source_mappings=mappings,
        expanded_output_digest=output_digest,
    )


def build_accepted_claim_guard(
    spec: AcceptedClaimGuardBuilderV1,
    *,
    compiler_rule_digest: str,
) -> ProcedureGuardExpansionV1:
    return _expansion(
        spec,
        builder_kind="accepted_claim",
        input_node=StateTapNodeV3.model_validate(
            {
                "node_id": "input",
                "query": spec.query,
                "parameters": spec.parameters,
                "as": "observed",
            }
        ),
        dependency_pins=(spec.query, spec.claim_type),
        acquisition_policy=None,
        compiler_rule_digest=compiler_rule_digest,
    )


def build_source_capture_guard(
    spec: SourceCaptureGuardBuilderV1,
    *,
    compiler_rule_digest: str,
) -> ProcedureGuardExpansionV1:
    return _expansion(
        spec,
        builder_kind="source_capture",
        input_node=SourceNodeV3.model_validate(
            {
                "node_id": "input",
                "capture_contract": spec.capture_contract,
                "provider": spec.provider,
                "request": spec.request,
                "as": "observed",
            }
        ),
        dependency_pins=(spec.capture_contract, spec.provider),
        acquisition_policy=spec.acquisition_policy,
        compiler_rule_digest=compiler_rule_digest,
    )


def build_exhaust_guard(
    spec: ExhaustGuardBuilderV1,
    *,
    compiler_rule_digest: str,
) -> ProcedureGuardExpansionV1:
    return _expansion(
        spec,
        builder_kind="exhaust",
        input_node=ExhaustTapNodeV3.model_validate(
            {
                "node_id": "input",
                "reducer_or_query": spec.reducer_or_query,
                "journal_identity": spec.journal_identity,
                "as": "observed",
            }
        ),
        dependency_pins=(spec.reducer_or_query,),
        acquisition_policy=spec.acquisition_policy,
        compiler_rule_digest=compiler_rule_digest,
    )


__all__ = [
    "AcceptedClaimGuardBuilderV1",
    "BuilderSourceMappingV1",
    "ExhaustGuardBuilderV1",
    "GuardBuilderCommonV1",
    "ProcedureGuardExpansionV1",
    "SourceCaptureGuardBuilderV1",
    "build_accepted_claim_guard",
    "build_exhaust_guard",
    "build_source_capture_guard",
]
