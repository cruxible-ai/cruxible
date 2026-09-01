"""Frozen provider-execution records shared by the client and Playbill core."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.canonical import Sha256Value, typed_digest


class _StrictProviderExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _digest(value: str | None) -> str | None:
    if value is not None:
        Sha256Value.from_tagged(value)
    return value


class ProviderSecretReferenceV1(_StrictProviderExecutionModel):
    tag: Literal["playbill-provider-secret-reference-v1"] = "playbill-provider-secret-reference-v1"
    realm: str
    name: str
    epoch: str
    purpose: str = ""
    resolver_kind: Literal["file", "environment"]

    @field_validator("realm", "name", "epoch")
    @classmethod
    def _identifier(cls, value: str) -> str:
        if (
            not value
            or any(part in value for part in ("/", "\\", "\x00"))
            or value
            in {
                ".",
                "..",
            }
        ):
            raise ValueError("secret reference components must be plain nonempty names")
        return value

    @property
    def ref(self) -> str:
        return f"{self.realm}/{self.name}"


class ProviderSecretBindingIdentityV1(_StrictProviderExecutionModel):
    tag: Literal["playbill-provider-secret-binding-identity-v1"] = (
        "playbill-provider-secret-binding-identity-v1"
    )
    realm: str
    name: str

    @field_validator("realm", "name")
    @classmethod
    def _identifier(cls, value: str) -> str:
        if (
            not value
            or any(part in value for part in ("/", "\\", "\x00"))
            or value
            in {
                ".",
                "..",
            }
        ):
            raise ValueError("secret binding components must be plain nonempty names")
        return value


PROVIDER_SECRET_BINDING_IDENTITY_DOMAIN = "playbill-provider-secret-binding-identity-v1"


def provider_secret_binding_identity_digest(
    identity: ProviderSecretBindingIdentityV1,
) -> str:
    payload = identity.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        PROVIDER_SECRET_BINDING_IDENTITY_DOMAIN,
        payload,
    ).tagged


class ProviderSecretResolutionPlanV1(_StrictProviderExecutionModel):
    tag: Literal["playbill-provider-secret-resolution-plan-v1"] = (
        "playbill-provider-secret-resolution-plan-v1"
    )
    references: tuple[ProviderSecretReferenceV1, ...] = ()
    binding_identity_digests: tuple[str, ...] = ()

    @field_validator("references")
    @classmethod
    def _references(
        cls, value: tuple[ProviderSecretReferenceV1, ...]
    ) -> tuple[ProviderSecretReferenceV1, ...]:
        expected = tuple(sorted(value, key=lambda item: (item.realm.encode(), item.name.encode())))
        keys = tuple((item.realm, item.name) for item in value)
        if value != expected or len(keys) != len(set(keys)):
            raise ValueError("secret references must be binding-sorted and unique")
        return value

    @field_validator("binding_identity_digests")
    @classmethod
    def _digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("secret binding identity digests must be sorted and unique")
        for item in value:
            _digest(item)
        return value

    @model_validator(mode="after")
    def _correspondence(self) -> ProviderSecretResolutionPlanV1:
        expected = tuple(
            sorted(
                (
                    provider_secret_binding_identity_digest(
                        ProviderSecretBindingIdentityV1(realm=item.realm, name=item.name)
                    )
                    for item in self.references
                ),
                key=lambda item: item.encode("ascii"),
            )
        )
        if self.binding_identity_digests != expected:
            raise ValueError("secret plan identity digests do not reproduce its references")
        return self


class ProviderBudgetTranslationV1(_StrictProviderExecutionModel):
    tag: Literal["playbill-provider-budget-translation-v1"] = (
        "playbill-provider-budget-translation-v1"
    )
    remaining_wall_clock_microseconds: int = Field(ge=0)
    procedure_wall_clock_microseconds: int = Field(ge=1)
    hard_cap_wall_clock_microseconds: int = Field(ge=1)
    runtime_wall_clock_seconds: int = Field(ge=1)
    procedure_output_bytes_cap: int | None = Field(default=None, ge=1)
    hard_output_bytes_cap: int | None = Field(default=None, ge=1)
    policy_output_bytes_cap: int = Field(ge=1)
    runtime_output_bytes_cap: int = Field(ge=1)
    max_provider_calls: int = Field(ge=0)
    max_items: int | None = Field(default=None, ge=1)
    result_bytes_cap: int = Field(ge=1)
    cost_units: None = None

    @model_validator(mode="after")
    def _winning_caps(self) -> ProviderBudgetTranslationV1:
        if (
            self.runtime_wall_clock_seconds
            != min(
                self.remaining_wall_clock_microseconds,
                self.procedure_wall_clock_microseconds,
                self.hard_cap_wall_clock_microseconds,
            )
            // 1_000_000
        ):
            raise ValueError("provider wall-clock translation does not reproduce")
        output_candidates = [self.policy_output_bytes_cap]
        if self.hard_output_bytes_cap is not None:
            output_candidates.append(self.hard_output_bytes_cap)
        if self.procedure_output_bytes_cap is not None:
            output_candidates.append(self.procedure_output_bytes_cap)
        if self.runtime_output_bytes_cap != min(output_candidates):
            raise ValueError("provider output translation does not select the narrowest cap")
        return self


PROVIDER_BUDGET_TRANSLATION_DOMAIN = "playbill-provider-budget-translation-v1"


def provider_budget_translation_digest(translation: ProviderBudgetTranslationV1) -> str:
    payload = translation.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, PROVIDER_BUDGET_TRANSLATION_DOMAIN, payload).tagged


class ProviderEgressObservationV1(_StrictProviderExecutionModel):
    tag: Literal["playbill-provider-egress-observation-v1"] = (
        "playbill-provider-egress-observation-v1"
    )
    declared_endpoints: tuple[str, ...] = ()
    observed_endpoints: tuple[str, ...] = ()
    dynamic_endpoint_forms: tuple[Literal["dynamic:target-from-run-input"], ...] = ()
    observer_backend: str
    observer_grade: Literal["attribution", "conformance"]

    @field_validator("declared_endpoints", "observed_endpoints", "dynamic_endpoint_forms")
    @classmethod
    def _sets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("egress observation sets must be sorted and unique")
        return value


PROVIDER_EGRESS_OBSERVATION_DOMAIN = "playbill-provider-egress-observation-v1"


def provider_egress_observation_digest(observation: ProviderEgressObservationV1) -> str:
    payload = observation.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, PROVIDER_EGRESS_OBSERVATION_DOMAIN, payload).tagged


class VerifiedProviderBindingV1(_StrictProviderExecutionModel):
    tag: Literal["playbill-verified-provider-binding-v1"] = "playbill-verified-provider-binding-v1"
    provider_artifact_digest: str
    interface_artifact_digest: str
    interface_id: str
    interface_digest: str
    implementation_digest: str
    deployment_digest: str
    materialization_digest: str
    environment_manifest_digest: str
    entrypoint: str
    protocol_version: Literal["1.0"] = "1.0"
    declared_endpoints: tuple[str, ...] = ()

    _digests = field_validator(
        "provider_artifact_digest",
        "interface_artifact_digest",
        "interface_digest",
        "implementation_digest",
        "deployment_digest",
        "materialization_digest",
        "environment_manifest_digest",
    )(_digest)

    @field_validator("declared_endpoints")
    @classmethod
    def _endpoints(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("declared endpoints must be sorted and unique")
        return value


class ProviderExternalOccurrencePlanV1(_StrictProviderExecutionModel):
    """Complete static Provider closure for one graph-v4 external occurrence."""

    tag: Literal["playbill-provider-external-occurrence-plan-v1"] = (
        "playbill-provider-external-occurrence-plan-v1"
    )
    occurrence_path: str
    occurrence_kind: Literal["provider", "source"]
    node_id: str
    repeat_node_id: str | None = None
    input_name: str | None = None
    provider_artifact_digest: str
    interface_artifact_digest: str
    interface_id: str
    interface_digest: str
    vocabulary_digest: str
    classifier_digest: str
    accepted_bucket_selectors: tuple[str, ...]
    implementation_digest: str
    effect_class: Literal["none", "external_read", "external_mutation"]
    capture_contract_digest: str | None = None
    contract_input_digest: str | None = None
    contract_output_digest: str | None = None
    local_execution: VerifiedProviderBindingV1
    secret_plan: ProviderSecretResolutionPlanV1
    budget_translation: ProviderBudgetTranslationV1
    source_runtime_plan_digest: str | None = None

    _digests = field_validator(
        "provider_artifact_digest",
        "interface_artifact_digest",
        "interface_digest",
        "vocabulary_digest",
        "classifier_digest",
        "implementation_digest",
        "capture_contract_digest",
        "contract_input_digest",
        "contract_output_digest",
        "source_runtime_plan_digest",
    )(_digest)

    @field_validator("accepted_bucket_selectors")
    @classmethod
    def _selectors(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("accepted bucket selectors must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _correspondence(self) -> ProviderExternalOccurrencePlanV1:
        local = self.local_execution
        for label, expected, actual in (
            ("Provider", self.provider_artifact_digest, local.provider_artifact_digest),
            ("interface artifact", self.interface_artifact_digest, local.interface_artifact_digest),
            ("interface id", self.interface_id, local.interface_id),
            ("interface", self.interface_digest, local.interface_digest),
            ("implementation", self.implementation_digest, local.implementation_digest),
        ):
            if expected != actual:
                raise ValueError(f"external occurrence {label} differs from local binding")
        if self.occurrence_kind == "source":
            if (
                self.input_name is None
                or self.capture_contract_digest is None
                or self.source_runtime_plan_digest is None
                or self.contract_input_digest is not None
                or self.contract_output_digest is not None
            ):
                raise ValueError("Source occurrence requires only its Source runtime fields")
        elif (
            self.input_name is not None
            or self.capture_contract_digest is not None
            or self.source_runtime_plan_digest is not None
            or self.contract_input_digest is None
            or self.contract_output_digest is None
        ):
            raise ValueError("Provider occurrence requires only its contract fields")
        return self


PROVIDER_EXTERNAL_OCCURRENCE_PLAN_DOMAIN = "playbill-provider-external-occurrence-plan-v1"


def provider_external_occurrence_plan_digest(
    plan: ProviderExternalOccurrencePlanV1,
) -> str:
    payload = plan.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, PROVIDER_EXTERNAL_OCCURRENCE_PLAN_DOMAIN, payload).tagged


__all__ = [
    "PROVIDER_BUDGET_TRANSLATION_DOMAIN",
    "PROVIDER_EGRESS_OBSERVATION_DOMAIN",
    "PROVIDER_EXTERNAL_OCCURRENCE_PLAN_DOMAIN",
    "PROVIDER_SECRET_BINDING_IDENTITY_DOMAIN",
    "ProviderBudgetTranslationV1",
    "ProviderEgressObservationV1",
    "ProviderExternalOccurrencePlanV1",
    "ProviderSecretBindingIdentityV1",
    "ProviderSecretReferenceV1",
    "ProviderSecretResolutionPlanV1",
    "VerifiedProviderBindingV1",
    "provider_budget_translation_digest",
    "provider_egress_observation_digest",
    "provider_external_occurrence_plan_digest",
    "provider_secret_binding_identity_digest",
]
