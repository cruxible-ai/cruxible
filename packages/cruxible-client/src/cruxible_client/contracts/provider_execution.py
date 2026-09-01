"""Frozen provider-execution records shared by the client and Playbill core."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.canonical import Sha256Value, normalize_canonical, typed_digest


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


class ProviderSecretReceiptReferenceV1(_StrictProviderExecutionModel):
    """Non-identifying custody reference committed by an invocation receipt."""

    tag: Literal["playbill-provider-secret-receipt-reference-v1"] = (
        "playbill-provider-secret-receipt-reference-v1"
    )
    binding_identity_digest: str
    purpose: str = ""

    _binding_digest = field_validator("binding_identity_digest")(_digest)


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
    """Provider budget window selected at admission.

    ``remaining_wall_clock_microseconds``, ``procedure_wall_clock_microseconds``,
    ``hard_cap_wall_clock_microseconds``, and ``runtime_wall_clock_seconds`` all
    read VALIDITY WINDOW.
    """

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


ProviderInvocationOutcomeClassV1 = Literal[
    "ok",
    "node_refusal",
    "operational",
    "internal",
]
ProviderInvocationAttributionV1 = Literal[
    "none",
    "implementation",
    "governed_binding",
    "closure_mirror_drift",
    "interface_registration",
    "runtime_compatibility",
    "executor",
    "local_deployment_binding",
    "materialization_integrity",
    "resolver",
    "cache",
    "deployment",
    "cache_integrity",
    "input",
    "input_budget",
    "custody",
    "binding_input",
    "input_upstream_response",
    "executor_mirror_drift",
    "provider_runtime",
]


class ProviderInvocationOutcomeV1(_StrictProviderExecutionModel):
    tag: Literal["playbill-provider-invocation-outcome-v1"] = (
        "playbill-provider-invocation-outcome-v1"
    )
    status: Literal["ok", "refused", "error"]
    outcome_class: ProviderInvocationOutcomeClassV1
    attribution: ProviderInvocationAttributionV1
    code: str | None = None
    message: str | None = None
    detail: object = Field(default_factory=dict)

    @field_validator("detail", mode="before")
    @classmethod
    def _detail(cls, value: object) -> object:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _status_correspondence(self) -> ProviderInvocationOutcomeV1:
        if self.status == "ok":
            if (
                self.outcome_class != "ok"
                or self.attribution != "none"
                or self.code is not None
                or self.message is not None
                or self.detail != {}
            ):
                raise ValueError("ok Provider outcome cannot carry refusal facts")
        elif self.outcome_class == "ok" or self.attribution == "none" or self.code is None:
            raise ValueError("non-ok Provider outcome requires class, attribution, and code")
        return self


PROVIDER_INVOCATION_OUTCOME_DOMAIN = "playbill-provider-invocation-outcome-v1"


def provider_invocation_outcome_digest(outcome: ProviderInvocationOutcomeV1) -> str:
    payload = outcome.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, PROVIDER_INVOCATION_OUTCOME_DOMAIN, payload).tagged


class ProviderInvocationReceiptV1(_StrictProviderExecutionModel):
    """Durable evidence for one Provider call.

    ``duration_microseconds`` reads VALIDITY WINDOW.  The call's start and
    completion instants are carried by the enclosing journal records, whose
    ``recorded_at`` fields read EVALUATION INSTANT.
    """

    tag: Literal["playbill-provider-invocation-receipt-v1"] = (
        "playbill-provider-invocation-receipt-v1"
    )
    invocation_id: str
    occurrence_path: str
    run_id: str
    admission_binding_digest: str
    provider_artifact_digest: str
    implementation_digest: str
    materialization_digest: str
    deployment_digest: str
    interface_id: str
    interface_digest: str
    protocol_version: str
    input_bucket: str
    capture_contract_digest: str | None = None
    input_digest: str
    outcome: ProviderInvocationOutcomeV1
    output: object | None = None
    egress: ProviderEgressObservationV1
    secret_references: tuple[ProviderSecretReceiptReferenceV1, ...] = ()
    budget_translation: ProviderBudgetTranslationV1
    duration_microseconds: int = Field(ge=0)
    trace: object = Field(default_factory=dict)
    stderr: str = ""

    @field_validator("output", "trace", mode="before")
    @classmethod
    def _canonical(cls, value: object | None) -> object | None:
        return None if value is None else normalize_canonical(value)

    _digests = field_validator(
        "admission_binding_digest",
        "provider_artifact_digest",
        "implementation_digest",
        "materialization_digest",
        "deployment_digest",
        "interface_digest",
        "capture_contract_digest",
        "input_digest",
    )(_digest)

    @field_validator("secret_references")
    @classmethod
    def _secret_references(
        cls,
        value: tuple[ProviderSecretReceiptReferenceV1, ...],
    ) -> tuple[ProviderSecretReceiptReferenceV1, ...]:
        expected = tuple(
            sorted(value, key=lambda item: item.binding_identity_digest.encode("ascii"))
        )
        identities = tuple(item.binding_identity_digest for item in value)
        if value != expected or len(identities) != len(set(identities)):
            raise ValueError("receipt secret identities must be digest-sorted and unique")
        return value

    @model_validator(mode="after")
    def _outcome_correspondence(self) -> ProviderInvocationReceiptV1:
        if (self.output is not None) != (self.outcome.status == "ok"):
            raise ValueError("only an ok Provider invocation may carry output")
        return self


PROVIDER_INVOCATION_RECEIPT_DOMAIN = "playbill-provider-invocation-receipt-v1"


def provider_invocation_receipt_digest(receipt: ProviderInvocationReceiptV1) -> str:
    payload = receipt.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(Sha256Value, PROVIDER_INVOCATION_RECEIPT_DOMAIN, payload).tagged


class ProviderInvocationStartedV1(_StrictProviderExecutionModel):
    """Provider-start payload whose journal ``recorded_at`` reads EVALUATION INSTANT."""

    tag: Literal["playbill-provider-invocation-started-v1"] = (
        "playbill-provider-invocation-started-v1"
    )
    invocation_id: str
    occurrence_path: str
    implementation_digest: str
    materialization_digest: str
    input_digest: str
    input_bucket: str

    _digests = field_validator("implementation_digest", "materialization_digest", "input_digest")(
        _digest
    )


class ProviderInvocationCompletedV1(_StrictProviderExecutionModel):
    """Provider-completion payload whose journal ``recorded_at`` reads EVALUATION INSTANT."""

    tag: Literal["playbill-provider-invocation-completed-v1"] = (
        "playbill-provider-invocation-completed-v1"
    )
    invocation_id: str
    receipt: ProviderInvocationReceiptV1
    receipt_digest: str

    _receipt_digest = field_validator("receipt_digest")(_digest)

    @model_validator(mode="after")
    def _receipt_correspondence(self) -> ProviderInvocationCompletedV1:
        if self.invocation_id != self.receipt.invocation_id:
            raise ValueError("completed Provider event names another invocation")
        if self.receipt_digest != provider_invocation_receipt_digest(self.receipt):
            raise ValueError("completed Provider receipt digest does not reproduce")
        return self


__all__ = [
    "PROVIDER_BUDGET_TRANSLATION_DOMAIN",
    "PROVIDER_EGRESS_OBSERVATION_DOMAIN",
    "PROVIDER_EXTERNAL_OCCURRENCE_PLAN_DOMAIN",
    "PROVIDER_INVOCATION_OUTCOME_DOMAIN",
    "PROVIDER_INVOCATION_RECEIPT_DOMAIN",
    "PROVIDER_SECRET_BINDING_IDENTITY_DOMAIN",
    "ProviderBudgetTranslationV1",
    "ProviderEgressObservationV1",
    "ProviderExternalOccurrencePlanV1",
    "ProviderInvocationAttributionV1",
    "ProviderInvocationCompletedV1",
    "ProviderInvocationOutcomeClassV1",
    "ProviderInvocationOutcomeV1",
    "ProviderInvocationReceiptV1",
    "ProviderInvocationStartedV1",
    "ProviderSecretBindingIdentityV1",
    "ProviderSecretReceiptReferenceV1",
    "ProviderSecretReferenceV1",
    "ProviderSecretResolutionPlanV1",
    "VerifiedProviderBindingV1",
    "provider_budget_translation_digest",
    "provider_egress_observation_digest",
    "provider_external_occurrence_plan_digest",
    "provider_invocation_outcome_digest",
    "provider_invocation_receipt_digest",
    "provider_secret_binding_identity_digest",
]
